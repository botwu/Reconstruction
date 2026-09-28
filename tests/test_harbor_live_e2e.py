"""Harbor 主路径活跑：prepare-rollout → harbor run → quality gate。

默认跳过。设置 ``HARBOR_LIVE_E2E=1`` 后才会访问北京 AGS。
覆盖 oracle / nop / hermes，以及审计后的 n_concurrent、job_name 绑定和分 mode 门禁。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from traceforge.harbor_ags.results import certify_hermes_job, read_rollout_results
from traceforge.harbor_ags.rollout import (
    DEFAULT_RUNTIME_CONFIG,
    HarborRolloutConfig,
    build_rollout_plan,
    execute_rollout_plan,
)
from traceforge.reconstruction.model_gateway import load_channel_connection, load_e2b_api_key

pytestmark = pytest.mark.live

HARBOR_ROOT = Path("/mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags")
SMOKE_TASK = HARBOR_ROOT / "datasets/smoke/sandbox-hermes-smoke"
LOCKED_HERMES_COMMIT = "3c231eb3979ab9c57d5cd6d02f1d577a3b718b43"


def _require_live() -> None:
    if os.environ.get("HARBOR_LIVE_E2E", "").strip() != "1":
        pytest.skip("设置 HARBOR_LIVE_E2E=1 才运行 Harbor/AGS 活跑")


def _secret_values() -> tuple[str, ...]:
    values = {
        os.environ[name].strip()
        for name in (
            "ANTHROPIC_API_KEY",
            "TOKENHUB_KEY",
            "ROLLOUT_LLM_API_KEY",
            "AGS_API_KEY",
            "E2B_API_KEY",
            "ROLLOUT_E2B_API_KEY",
        )
        if os.environ.get(name, "").strip()
    }
    config = Path(DEFAULT_RUNTIME_CONFIG)
    if config.is_file():
        try:
            sandbox_key = load_e2b_api_key(config)
        except Exception:
            sandbox_key = None
        if sandbox_key:
            values.add(sandbox_key)
        try:
            _url, channel_key = load_channel_connection(config, "claude")
        except Exception:
            channel_key = None
        if channel_key:
            values.add(channel_key)
    return tuple(item for item in values if len(item) >= 8)


def _assert_no_secrets(root: Path, secrets: tuple[str, ...]) -> None:
    skip_suffixes = {".whl", ".png", ".jpg", ".pyc"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() in skip_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for secret in secrets:
            if secret in text:
                raise AssertionError(f"密钥泄漏到 {path.relative_to(root)}")


def _compile_smoke_task(root: Path) -> Path:
    destination = root / "compiled-dataset"
    if destination.exists():
        shutil.rmtree(destination)
    compiled = subprocess.run(
        [
            str(HARBOR_ROOT / ".venv/bin/harbor-ags"),
            "compile-smoke",
            "--output",
            str(destination),
            "--n-trials",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if compiled.returncode != 0:
        raise AssertionError(
            f"compile-smoke 失败：{compiled.stderr[-1000:] or compiled.stdout[-1000:]}"
        )
    tasks = [
        path
        for path in destination.iterdir()
        if path.is_dir() and (path / "task.toml").is_file()
    ]
    if len(tasks) != 1:
        raise AssertionError(f"compile-smoke 应产出 1 个 task，实际 {len(tasks)}")
    return tasks[0]


def _prepare(mode: str, output_root: Path, jobs_root: Path, task_dir: Path) -> Path:
    return build_rollout_plan(
        HarborRolloutConfig(
            task_dir=task_dir,
            harbor_root=HARBOR_ROOT,
            output_root=output_root,
            jobs_root=jobs_root,
            agent_mode=mode,
            model="anthropic/claude-opus-4-8",
            trials=1,
            concurrency=1,
            timeout_seconds=900,
            expected_hermes_commit=LOCKED_HERMES_COMMIT,
        )
    )


def _execute_and_read(plan_dir: Path, *, mode: str) -> dict[str, object]:
    execution = execute_rollout_plan(
        plan_dir,
        timeout_seconds=900,
        config_path=DEFAULT_RUNTIME_CONFIG,
        channel="claude",
    )
    plan = json.loads((plan_dir / "rollout_plan.json").read_text(encoding="utf-8"))
    job_name = str(plan["job_name"])
    job_dir = Path(str(plan["jobs_root"])) / job_name
    if mode == "hermes" and job_dir.is_dir():
        certify_hermes_job(job_dir, harbor_root=HARBOR_ROOT)
    results = None
    if job_dir.is_dir():
        results = read_rollout_results(job_dir, agent_mode=mode, expected_trial_count=1)
    return {"execution": execution, "plan": plan, "job_dir": job_dir, "results": results}


def test_live_harbor_cli_rollout_oracle_nop_hermes(tmp_path: Path) -> None:
    _require_live()
    if not SMOKE_TASK.is_dir():
        pytest.skip("缺少 harbor_ags smoke Task Bundle")
    if not (HARBOR_ROOT / ".venv/bin/harbor").is_file():
        pytest.skip("缺少 Harbor 可执行文件")
    secrets = _secret_values()
    if not secrets:
        pytest.skip("缺少 AGS / TokenHub 凭据")

    root = Path(os.environ.get("HARBOR_LIVE_E2E_ROOT", str(tmp_path))).resolve()
    root.mkdir(parents=True, exist_ok=True)

    print("e2e: compiling smoke dataset", flush=True)
    task_dir = _compile_smoke_task(root)
    hermes_plan = _prepare("hermes", root / "plans/hermes", root / "jobs/hermes", task_dir)
    rendered = (hermes_plan / "harbor-config.yaml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^n_concurrent_trials:\s*1\s*$", rendered)
    assert re.search(r"(?m)^\s+n_concurrent:\s*1\s*$", rendered)
    assert "n_concurrent: 8" not in rendered
    hermes_meta = json.loads((hermes_plan / "rollout_plan.json").read_text(encoding="utf-8"))
    assert hermes_meta["job_name"] == hermes_meta["run_id"]
    assert hermes_meta["verifier"]["artifact_manifest_required"] is True

    oracle_plan = _prepare("oracle", root / "plans/oracle", root / "jobs/oracle", task_dir)
    nop_plan = _prepare("nop", root / "plans/nop", root / "jobs/nop", task_dir)
    oracle_meta = json.loads((oracle_plan / "rollout_plan.json").read_text(encoding="utf-8"))
    assert oracle_meta["verifier"]["artifact_manifest_required"] is False

    print("e2e: executing oracle via harbor run", flush=True)
    oracle = _execute_and_read(oracle_plan, mode="oracle")
    assert oracle["execution"]["status"] == "COMPLETED"
    assert oracle["results"] is not None
    assert oracle["results"]["quality_gate"]["ok"] is True
    assert oracle["results"]["trials"][0]["status"] == "PASS"
    assert oracle["results"]["trials"][0]["reward"] == 1.0

    print("e2e: executing nop via harbor run", flush=True)
    nop = _execute_and_read(nop_plan, mode="nop")
    assert nop["execution"]["status"] == "COMPLETED"
    assert nop["results"] is not None
    assert nop["results"]["quality_gate"]["ok"] is True
    assert nop["results"]["trials"][0]["status"] == "FAIL"
    assert nop["results"]["trials"][0]["reward"] == 0.0

    print("e2e: executing hermes via harbor run", flush=True)
    hermes = _execute_and_read(hermes_plan, mode="hermes")
    assert hermes["execution"]["status"] == "COMPLETED"
    assert hermes["results"] is not None
    assert hermes["results"]["quality_gate"]["ok"] is True, hermes["results"]["quality_gate"]
    assert hermes["results"]["trials"][0]["status"] == "PASS"
    assert hermes["results"]["trials"][0]["reward"] == 1.0
    assert hermes["results"]["trials"][0]["trajectory_present"] is True
    assert hermes["results"]["trials"][0]["artifact_manifest_present"] is True

    _assert_no_secrets(root, secrets)
    report = {
        "ok": True,
        "oracle": {
            "job_dir": str(oracle["job_dir"]),
            "status": oracle["results"]["trials"][0]["status"],
        },
        "nop": {
            "job_dir": str(nop["job_dir"]),
            "status": nop["results"]["trials"][0]["status"],
        },
        "hermes": {
            "job_dir": str(hermes["job_dir"]),
            "status": hermes["results"]["trials"][0]["status"],
            "quality_gate": hermes["results"]["quality_gate"],
        },
    }
    (root / "e2e-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def test_completion_can_write_below_uploaded_project_directory(tmp_path: Path) -> None:
    """真实验证 root 上传后的普通用户写入，同时保留原始正文保护。"""
    _require_live()
    from traceforge.reconstruction.agents.roles import COMPLETION_ROLE
    from traceforge.reconstruction.agents.sandbox import prepare_role_sandbox, run_coro
    from traceforge.reconstruction.agents.session import AgentSession, execute_tool
    from traceforge.reconstruction.container_verification import build_ags_runtime_factory

    workspace = tmp_path / "workspace"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg/input.txt").write_text("observed bytes\n")
    session = AgentSession(
        workspace=workspace, allow_write=True, protected_paths={"pkg/input.txt"},
        evidence=[{"evidence_ref_id": "read-1"}],
    )
    runtime = build_ags_runtime_factory(
        harbor_root=HARBOR_ROOT, output_root=tmp_path / "ags",
        config_path=DEFAULT_RUNTIME_CONFIG,
    )()
    try:
        run_coro(prepare_role_sandbox(
            role=COMPLETION_ROLE, runtime=runtime, session=session,
            staging_root=tmp_path / "staging",
        ))
        permissions = run_coro(runtime.exec(
            "id -un && stat -c '%U %a' /home/user/workspace/pkg",
            cwd="/", user="user",
        ))
        written = execute_tool("write_file", {
            "path": "pkg/new.txt", "content": "grounded context\n",
            "evidence_ref_ids": ["read-1"],
        }, session)
        assert written.startswith("wrote"), (written, permissions.stdout)
        assert execute_tool("read_file", {"path": "pkg/new.txt"}, session) == "grounded context\n"
        blocked = execute_tool("write_file", {
            "path": "pkg/input.txt", "content": "changed",
            "evidence_ref_ids": ["read-1"],
        }, session)
        assert blocked.startswith("error:"), blocked
        assert execute_tool("read_file", {"path": "pkg/input.txt"}, session) == "observed bytes\n"
    finally:
        if session.sandbox_started:
            run_coro(runtime.stop(delete=True))
