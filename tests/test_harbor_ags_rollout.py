"""Harbor/AGS rollout 桥接测试。"""

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from traceforge.harbor_ags.rollout import (
    HarborRolloutConfig,
    HarborRolloutError,
    build_rollout_plan,
    execute_rollout_plan,
)


def _bundle(root: Path) -> Path:
    for path in (
        root / "workspace",
        root / "environment",
        root / "solution",
        root / "tests/control",
    ):
        path.mkdir(parents=True)
    (root / "workspace/input.txt").write_text("public\n")
    (root / "environment/README").write_text("runner-only\n")
    (root / "solution/solve.sh").write_text("#!/bin/sh\n")
    (root / "tests/grader.py").write_text("print('ok')\n")
    (root / "tests/rubric.json").write_text("{}\n")
    (root / "tests/control/truth.txt").write_text("hidden\n")
    (root / "tests/test.sh").write_text("#!/bin/sh\nset -eu\n\npython3 /tests/grader.py\n")
    (root / "task.toml").write_text(
        'schema_version = "1.4"\n'
        '[task]\nname = "traceforge/test"\n'
        '[verifier]\nenvironment_mode = "separate"\n'
        '[verifier.environment]\nnetwork_mode = "no-network"\n'
    )
    (root / "instruction.md").write_text("Do the task.\n")
    return root


def _harbor_root(root: Path) -> Path:
    (root / "configs").mkdir(parents=True)
    config = """jobs_dir: runs
n_concurrent_trials: 1
agents:
  - import_path: harbor_ags.agent:LosslessHermesAgent
    model_name: anthropic/claude-opus-4-8
    kwargs:
      expected_commit: abc
environment:
  import_path: harbor_ags.environment:AGSPrebuiltEnvironment
  kwargs:
    sandbox_timeout_sec: 900
"""
    for name in ("hermes-batch.yaml", "oracle.yaml", "nop.yaml"):
        (root / "configs" / name).write_text(config)
    executable = root / ".venv/bin/harbor"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    return root


def test_prepare_rollout_materializes_dataset_without_executing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGS_API_KEY", "ags-secret-value")
    monkeypatch.setenv("TOKENHUB_KEY", "tokenhub-secret-value")
    output = build_rollout_plan(
        HarborRolloutConfig(
            task_dir=_bundle(tmp_path / "task"),
            harbor_root=_harbor_root(tmp_path / "harbor"),
            output_root=tmp_path / "plans",
            jobs_root=tmp_path / "jobs",
            trials=2,
            concurrency=2,
        )
    )
    plan_text = (output / "rollout_plan.json").read_text()
    plan = json.loads(plan_text)
    assert plan["external_execution"] is False
    assert plan["verifier"]["environment_mode"] == "separate"
    assert plan["verifier"]["network_mode"] == "no-network"
    assert plan["agent"]["mode"] == "hermes"
    config_text = (output / "harbor-config.yaml").read_text()
    assert "n_concurrent_trials: 2" in config_text
    assert 'model_name: "anthropic/claude-opus-4-8"' in config_text
    assert Path(plan["dataset"]["dataset_root"]).is_dir()
    assert Path(plan["dataset"]["dataset_root"], "dataset.toml").is_file()
    generated_task = Path(plan["dataset"]["dataset_root"]) / plan["dataset"][
        "task_relative_paths"
    ][0]
    task_text = (generated_task / "task.toml").read_text()
    assert tomllib.loads(task_text)["verifier"]["collect"][0]["service"] == "main"
    assert "TraceForge workspace snapshot hook" in task_text
    assert "/home/user/workspace" in task_text
    assert "/logs/artifacts/traceforge/workspace" in task_text
    assert "ags-secret-value" not in plan_text
    assert "tokenhub-secret-value" not in plan_text


def test_execute_rollout_requires_credentials(tmp_path: Path) -> None:
    output = build_rollout_plan(
        HarborRolloutConfig(
            task_dir=_bundle(tmp_path / "task"),
            harbor_root=_harbor_root(tmp_path / "harbor"),
            output_root=tmp_path / "plans",
            jobs_root=tmp_path / "jobs",
        )
    )
    old_ags = os.environ.pop("AGS_API_KEY", None)
    old_tokenhub = os.environ.pop("TOKENHUB_KEY", None)
    old_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        with pytest.raises(HarborRolloutError, match="缺少凭据"):
            execute_rollout_plan(output)
    finally:
        for name, value in (
            ("AGS_API_KEY", old_ags),
            ("TOKENHUB_KEY", old_tokenhub),
            ("ANTHROPIC_API_KEY", old_anthropic),
        ):
            if value is not None:
                os.environ[name] = value


def test_execute_rollout_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = build_rollout_plan(
        HarborRolloutConfig(
            task_dir=_bundle(tmp_path / "task"),
            harbor_root=_harbor_root(tmp_path / "harbor"),
            output_root=tmp_path / "plans",
            jobs_root=tmp_path / "jobs",
        )
    )
    monkeypatch.setenv("AGS_API_KEY", "ags-secret")
    monkeypatch.setenv("TOKENHUB_KEY", "llm-secret")
    observed: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = execute_rollout_plan(output)
    assert result["status"] == "COMPLETED"
    assert len(observed) == 1
    assert observed[0][1] == "run"


def test_oracle_mode_does_not_require_tokenhub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = build_rollout_plan(
        HarborRolloutConfig(
            task_dir=_bundle(tmp_path / "task"),
            harbor_root=_harbor_root(tmp_path / "harbor"),
            output_root=tmp_path / "plans",
            jobs_root=tmp_path / "jobs",
            agent_mode="oracle",
        )
    )
    monkeypatch.setenv("AGS_API_KEY", "ags-secret")
    monkeypatch.delenv("TOKENHUB_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert execute_rollout_plan(output)["status"] == "COMPLETED"
