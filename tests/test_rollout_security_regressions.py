from __future__ import annotations

import json
import subprocess
from pathlib import Path

from traceforge.harbor_ags.rollout import HarborRolloutConfig, build_rollout_plan, execute_rollout_plan


def _bundle(root: Path) -> Path:
    for path in (root / "workspace", root / "environment", root / "solution", root / "tests/control"):
        path.mkdir(parents=True)
    (root / "workspace/input.txt").write_text("x", encoding="utf-8")
    (root / "task.toml").write_text(
        'schema_version="1.4"\n[task]\nname="traceforge/security"\n[verifier]\nenvironment_mode="separate"\n[verifier.environment]\nnetwork_mode="no-network"\n',
        encoding="utf-8",
    )
    (root / "instruction.md").write_text("x\n", encoding="utf-8")
    (root / "tests/grader.py").write_text("", encoding="utf-8")
    (root / "tests/rubric.json").write_text("{}\n", encoding="utf-8")
    (root / "tests/test.sh").write_text("#!/bin/sh\nset -eu\n\npython3 /tests/grader.py\n", encoding="utf-8")
    return root


def _harbor(root: Path) -> Path:
    (root / "configs").mkdir(parents=True)
    config = """jobs_dir: runs
n_concurrent_trials: 1
agents:
  - import_path: harbor_ags.agent:LosslessHermesAgent
    model_name: anthropic/claude-opus-4-8
    n_concurrent: 1
environment:
  import_path: harbor_ags.environment:AGSPrebuiltEnvironment
  kwargs:
    sandbox_timeout_sec: 900
"""
    for name in ("hermes-batch.yaml", "oracle.yaml", "nop.yaml"):
        (root / "configs" / name).write_text(config, encoding="utf-8")
    exe = root / ".venv/bin/harbor"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    return root


def test_rollout_run_id_binds_agent_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGS_API_KEY", "ags")
    task = _bundle(tmp_path / "task")
    harbor = _harbor(tmp_path / "harbor")
    hermes = build_rollout_plan(HarborRolloutConfig(task, harbor, tmp_path / "p1", tmp_path / "j1"))
    oracle = build_rollout_plan(HarborRolloutConfig(task, harbor, tmp_path / "p2", tmp_path / "j2", agent_mode="oracle"))
    assert json.loads((hermes / "rollout_plan.json").read_text())["run_id"] != json.loads((oracle / "rollout_plan.json").read_text())["run_id"]


def test_rollout_execution_preserves_original_output(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGS_API_KEY", "ags")
    monkeypatch.setenv("TOKENHUB_KEY", "token")
    task = _bundle(tmp_path / "task")
    harbor = _harbor(tmp_path / "harbor")
    plan = build_rollout_plan(HarborRolloutConfig(task, harbor, tmp_path / "plans", tmp_path / "jobs"))

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "Authorization: Bearer sk-super-secret-value\n", "api_key=sk-another-secret\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = execute_rollout_plan(plan)
    assert result["stdout"] == "Authorization: Bearer sk-super-secret-value\n"
    assert result["stderr"] == "api_key=sk-another-secret\n"
