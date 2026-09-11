"""把正式 Task Bundle 交给 Harbor/AGS 的受控运行桥接。

桥接层只负责运行边界，不负责生成任务、解答或评分逻辑。默认只生成
``rollout_plan.json``，只有调用方显式设置 ``execute=True`` 才会启动 Harbor。
凭据永远从当前进程环境读取，不写入命令、配置或 artifact。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.trajectory.json_codec import stable_id

from .adapter import validate_bundle_layout

ROLLOUT_BRIDGE_SCHEMA = "traceforge.harbor-ags-rollout-bridge.v1"
ROLLOUT_RECEIPT_SCHEMA = "traceforge.harbor-ags-rollout-receipt.v1"


class HarborRolloutError(RuntimeError):
    """Harbor/AGS 运行桥接无法安全继续。"""


@dataclass(frozen=True, slots=True)
class HarborRolloutConfig:
    """一次 Harbor Job 的显式、可序列化配置。"""

    task_dir: Path
    harbor_root: Path
    output_root: Path
    jobs_root: Path
    agent_mode: str = "hermes"
    model: str = "anthropic/claude-opus-4-8"
    trials: int = 1
    concurrency: int = 1
    timeout_seconds: int = 900
    expected_hermes_commit: str | None = None

    def validate(self) -> None:
        if self.agent_mode not in {"hermes", "oracle", "nop"}:
            raise HarborRolloutError("agent_mode 必须是 hermes/oracle/nop")
        if self.trials < 1 or self.concurrency < 1 or self.concurrency > self.trials:
            raise HarborRolloutError("trials/concurrency 必须满足 1 <= concurrency <= trials")
        if self.timeout_seconds < 1:
            raise HarborRolloutError("timeout_seconds 必须大于 0")
        if not self.model.strip():
            raise HarborRolloutError("model 不能为空")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_name(task_dir: Path) -> str:
    try:
        payload = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise HarborRolloutError("task.toml 无法解析") from exc
    task = payload.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("name"), str):
        raise HarborRolloutError("task.toml 缺少 task.name")
    value = task["name"].strip()
    if not value:
        raise HarborRolloutError("task.name 不能为空")
    return value


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value).strip()


def _materialize_dataset(
    task_dir: Path, destination: Path, *, trials: int
) -> tuple[Path, dict[str, Any]]:
    """将单个 Bundle 复制成 Harbor 可读取的 Dataset；源目录只读消费。"""

    if destination.exists():
        raise HarborRolloutError(f"Dataset 目标已存在，拒绝覆盖：{destination}")
    destination.mkdir(parents=True)
    task_name = _task_name(task_dir)
    task_slug = _safe_name(task_name.replace("/", "_")) or "task"
    task_targets = []
    for index in range(1, trials + 1):
        suffix = f"--trial-{index:03d}" if trials > 1 else ""
        task_target = destination / f"{task_slug}{suffix}"
        shutil.copytree(task_dir, task_target, symlinks=False)
        task_targets.append(task_target.relative_to(destination).as_posix())
    dataset_toml = (
        "[dataset]\n"
        f'name = "traceforge/generated-{hashlib.sha256(task_name.encode()).hexdigest()[:12]}"\n'
        'version = "1.0.0"\n'
        'description = "TraceForge reconstructed task"\n'
        'authors = [{ name = "TraceForge" }]\n'
    )
    (destination / "dataset.toml").write_text(dataset_toml, encoding="utf-8")
    return destination, {
        "dataset_root": str(destination),
        "task_relative_paths": task_targets,
        "trial_count": trials,
        "dataset_toml_sha256": _sha256_file(destination / "dataset.toml"),
    }


def _credential_status(agent_mode: str) -> dict[str, Any]:
    names = ("AGS_API_KEY", "TOKENHUB_KEY", "ANTHROPIC_API_KEY")
    required = ["AGS_API_KEY"]
    if agent_mode == "hermes":
        required.append("TOKENHUB_KEY or ANTHROPIC_API_KEY")
    return {
        "required_env": required,
        "present_env": [name for name in names if os.environ.get(name, "").strip()],
        "credentials_embedded": False,
    }


def _harbor_command(root: Path) -> list[str]:
    candidate = root / ".venv/bin/harbor"
    if candidate.is_file():
        if os.access(candidate, os.X_OK):
            return [str(candidate)]
        interpreter = root / ".venv/bin/python"
        if interpreter.is_file():
            return [str(interpreter), str(candidate)]
    resolved = shutil.which("harbor")
    if resolved:
        return [resolved]
    raise HarborRolloutError("找不到 Harbor 可执行文件")


def _render_harbor_config(
    *,
    source: Path,
    jobs_root: Path,
    agent_mode: str,
    model: str,
    concurrency: int,
    timeout_seconds: int,
    expected_hermes_commit: str | None,
) -> str:
    """从已验收配置派生本次冻结配置，并覆盖显式运行参数。"""

    try:
        rendered = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarborRolloutError(f"无法读取 Harbor 配置：{source}") from exc
    replacements = {
        r"(?m)^jobs_dir:.*$": f"jobs_dir: {json.dumps(str(jobs_root))}",
        r"(?m)^n_concurrent_trials:.*$": f"n_concurrent_trials: {concurrency}",
        r"(?m)^(\s+sandbox_timeout_sec:).*$": rf"\g<1> {timeout_seconds}",
    }
    if agent_mode == "hermes":
        replacements[r"(?m)^(\s+model_name:).*$"] = rf"\g<1> {json.dumps(model)}"
        if expected_hermes_commit:
            replacements[r"(?m)^(\s+expected_commit:).*$"] = (
                rf"\g<1> {json.dumps(expected_hermes_commit)}"
            )
    for pattern, replacement in replacements.items():
        rendered, count = re.subn(pattern, replacement, rendered, count=1)
        if count != 1:
            raise HarborRolloutError(f"Harbor 配置缺少可覆盖字段：{pattern}")
    return rendered


def build_rollout_plan(config: HarborRolloutConfig) -> Path:
    """校验 Bundle 并发布可执行计划；不启动模型。"""

    config.validate()
    task_dir = config.task_dir.resolve()
    harbor_root = config.harbor_root.resolve()
    layout = validate_bundle_layout(task_dir)
    config_name = {
        "hermes": "hermes-batch.yaml",
        "oracle": "oracle.yaml",
        "nop": "nop.yaml",
    }[config.agent_mode]
    harbor_config = harbor_root / "configs" / config_name
    if not harbor_config.is_file():
        raise HarborRolloutError(f"Harbor 项目缺少 configs/{config_name}")
    harbor_command = _harbor_command(harbor_root)
    task_name = _task_name(task_dir)
    bundle_digest = hashlib.sha256(
        json.dumps(layout, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    run_id = stable_id(
        "traceforge-harbor-rollout-v1",
        {
            "bundle_digest": bundle_digest,
            "model": config.model,
            "trials": config.trials,
            "concurrency": config.concurrency,
            "expected_hermes_commit": config.expected_hermes_commit,
        },
    )
    workspace = ArtifactWorkspace(config.output_root.resolve(), run_id)
    try:
        _dataset_staging_root, dataset_meta = _materialize_dataset(
            task_dir, workspace.staging_path / "dataset", trials=config.trials
        )
        dataset_root = workspace.final_path / "dataset"
        dataset_meta["dataset_root"] = str(dataset_root)
        jobs_root = config.jobs_root.resolve()
        jobs_root.mkdir(parents=True, exist_ok=True)
        rendered_config = _render_harbor_config(
            source=harbor_config,
            jobs_root=jobs_root,
            agent_mode=config.agent_mode,
            model=config.model,
            concurrency=config.concurrency,
            timeout_seconds=config.timeout_seconds,
            expected_hermes_commit=config.expected_hermes_commit,
        )
        (workspace.staging_path / "harbor-config.yaml").write_text(
            rendered_config, encoding="utf-8"
        )
        published_config_path = workspace.final_path / "harbor-config.yaml"
        command = [
            *harbor_command,
            "run",
            "-c",
            str(published_config_path),
            "-p",
            str(dataset_root),
        ]
        plan = {
            "schema_version": ROLLOUT_BRIDGE_SCHEMA,
            "run_id": run_id,
            "status": "READY",
            "task_name": task_name,
            "source_bundle": str(task_dir),
            "bundle_sha256": bundle_digest,
            "dataset": dataset_meta,
            "command": command,
            "harbor_root": str(harbor_root),
            "jobs_root": str(jobs_root),
            "agent": {
                "mode": config.agent_mode,
                "model": config.model,
                "trials": config.trials,
                "concurrency": config.concurrency,
                "timeout_seconds": config.timeout_seconds,
                "expected_hermes_commit": config.expected_hermes_commit,
            },
            "verifier": {
                "environment_mode": "separate",
                "network_mode": "no-network",
                "artifact_manifest_required": True,
                "sandbox_cleanup_required": True,
            },
            "credentials": _credential_status(config.agent_mode),
            "harbor_config": {
                "source": str(harbor_config.resolve()),
                "materialized": str(published_config_path),
                "sha256": hashlib.sha256(rendered_config.encode("utf-8")).hexdigest(),
            },
            "external_execution": False,
        }
        entries = [write_json_artifact(workspace.staging_path, "rollout_plan.json", plan)]
        manifest_entry = write_json_artifact(
            workspace.staging_path,
            "artifact_manifest.json",
            {
                "schema_version": "traceforge.harbor-ags-rollout-artifacts.v1",
                "run_id": run_id,
                "files": artifact_entry_dicts(entries),
            },
        )
        write_json_artifact(
            workspace.staging_path,
            "run_receipt.json",
            {
                "schema_version": ROLLOUT_RECEIPT_SCHEMA,
                "run_id": run_id,
                "status": "PLAN_ONLY",
                "artifact_manifest_sha256": manifest_entry.sha256,
                "external_execution": False,
            },
        )
        return workspace.publish()
    except BaseException:
        workspace.abort()
        raise


def execute_rollout_plan(plan_dir: Path, *, timeout_seconds: int = 900) -> dict[str, Any]:
    """显式执行已发布计划；执行结果只保留有界 stdout/stderr 摘要。"""

    plan_path = plan_dir / "rollout_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborRolloutError("rollout_plan.json 无法读取") from exc
    if not isinstance(plan, dict) or plan.get("schema_version") != ROLLOUT_BRIDGE_SCHEMA:
        raise HarborRolloutError("rollout plan schema 不匹配")
    command = plan.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise HarborRolloutError("rollout plan command 非法")
    env = os.environ.copy()
    missing = [name for name in ("AGS_API_KEY",) if not env.get(name, "").strip()]
    agent = plan.get("agent")
    mode = agent.get("mode") if isinstance(agent, dict) else "hermes"
    if mode == "hermes" and not (
        env.get("TOKENHUB_KEY", "").strip() or env.get("ANTHROPIC_API_KEY", "").strip()
    ):
        missing.append("TOKENHUB_KEY/ANTHROPIC_API_KEY")
    if missing:
        raise HarborRolloutError(f"执行 Harbor 前缺少凭据环境变量：{', '.join(missing)}")
    try:
        result = subprocess.run(
            command,
            cwd=plan.get("harbor_root"),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "TIMEOUT",
            "returncode": None,
            "stdout": "",
            "stderr": str(exc)[:2000],
        }
    return {
        "status": "COMPLETED" if result.returncode == 0 else "FAILED",
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


__all__ = [
    "ROLLOUT_BRIDGE_SCHEMA",
    "HarborRolloutConfig",
    "HarborRolloutError",
    "build_rollout_plan",
    "execute_rollout_plan",
]
