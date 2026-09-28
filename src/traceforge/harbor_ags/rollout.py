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
from urllib.parse import urlsplit

from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    iter_config_items,
    load_channel_connection,
    load_e2b_api_key,
)
from traceforge.reconstruction.tls import pin_process_tls
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.trajectory.json_codec import stable_id

from .adapter import validate_bundle_layout, validate_harbor_bundle

DEFAULT_RUNTIME_CONFIG = Path(
    "/mnt/afs_toolcall/wujian1/Projects/workspace/TraceRconstruction/config.yaml"
)

ROLLOUT_BRIDGE_SCHEMA = "traceforge.harbor-ags-rollout-bridge.v1"
ROLLOUT_RECEIPT_SCHEMA = "traceforge.harbor-ags-rollout-receipt.v1"
DEFAULT_AGS_DOMAIN = "ap-beijing.tencentags.com"
DEFAULT_AGS_TEMPLATE = "node-python-hermes"
DEFAULT_TOKENHUB_BASE_URL = "https://tokenhub.sensetime.com"
_AGS_KEY_ENV = ("AGS_API_KEY", "E2B_API_KEY", "ROLLOUT_E2B_API_KEY")
_MODEL_KEY_ENV = ("TOKENHUB_KEY", "ANTHROPIC_API_KEY", "ROLLOUT_LLM_API_KEY")
_MODEL_URL_ENV = ("TOKENHUB_BASE_URL", "ANTHROPIC_BASE_URL", "ROLLOUT_LLM_BASE_URL")
_SECRET_OUTPUT_RE = re.compile(
    r"(?i)(?:authorization\s*:\s*bearer\s+|(?:api[_-]?key|token|secret|password)\s*[=:]\s*)([^\s,;]+)|\bsk-[A-Za-z0-9_-]{12,}\b"
)


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
    agent_max_iterations: int = 30
    expected_hermes_commit: str | None = None

    def validate(self) -> None:
        if self.agent_mode not in {"hermes", "oracle", "nop"}:
            raise HarborRolloutError("agent_mode 必须是 hermes/oracle/nop")
        if self.trials < 1 or self.concurrency < 1 or self.concurrency > self.trials:
            raise HarborRolloutError("trials/concurrency 必须满足 1 <= concurrency <= trials")
        if self.timeout_seconds < 1:
            raise HarborRolloutError("timeout_seconds 必须大于 0")
        if self.agent_max_iterations < 1:
            raise HarborRolloutError("agent_max_iterations 必须大于 0")
        if not self.model.strip():
            raise HarborRolloutError("model 不能为空")
        if self.agent_mode == "hermes":
            provider, _, model = self.model.partition("/")
            if provider not in {"anthropic", "deepseek", "vol"} or not model.strip():
                raise HarborRolloutError(
                    "Hermes rollout 模型必须是受支持 provider/model（anthropic、deepseek 或 vol）"
                )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _harbor_runtime_metadata(harbor_root: Path) -> dict[str, Any]:
    """Record the external Harbor evidence runtime used for this plan."""
    files: dict[str, str] = {}
    for relative in (
        "src/harbor_ags/agent.py",
        "src/harbor_ags/capture.py",
        "src/harbor_ags/evidence.py",
        "src/harbor_ags/validator.py",
    ):
        runtime_file = harbor_root / relative
        if runtime_file.is_file():
            files[relative] = _sha256_file(runtime_file)
    return {"files": files}


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


def redact_harbor_output(value: str) -> str:
    """对有限长度 Harbor 输出做凭据脱敏；调用方负责限制总长度。"""
    return _SECRET_OUTPUT_RE.sub("[REDACTED]", value)


def _redact_output(value: str) -> str:
    """Bounded Harbor logs may contain credentials; never persist them."""
    return redact_harbor_output(value)


def _materialize_dataset(
    task_dir: Path,
    destination: Path,
    *,
    trials: int,
    harbor_root: Path,
) -> tuple[Path, dict[str, Any]]:
    """复制 Bundle，并发布 Harbor 编译清单，确保运行后可做完整绑定。"""

    if destination.exists():
        raise HarborRolloutError(f"Dataset 目标已存在，拒绝覆盖：{destination}")
    destination.mkdir(parents=True)
    task_name = _task_name(task_dir)
    task_slug = _safe_name(task_name.replace("/", "_")) or "task"
    task_targets: list[str] = []
    task_hashes: dict[str, str] = {}
    manifest_tasks: list[dict[str, Any]] = []
    for index in range(1, trials + 1):
        suffix = f"--trial-{index:03d}" if trials > 1 else ""
        task_target = destination / f"{task_slug}{suffix}"
        shutil.copytree(task_dir, task_target, symlinks=False)
        _ensure_workspace_snapshot_hook(task_target / "task.toml")
        bundle_contract = validate_harbor_bundle(task_target, harbor_root=harbor_root)
        relative = task_target.relative_to(destination).as_posix()
        task_targets.append(relative)
        task_hashes[relative] = _sha256_tree(task_target)
        manifest_tasks.append(
            {
                "task_name": bundle_contract["task_name"],
                "task_dir": relative,
                "instruction_sha256": bundle_contract["instruction_sha256"],
                "task_bundle": bundle_contract,
            }
        )
    dataset_toml = (
        "[dataset]\n"
        f'name = "traceforge/generated-{hashlib.sha256(task_name.encode()).hexdigest()[:12]}"\n'
        'version = "1.0.0"\n'
        'description = "TraceForge reconstructed task"\n'
        'authors = [{ name = "TraceForge" }]\n'
    )
    (destination / "dataset.toml").write_text(dataset_toml, encoding="utf-8")
    compile_manifest = {
        "schema_version": "traceforge-harbor-compiled-dataset/v1",
        "scenario": "traceforge-reconstruction",
        "n_trials": trials,
        "tasks": manifest_tasks,
    }
    compile_path = destination / "compile-manifest.json"
    compile_path.write_text(
        json.dumps(compile_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return destination, {
        "dataset_root": str(destination),
        "task_relative_paths": task_targets,
        "trial_count": trials,
        "task_hashes": task_hashes,
        "dataset_toml_sha256": _sha256_file(destination / "dataset.toml"),
        "compile_manifest_sha256": _sha256_file(compile_path),
    }

def _ensure_workspace_snapshot_hook(task_toml: Path) -> None:
    """声明在 Harbor artifact collection 前复制 Agent 最终 workspace。

    Harbor 的 ``verifier.collect`` hook 在 Agent 阶段结束后、沙盒销毁前执行。
    快照只来自 ``/home/user/workspace``，随后由约定的
    ``/logs/artifacts/traceforge`` convention artifact 下载并投影给独立 verifier。
    """

    try:
        raw = task_toml.read_text(encoding="utf-8")
        tomllib.loads(raw)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise HarborRolloutError("复制后的 task.toml 无法解析") from exc
    marker_text = "TraceForge workspace snapshot hook"
    if marker_text in raw:
        return
    marker = (
        "\n# TraceForge workspace snapshot hook\n"
        "[[verifier.collect]]\n"
        'command = "set -eu; rm -rf /logs/artifacts/traceforge/workspace; '
        "mkdir -p /logs/artifacts/traceforge/workspace; "
        'cp -a /home/user/workspace/. /logs/artifacts/traceforge/workspace/"\n'
        'service = "main"\n'
        'user = "root"\n'
        "timeout_sec = 120.0\n"
    )
    # Array-of-table must be appended after all existing tables. Inserting it
    # inside [verifier] would re-parent later fields and can create duplicate
    # keys when the hook declares timeout/user itself.
    rendered = raw.rstrip() + "\n" + marker.lstrip("\n")
    try:
        tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise HarborRolloutError("snapshot hook 生成了非法 task.toml") from exc
    task_toml.write_text(rendered, encoding="utf-8")


def _first_env(env: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return ""


def _is_loopback_url(value: str) -> bool:
    try:
        host = (urlsplit(value).hostname or "").lower()
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def _credential_status(agent_mode: str) -> dict[str, Any]:
    names = (
        "AGS_API_KEY",
        "E2B_API_KEY",
        "TOKENHUB_KEY",
        "ANTHROPIC_API_KEY",
        "TOKENHUB_BASE_URL",
        "AGS_TEMPLATE_ID",
        "AGS_DOMAIN",
    )
    required = ["AGS_API_KEY or E2B_API_KEY"]
    if agent_mode == "hermes":
        required.append("TOKENHUB_KEY or ANTHROPIC_API_KEY")
    return {
        "required_env": required,
        "present_env": [name for name in names if os.environ.get(name, "").strip()],
        "credentials_embedded": False,
    }


def _apply_runtime_config(
    env: dict[str, str],
    *,
    config_path: Path | None,
    channel: str | None,
) -> dict[str, str]:
    """用 config.yaml 填补缺失的 AGS / TokenHub 凭据，不把密钥写回计划。"""

    if config_path is None:
        return env
    path = Path(config_path)
    if not path.is_file():
        raise HarborRolloutError(f"运行配置不存在：{path}")
    try:
        sandbox_key = load_e2b_api_key(path)
    except ModelGatewayError as exc:
        raise HarborRolloutError(str(exc)) from exc
    if sandbox_key:
        env.setdefault("AGS_API_KEY", sandbox_key)
        env.setdefault("E2B_API_KEY", sandbox_key)
    selected = (channel or "").strip()
    if selected:
        try:
            url, key = load_channel_connection(path, selected)
        except ModelGatewayError as exc:
            raise HarborRolloutError(str(exc)) from exc
        if not env.get("TOKENHUB_KEY", "").strip():
            env["TOKENHUB_KEY"] = key
        configured = env.get("TOKENHUB_BASE_URL", "").strip()
        if (not configured or _is_loopback_url(configured)) and url and not _is_loopback_url(url):
            env["TOKENHUB_BASE_URL"] = url.rstrip("/")
    return env


def _prepare_execution_env(
    agent_mode: str,
    *,
    config_path: Path | None = None,
    channel: str | None = None,
) -> dict[str, str]:
    """为 Harbor/AGS 子进程补齐模板、域名和可注入的模型地址。

    ``AGSPrebuiltEnvironment.preflight()`` 只读进程环境，不读 YAML kwargs。
    Hermes 配置用 ``${TOKENHUB_KEY}`` / ``${TOKENHUB_BASE_URL}`` 插值，因此必须
    把宿主机上的 Anthropic 别名映射过去。沙盒内不能使用宿主机 loopback 代理。
    若提供 ``config.yaml``，用其中的 ``e2bapikey`` 与 channel 填补缺失凭据。
    """

    env = os.environ.copy()
    pin_process_tls(env)
    env = _apply_runtime_config(env, config_path=config_path, channel=channel)
    ags_key = _first_env(env, _AGS_KEY_ENV)
    if ags_key and not env.get("AGS_API_KEY", "").strip():
        env["AGS_API_KEY"] = ags_key
    if not env.get("AGS_DOMAIN", "").strip():
        env["AGS_DOMAIN"] = (
            _first_env(env, ("E2B_DOMAIN", "ROLLOUT_E2B_DOMAIN")) or DEFAULT_AGS_DOMAIN
        )
    if not env.get("AGS_TEMPLATE_ID", "").strip() and not env.get(
        "ROLLOUT_E2B_TEMPLATE", ""
    ).strip():
        env["AGS_TEMPLATE_ID"] = DEFAULT_AGS_TEMPLATE
    if not env.get("E2B_VALIDATE_API_KEY", "").strip():
        env["E2B_VALIDATE_API_KEY"] = "false"
    if agent_mode == "hermes":
        model_key = _first_env(env, _MODEL_KEY_ENV)
        if model_key and not env.get("TOKENHUB_KEY", "").strip():
            env["TOKENHUB_KEY"] = model_key
        raw_url = _first_env(env, _MODEL_URL_ENV)
        if not raw_url or _is_loopback_url(raw_url):
            env["TOKENHUB_BASE_URL"] = DEFAULT_TOKENHUB_BASE_URL
        elif not env.get("TOKENHUB_BASE_URL", "").strip():
            env["TOKENHUB_BASE_URL"] = raw_url.rstrip("/")
        public_url = env["TOKENHUB_BASE_URL"].rstrip("/")
        # 盒内 Hermes / preflight 读 ANTHROPIC_BASE_URL 优先。宿主机 loopback
        # 代理在 AGS 里不可达，必须改成公网 TokenHub。
        for name in _MODEL_URL_ENV:
            current = env.get(name, "").strip()
            if not current or _is_loopback_url(current):
                env[name] = public_url
    return env


def _reviewed_rollout_budget(path: Path | None) -> tuple[int | None, int | None]:
    # Read only explicitly reviewed rollout limits from config.yaml.
    if path is None:
        return None, None
    for name, value in iter_config_items(path):
        if name != "roles" or not isinstance(value, dict):
            continue
        entry = value.get("rollout")
        if not isinstance(entry, dict):
            return None, None
        limits = []
        for key in ("timeout_seconds", "max_iterations"):
            configured = entry.get(key)
            limits.append(configured if type(configured) is int and configured > 0 else None)
        return limits[0], limits[1]
    return None, None


def _missing_execution_credentials(env: dict[str, str], agent_mode: str) -> list[str]:
    missing: list[str] = []
    if not _first_env(env, _AGS_KEY_ENV):
        missing.append("AGS_API_KEY/E2B_API_KEY")
    if agent_mode == "hermes" and not _first_env(env, _MODEL_KEY_ENV):
        missing.append("TOKENHUB_KEY/ANTHROPIC_API_KEY")
    return missing


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


def _rewrite_extra_instruction_paths(rendered: str, harbor_root: Path) -> str:
    appendix = harbor_root / "configs" / "runtime-appendix.md"
    if not appendix.is_file():
        return rendered
    rewritten, count = re.subn(
        r"(?m)^(\s+- )configs/runtime-appendix\.md\s*$",
        rf"\g<1>{appendix.as_posix()}",
        rendered,
        count=1,
    )
    return rewritten if count == 1 else rendered


def _bind_environment_timeouts(rendered: str, timeout_seconds: int) -> str:
    """让 AGS 长命令与声明的 rollout 预算一致，避免默认 120 秒提前中断。"""
    pattern = r"(?ms)^environment:[ \t]*\n.*?(?=^\S|\Z)"
    blocks = list(re.finditer(pattern, rendered))
    if len(blocks) != 1:
        raise HarborRolloutError("Harbor 配置必须包含唯一 environment 块")
    block = blocks[0]
    body = block.group(0)
    anchor = re.search(r"(?m)^([ \t]+)sandbox_timeout_sec:.*$", body)
    if anchor is None:
        raise HarborRolloutError("Harbor environment 缺少 sandbox_timeout_sec")
    indent = anchor.group(1)
    body = re.sub(
        rf"(?m)^{indent}(?:request_timeout_sec|transfer_timeout_sec):[^\n]*\n?",
        "",
        body,
    )
    lines = "\n".join(
        f"{indent}{key}: {timeout_seconds}"
        for key in ("sandbox_timeout_sec", "request_timeout_sec", "transfer_timeout_sec")
    )
    body, count = re.subn(rf"(?m)^{indent}sandbox_timeout_sec:.*$", lines, body)
    if count != 1:
        raise HarborRolloutError("Harbor environment 的 sandbox_timeout_sec 必须唯一")
    return rendered[:block.start()] + body + rendered[block.end():]


def _bind_agent_budgets(rendered: str, iterations: int, timeout_seconds: int) -> str:
    """把时间和轮次限制写入唯一 agent，避免误改 environment 的同名字段。"""
    blocks = list(re.finditer(r"(?ms)^agents:[ \t]*\n.*?(?=^\S|\Z)", rendered))
    if len(blocks) != 1:
        raise HarborRolloutError("Harbor 配置必须包含唯一 agents 块")
    block = blocks[0]
    body = block.group(0)
    agents = list(re.finditer(r"(?m)^([ \t]+)-[ \t]+(?:import_path|name):", body))
    if len(agents) != 1:
        raise HarborRolloutError("Harbor 配置必须包含唯一 agent")
    indent = agents[0].group(1) + "  "
    timeout_pattern = rf"(?m)^{indent}override_timeout_sec:[^\n]*$"
    if re.search(timeout_pattern, body):
        body = re.sub(timeout_pattern, f"{indent}override_timeout_sec: {timeout_seconds}", body)
    else:
        body = body.rstrip() + f"\n{indent}override_timeout_sec: {timeout_seconds}\n"
    kwargs_pattern = rf"(?m)^{indent}kwargs:[ \t]*$"
    kwargs = re.search(kwargs_pattern, body)
    if kwargs is None:
        body = body.rstrip() + f"\n{indent}kwargs:\n{indent}  max_iterations: {iterations}\n"
    else:
        # 仅在当前 kwargs 的直属字段中改写，不能匹配 env 映射里的内容。
        tail = body[kwargs.end():]
        boundary = re.search(rf"(?m)^{indent}\S", tail)
        finish = kwargs.end() + boundary.start() if boundary else len(body)
        contents = body[kwargs.end():finish]
        pattern = rf"(?m)^{indent}  max_iterations:[^\n]*$"
        if re.search(pattern, contents):
            contents = re.sub(pattern, f"{indent}  max_iterations: {iterations}", contents)
        else:
            contents = f"\n{indent}  max_iterations: {iterations}" + contents
        body = body[:kwargs.end()] + contents + body[finish:]
    return rendered[:block.start()] + body + rendered[block.end():]


def _job_timeout_seconds(trials: int, concurrency: int, agent_timeout_seconds: int) -> int:
    """为串/并行 trials 预留每个 agent 外的建环境与清理时间。"""
    batches = (trials + concurrency - 1) // concurrency
    return batches * (agent_timeout_seconds + 900) + 60


def _render_harbor_config(
    *,
    source: Path,
    harbor_root: Path,
    jobs_root: Path,
    job_name: str,
    agent_mode: str,
    model: str,
    concurrency: int,
    timeout_seconds: int,
    agent_max_iterations: int,
    expected_hermes_commit: str | None,
) -> str:
    """从已验收配置派生本次冻结配置，并覆盖显式运行参数。"""

    try:
        rendered = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HarborRolloutError(f"无法读取 Harbor 配置：{source}") from exc
    job_name_line = f"job_name: {json.dumps(job_name)}"
    if re.search(r"(?m)^job_name:", rendered):
        rendered, count = re.subn(r"(?m)^job_name:.*$", job_name_line, rendered, count=1)
        if count != 1:
            raise HarborRolloutError("Harbor 配置的 job_name 无法覆盖")
    else:
        rendered = f"{job_name_line}\n{rendered}"
    replacements = {
        r"(?m)^jobs_dir:.*$": f"jobs_dir: {json.dumps(str(jobs_root))}",
        r"(?m)^n_concurrent_trials:.*$": f"n_concurrent_trials: {concurrency}",
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
    rendered, _agent_concurrency_count = re.subn(
        r"(?m)^(\s+)n_concurrent:(?!_trials)\s*.*$",
        rf"\1n_concurrent: {concurrency}",
        rendered,
        count=1,
    )
    if _agent_concurrency_count == 0:
        rendered, _injected = re.subn(
            r"(?m)^(\s+)(import_path:\s*harbor_ags\.agent:LosslessHermesAgent\s*)$",
            rf"\1\2\n\1n_concurrent: {concurrency}",
            rendered,
            count=1,
        )
    if agent_mode == "hermes":
        rendered = _bind_agent_budgets(rendered, agent_max_iterations, timeout_seconds)
    rendered = _bind_environment_timeouts(rendered, timeout_seconds)
    return _rewrite_extra_instruction_paths(rendered, harbor_root)


def _bundle_files(root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        files.append({
            "path": relative,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    return files


def _rebind_compile_manifest(
    source: Path,
    destination: Path,
    *,
    source_task_relative: str,
) -> None:
    """导出单任务 bundle 时重绑定编译清单中的实际 task 路径。"""

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborRolloutError("compile-manifest.json 无法读取") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        raise HarborRolloutError("compile-manifest.json 契约无效")
    candidates = [
        item
        for item in payload["tasks"]
        if isinstance(item, dict) and item.get("task_dir") == source_task_relative
    ]
    if len(candidates) != 1:
        raise HarborRolloutError("compile-manifest 未绑定导出的 task")
    task = dict(candidates[0])
    task["task_dir"] = "task"
    payload["tasks"] = [task]
    payload["n_trials"] = 1
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def publish_rollout_bundle(plan_dir: Path | str, destination: Path | str) -> Path:
    """原子发布计划实际使用的任务输入；只保存输入，不宣称执行通过。

    保留任务的 workspace 快照 hook，并核对已冻结的计划与内容哈希。
    目标目录存在时拒绝覆盖，防止将旧输入误认成当前执行的输入。
    """
    plan_root = Path(plan_dir).resolve()
    destination_path = Path(destination)
    if not plan_root.is_dir():
        raise HarborRolloutError(f"rollout plan 不存在: {plan_root}")
    plan = load_verified_rollout_plan(plan_root)
    dataset = plan.get("dataset")
    paths = dataset.get("task_relative_paths") if isinstance(dataset, dict) else None
    if not isinstance(paths, list) or not paths or not all(isinstance(x, str) for x in paths):
        raise HarborRolloutError("rollout plan 缺少 dataset task_relative_paths")
    dataset_root = Path(dataset["dataset_root"]).resolve()
    source = (dataset_root / paths[0]).resolve()
    if len(set(dataset["task_hashes"].values())) != 1:
        raise HarborRolloutError("单任务 Harbor bundle 不能导出内容不同的多 trial 输入")
    try:
        source.relative_to(dataset_root)
    except ValueError as exc:
        raise HarborRolloutError("dataset task path 越界") from exc
    validate_bundle_layout(source)
    if destination_path.exists():
        raise HarborRolloutError(f"Harbor bundle 目标已存在，拒绝覆盖: {destination_path}")
    workspace = ArtifactWorkspace(destination_path.parent, destination_path.name)
    try:
        task_target = workspace.staging_path / "task"
        shutil.copytree(source, task_target, symlinks=False)
        validate_bundle_layout(task_target)
        shutil.copy2(dataset_root / "dataset.toml", workspace.staging_path / "dataset.toml")
        compile_manifest = dataset_root / "compile-manifest.json"
        if compile_manifest.is_file():
            _rebind_compile_manifest(
                compile_manifest,
                workspace.staging_path / "compile-manifest.json",
                source_task_relative=paths[0],
            )
        if _sha256_tree(task_target) != dataset["task_hashes"][paths[0]]:
            raise HarborRolloutError("Harbor bundle 复制后的 task hash 不匹配")
        files = _bundle_files(workspace.staging_path)
        manifest = {
            "schema_version": "traceforge.harbor-bundle-manifest.v1",
            "kind": "ROLLOUT_INPUT",
            "source_plan": str(plan_root),
            "source_plan_sha256": _sha256_file(plan_root / "rollout_plan.json"),
            "source_run_id": plan.get("run_id"),
            "task_name": plan.get("task_name"),
            "task_path": "task",
            "dataset_trial_count": (
                dataset.get("trial_count") if isinstance(dataset, dict) else None
            ),
            "files": files,
            "content_sha256": hashlib.sha256(
                json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "execution_status": "NOT_ASSERTED",
        }
        (workspace.staging_path / "artifact_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return workspace.publish()
    except BaseException:
        workspace.abort()
        raise

def _reconstruction_rollout_gate(task_dir: Path) -> dict[str, Any]:
    """若 bundle 来自重建产物，执行前强制通过 TaskFit。"""

    resolved = task_dir.resolve()
    for ancestor in (resolved, *resolved.parents):
        if ancestor.name != "tasks" or not (ancestor.parent / "reconstruction_manifest.json").is_file():
            continue
        relative = resolved.relative_to(ancestor)
        if not relative.parts:
            break
        task_id = relative.parts[0]
        task_root = ancestor / task_id
        fit_path = task_root / "task_fit.json"
        try:
            fit = json.loads(fit_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HarborRolloutError("rollout gate 缺少有效 task_fit.json") from exc
        if not isinstance(fit, dict):
            raise HarborRolloutError("rollout gate 的 task_fit 必须是对象")
        decision = fit.get("decision")
        if isinstance(decision, str):
            decision = decision.strip().upper()
        errors = [str(item) for item in (fit.get("errors") or [])]
        review_allowed = decision == "REVIEW_TASK_FIT" and fit.get("execution_policy") == "PROCEED_ORIGINAL"
        if not review_allowed and (errors or decision not in {"READY_ORIGINAL", "INCOMPATIBLE"}):
            raise HarborRolloutError(
                f"rollout gate 未通过 TaskFit: decision={decision or 'MISSING'}; errors={errors}"
            )
        if decision == "INCOMPATIBLE" and fit.get("variant_eligible") is not True:
            raise HarborRolloutError("rollout gate 的 INCOMPATIBLE TaskFit 未获得变体资格")
        return {
            "status": "PASS_WITH_REVIEW" if review_allowed else "PASS",
            "source_root": str(ancestor.parent),
            "task_id": task_id,
            "task_fit_decision": decision,
        }
    return {"status": "NOT_APPLICABLE"}


def build_rollout_plan(config: HarborRolloutConfig) -> Path:
    """校验 Bundle 并发布可执行计划；不启动模型。"""

    config.validate()
    task_dir = config.task_dir.resolve()
    harbor_root = config.harbor_root.resolve()
    reconstruction_gate = _reconstruction_rollout_gate(task_dir)
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
            "agent_mode": config.agent_mode,
            "model": config.model,
            "trials": config.trials,
            "concurrency": config.concurrency,
            "timeout_seconds": config.timeout_seconds,
            "agent_max_iterations": config.agent_max_iterations,
            "expected_hermes_commit": config.expected_hermes_commit,
        },
    )
    workspace = ArtifactWorkspace(config.output_root.resolve(), run_id)
    try:
        _dataset_staging_root, dataset_meta = _materialize_dataset(
            task_dir,
            workspace.staging_path / "dataset",
            trials=config.trials,
            harbor_root=harbor_root,
        )
        dataset_root = workspace.final_path / "dataset"
        dataset_meta["dataset_root"] = str(dataset_root)
        jobs_root = config.jobs_root.resolve()
        jobs_root.mkdir(parents=True, exist_ok=True)
        rendered_config = _render_harbor_config(
            source=harbor_config,
            harbor_root=harbor_root,
            jobs_root=jobs_root,
            job_name=run_id,
            agent_mode=config.agent_mode,
            model=config.model,
            concurrency=config.concurrency,
            timeout_seconds=config.timeout_seconds,
            agent_max_iterations=config.agent_max_iterations,
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
            "job_name": run_id,
            "status": "READY",
            "task_name": task_name,
            "source_bundle": str(task_dir),
            "bundle_sha256": bundle_digest,
            "dataset": dataset_meta,
            "command": command,
            "harbor_root": str(harbor_root),
            "harbor_runtime": _harbor_runtime_metadata(harbor_root),
            "jobs_root": str(jobs_root),
            "agent": {
                "mode": config.agent_mode,
                "model": config.model,
                "trials": config.trials,
                "concurrency": config.concurrency,
                "timeout_seconds": config.timeout_seconds,
                "max_iterations": config.agent_max_iterations,
                "expected_hermes_commit": config.expected_hermes_commit,
            },
            "timeouts": {
                "agent_timeout_seconds": config.timeout_seconds,
                "job_timeout_seconds": _job_timeout_seconds(
                    config.trials, config.concurrency, config.timeout_seconds
                ),
            },
            "reconstruction_gate": reconstruction_gate,
            "verifier": {
                "environment_mode": "separate",
                "network_mode": "no-network",
                "artifact_manifest_required": config.agent_mode == "hermes",
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


def _resolve_existing_path(value: str) -> str:
    path = Path(value)
    if path.exists():
        return str(path.resolve())
    return value


def _normalize_command(command: list[str]) -> list[str]:
    return [_resolve_existing_path(item) for item in command]


def _assert_plan_integrity(plan_dir: Path, plan: dict[str, Any]) -> None:
    plan_path = plan_dir / "rollout_plan.json"
    manifest_path = plan_dir / "artifact_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborRolloutError("artifact_manifest.json 无法读取") from exc
    if not isinstance(manifest, dict):
        raise HarborRolloutError("artifact manifest 根节点必须是对象")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise HarborRolloutError("artifact manifest 缺少 files")
    entry = next(
        (
            item
            for item in files
            if isinstance(item, dict) and item.get("relative_path") == "rollout_plan.json"
        ),
        None,
    )
    if entry is None:
        raise HarborRolloutError("artifact manifest 缺少 rollout_plan.json")
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    if digest != entry.get("sha256"):
        raise HarborRolloutError("rollout plan hash 与 artifact manifest 不一致")
    harbor_root = plan.get("harbor_root")
    if not isinstance(harbor_root, str) or not harbor_root.strip():
        raise HarborRolloutError("rollout plan 缺少 harbor_root")
    root = Path(harbor_root).resolve()
    if not root.is_dir():
        raise HarborRolloutError(f"harbor_root 不存在：{root}")
    config_path = (plan_dir / "harbor-config.yaml").resolve()
    dataset_meta = plan.get("dataset")
    dataset_root = dataset_meta.get("dataset_root") if isinstance(dataset_meta, dict) else None
    if not isinstance(dataset_root, str):
        raise HarborRolloutError("rollout plan 缺少 dataset_root")
    dataset = Path(dataset_root).resolve()
    try:
        dataset.relative_to(plan_dir.resolve())
    except ValueError as exc:
        raise HarborRolloutError("dataset 必须位于当前 rollout plan 目录内") from exc
    task_paths = dataset_meta.get("task_relative_paths")
    task_hashes = dataset_meta.get("task_hashes")
    if not isinstance(task_paths, list) or not task_paths or not isinstance(task_hashes, dict):
        raise HarborRolloutError("rollout plan dataset 缺少 task paths/hashes")
    for relative in task_paths:
        if not isinstance(relative, str):
            raise HarborRolloutError("dataset task path 非法")
        target = (dataset / relative).resolve()
        try:
            target.relative_to(dataset)
        except ValueError as exc:
            raise HarborRolloutError("dataset task path 越界") from exc
        if not target.is_dir() or task_hashes.get(relative) != _sha256_tree(target):
            raise HarborRolloutError(f"dataset task hash 不匹配：{relative}")
    dataset_toml = dataset / "dataset.toml"
    if (
        not dataset_toml.is_file()
        or dataset_meta.get("dataset_toml_sha256") != _sha256_file(dataset_toml)
    ):
        raise HarborRolloutError("dataset.toml hash 与 plan 不一致")
    # compile-manifest was added after the first rollout plans were published.
    # Keep those immutable historical plans executable; new plans bind the file
    # and must still fail closed on a missing or mismatched manifest.
    compile_hash = dataset_meta.get("compile_manifest_sha256")
    if compile_hash is not None:
        compile_manifest = dataset / "compile-manifest.json"
        if (
            not compile_manifest.is_file()
            or not isinstance(compile_hash, str)
            or compile_hash != _sha256_file(compile_manifest)
        ):
            raise HarborRolloutError("compile-manifest.json hash 与 plan 不一致")
    materialized = plan.get("harbor_config")
    published = (
        Path(materialized["materialized"]).resolve()
        if isinstance(materialized, dict) and isinstance(materialized.get("materialized"), str)
        else None
    )
    if published != config_path:
        raise HarborRolloutError("harbor-config 路径与本 plan 目录不一致")
    if not config_path.is_file() or not dataset.is_dir():
        raise HarborRolloutError("本 plan 的 harbor-config 或 dataset 缺失")
    expected_hash = materialized.get("sha256") if isinstance(materialized, dict) else None
    if expected_hash != hashlib.sha256(config_path.read_bytes()).hexdigest():
        raise HarborRolloutError("harbor-config hash 与 plan 不一致")
    command = plan.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise HarborRolloutError("rollout plan command 非法")
    expected = [*_harbor_command(root), "run", "-c", str(config_path), "-p", str(dataset)]
    if _normalize_command(command) != _normalize_command(expected):
        raise HarborRolloutError("rollout plan command 未绑定到本 plan 的 harbor run")


def load_verified_rollout_plan(plan_dir: Path | str) -> dict[str, Any]:
    """读取并核对冻结计划、配置与 dataset 内容，不执行 Harbor。"""

    plan_root = Path(plan_dir).resolve()
    try:
        plan = json.loads((plan_root / "rollout_plan.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborRolloutError("rollout_plan.json 无法读取") from exc
    if not isinstance(plan, dict) or plan.get("schema_version") != ROLLOUT_BRIDGE_SCHEMA:
        raise HarborRolloutError("rollout plan schema 不匹配")
    _assert_plan_integrity(plan_root, plan)
    return plan


def _update_run_receipt(
    plan_dir: Path, *, status: str, returncode: int | None = None, error: str = ""
) -> None:
    """原子更新真实执行状态，保留不可变计划及其清单绑定。"""

    path = plan_dir / "run_receipt.json"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborRolloutError("run_receipt.json 无法读取") from exc
    if not isinstance(receipt, dict):
        raise HarborRolloutError("run_receipt.json 根节点必须是对象")
    receipt.update(
        status=status,
        external_execution=True,
        returncode=returncode,
        error=_redact_output(error)[-2000:],
    )
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def execute_rollout_plan(
    plan_dir: Path,
    *,
    timeout_seconds: int | None = None,
    config_path: Path | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """显式执行已发布计划；执行结果只保留有界 stdout/stderr 摘要。"""

    plan = load_verified_rollout_plan(plan_dir)
    runtime = plan.get("harbor_runtime")
    if isinstance(runtime, dict):
        runtime_files = runtime.get("files")
        if isinstance(runtime_files, dict):
            harbor_root = Path(str(plan.get("harbor_root"))).resolve()
            for relative, expected_hash in runtime_files.items():
                if not isinstance(relative, str) or not isinstance(expected_hash, str):
                    continue
                runtime_path = harbor_root / relative
                if not runtime_path.is_file() or _sha256_file(runtime_path) != expected_hash:
                    raise HarborRolloutError(
                        f"Harbor runtime changed after plan creation: {relative}"
                    )
    command = plan["command"]
    if timeout_seconds is None:
        timeouts = plan.get("timeouts")
        timeout_seconds = (
            int(timeouts["job_timeout_seconds"])
            if isinstance(timeouts, dict) and isinstance(timeouts.get("job_timeout_seconds"), int)
            else int((plan.get("agent") or {}).get("timeout_seconds", 900))
        )
    if timeout_seconds < 1:
        raise HarborRolloutError("执行 timeout_seconds 必须大于 0")
    agent = plan.get("agent")
    mode = agent.get("mode") if isinstance(agent, dict) else "hermes"
    reviewed_timeout, reviewed_iterations = _reviewed_rollout_budget(config_path)
    if isinstance(agent, dict):
        planned_timeout = agent.get("timeout_seconds")
        planned_iterations = agent.get("max_iterations")
        if (
            reviewed_timeout is not None
            and type(planned_timeout) is int
            and planned_timeout < reviewed_timeout
        ):
            raise HarborRolloutError(
                "rollout plan timeout_seconds is below the reviewed config budget "
                f"{reviewed_timeout}"
            )
        if (
            reviewed_iterations is not None
            and type(planned_iterations) is int
            and planned_iterations < reviewed_iterations
        ):
            raise HarborRolloutError(
                "rollout plan max_iterations is below the reviewed config budget "
                f"{reviewed_iterations}"
            )
    env = _prepare_execution_env(str(mode), config_path=config_path, channel=channel)
    missing = _missing_execution_credentials(env, str(mode))
    if missing:
        raise HarborRolloutError(f"执行 Harbor 前缺少凭据环境变量：{', '.join(missing)}")
    _update_run_receipt(plan_dir, status="EXECUTING")
    try:
        result = subprocess.run(
            command,
            cwd=str(Path(str(plan.get("harbor_root"))).resolve()),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            # 让 Harbor job 脱离启动它的 SSH/交互进程组；长 Hermes
            # trial 不应因外层会话断开而收到 SIGTERM。
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        _update_run_receipt(plan_dir, status="TIMEOUT", error=str(exc))
        return {
            "status": "TIMEOUT",
            "returncode": None,
            "stdout": "",
            "stderr": _redact_output(str(exc))[-2000:],
        }
    except BaseException as exc:
        _update_run_receipt(
            plan_dir,
            status="ABORTED",
            error=f"{type(exc).__name__}: {_redact_output(str(exc))[-1800:]}",
        )
        raise
    status = "COMPLETED" if result.returncode == 0 else "FAILED"
    _update_run_receipt(
        plan_dir,
        status=status,
        returncode=result.returncode,
        error=result.stderr if result.returncode else "",
    )
    return {
        "status": status,
        "returncode": result.returncode,
        "stdout": _redact_output(result.stdout[-4000:]),
        "stderr": _redact_output(result.stderr[-4000:]),
    }


__all__ = [
    "DEFAULT_RUNTIME_CONFIG",
    "ROLLOUT_BRIDGE_SCHEMA",
    "HarborRolloutConfig",
    "HarborRolloutError",
    "build_rollout_plan",
    "execute_rollout_plan",
    "load_verified_rollout_plan",
    "publish_rollout_bundle",
]
