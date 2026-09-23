"""Harbor/AGS rollout 桥接测试。"""

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from traceforge.harbor_ags.rollout import (
    HarborRolloutConfig,
    HarborRolloutError,
    build_rollout_plan,
    execute_rollout_plan,
    publish_rollout_bundle,
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
    n_concurrent: 8
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
    assert "n_concurrent: 2" in config_text
    assert "n_concurrent: 8" not in config_text
    assert "max_iterations: 30" in config_text
    assert f"job_name: {json.dumps(plan['job_name'])}" in config_text
    assert plan["job_name"] == plan["run_id"]
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



def test_publish_rollout_bundle_is_harbor_visible_and_immutable(tmp_path: Path) -> None:
    plan = build_rollout_plan(
        HarborRolloutConfig(
            task_dir=_bundle(tmp_path / "task"),
            harbor_root=_harbor_root(tmp_path / "harbor"),
            output_root=tmp_path / "plans",
            jobs_root=tmp_path / "jobs",
            trials=2,
        )
    )
    published = publish_rollout_bundle(plan, tmp_path / "harbor_bundle")
    assert published == tmp_path / "harbor_bundle"
    for relative in (
        "task/task.toml",
        "task/instruction.md",
        "task/workspace/input.txt",
        "task/environment/README",
        "task/solution/solve.sh",
        "task/tests/control/truth.txt",
        "task/tests/grader.py",
        "dataset.toml",
        "artifact_manifest.json",
    ):
        assert (published / relative).is_file(), relative
    assert "TraceForge workspace snapshot hook" in (published / "task/task.toml").read_text()
    manifest = json.loads((published / "artifact_manifest.json").read_text())
    assert manifest["kind"] == "ROLLOUT_INPUT"
    assert manifest["execution_status"] == "NOT_ASSERTED"
    assert manifest["dataset_trial_count"] == 2
    with pytest.raises(HarborRolloutError, match="已存在"):
        publish_rollout_bundle(plan, tmp_path / "harbor_bundle")

def test_execute_rollout_requires_credentials(
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
    for name in (
        "AGS_API_KEY",
        "E2B_API_KEY",
        "ROLLOUT_E2B_API_KEY",
        "TOKENHUB_KEY",
        "ANTHROPIC_API_KEY",
        "ROLLOUT_LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(HarborRolloutError, match="缺少凭据"):
        execute_rollout_plan(output)


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


def test_execute_rollout_rejects_unbound_command(
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
    plan_path = output / "rollout_plan.json"
    plan = json.loads(plan_path.read_text())
    plan["command"] = ["/bin/echo", "not-harbor"]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    manifest_path = output / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    for entry in manifest["files"]:
        if entry.get("relative_path") == "rollout_plan.json":
            entry["sha256"] = digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("AGS_API_KEY", "ags-secret")
    monkeypatch.setenv("TOKENHUB_KEY", "llm-secret")
    with pytest.raises(HarborRolloutError, match="绑定"):
        execute_rollout_plan(output)


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


def test_execute_rollout_maps_aliases_and_rejects_loopback_tokenhub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harbor = _harbor_root(tmp_path / "harbor")
    appendix = harbor / "configs" / "runtime-appendix.md"
    appendix.write_text("appendix\n")
    config_path = harbor / "configs" / "hermes-batch.yaml"
    config_path.write_text(
        config_path.read_text()
        + "extra_instruction_paths:\n  - configs/runtime-appendix.md\n"
    )
    output = build_rollout_plan(
        HarborRolloutConfig(
            task_dir=_bundle(tmp_path / "task"),
            harbor_root=harbor,
            output_root=tmp_path / "plans",
            jobs_root=tmp_path / "jobs",
        )
    )
    rendered = (output / "harbor-config.yaml").read_text()
    assert str(appendix) in rendered
    monkeypatch.delenv("AGS_API_KEY", raising=False)
    monkeypatch.delenv("TOKENHUB_KEY", raising=False)
    monkeypatch.delenv("TOKENHUB_BASE_URL", raising=False)
    monkeypatch.setenv("E2B_API_KEY", "e2b-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8443")
    captured: dict[str, str] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        captured.update({key: str(value) for key, value in env.items()})
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert execute_rollout_plan(output)["status"] == "COMPLETED"
    assert captured["AGS_API_KEY"] == "e2b-secret"
    assert captured["TOKENHUB_KEY"] == "anthropic-secret"
    assert captured["TOKENHUB_BASE_URL"] == "https://tokenhub.sensetime.com"
    assert captured["ANTHROPIC_BASE_URL"] == "https://tokenhub.sensetime.com"
    assert captured["AGS_TEMPLATE_ID"] == "node-python-hermes"
    assert captured["AGS_DOMAIN"] == "ap-beijing.tencentags.com"
    assert captured["E2B_VALIDATE_API_KEY"] == "false"


def test_execute_rollout_loads_ags_and_channel_from_config(
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
    config = tmp_path / "config.yaml"
    config.write_text(
        "e2bapikey:\n"
        "  e2b_unit_test_sandbox_key\n"
        "deepseek:\n"
        '  {"_type":"newapi_channel_conn","key":"unit-tokenhub-key",'
        '"url":"https://tokenhub.example"}\n',
        encoding="utf-8",
    )
    for name in (
        "AGS_API_KEY",
        "E2B_API_KEY",
        "TOKENHUB_KEY",
        "TOKENHUB_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    captured: dict[str, str] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        captured.update({key: str(value) for key, value in env.items()})
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert (
        execute_rollout_plan(output, config_path=config, channel="deepseek")["status"]
        == "COMPLETED"
    )
    assert captured["AGS_API_KEY"] == "e2b_unit_test_sandbox_key"
    assert captured["E2B_API_KEY"] == "e2b_unit_test_sandbox_key"
    assert captured["TOKENHUB_KEY"] == "unit-tokenhub-key"
    assert captured["TOKENHUB_BASE_URL"] == "https://tokenhub.example"
    plan_text = (output / "rollout_plan.json").read_text()
    assert "e2b_unit_test_sandbox_key" not in plan_text
    assert "unit-tokenhub-key" not in plan_text


@pytest.mark.parametrize("existing", ["", "    request_timeout_sec: 120\n    transfer_timeout_sec: 120\n"])
def test_rollout_binds_ags_command_and_request_timeouts(tmp_path: Path, existing: str) -> None:
    harbor = _harbor_root(tmp_path / "harbor")
    source = harbor / "configs/hermes-batch.yaml"
    source.write_text(source.read_text() + existing + "verifier:\n  timeout_sec: 30\n")
    output = build_rollout_plan(
        HarborRolloutConfig(
            task_dir=_bundle(tmp_path / "task"),
            harbor_root=harbor,
            output_root=tmp_path / "plans",
            jobs_root=tmp_path / "jobs",
            timeout_seconds=720,
        )
    )
    rendered = (output / "harbor-config.yaml").read_text()
    for field in ("sandbox_timeout_sec", "request_timeout_sec", "transfer_timeout_sec"):
        assert rendered.count(f"{field}: 720") == 1
    assert "timeout_sec: 120" not in rendered
    assert "verifier:\n  timeout_sec: 30" in rendered


def test_rollout_rejects_timeout_outside_environment(tmp_path: Path) -> None:
    harbor = _harbor_root(tmp_path / "harbor")
    source = harbor / "configs/hermes-batch.yaml"
    source.write_text(source.read_text().replace("environment:", "unrelated:"))
    with pytest.raises(HarborRolloutError, match="environment"):
        build_rollout_plan(
            HarborRolloutConfig(
                task_dir=_bundle(tmp_path / "task"),
                harbor_root=harbor,
                output_root=tmp_path / "plans",
                jobs_root=tmp_path / "jobs",
            )
        )


@pytest.mark.parametrize("tampered", ["task", "dataset", "plan"])
def test_publish_bundle_rejects_modified_plan_input(tmp_path: Path, tampered: str) -> None:
    plan = build_rollout_plan(
        HarborRolloutConfig(
            task_dir=_bundle(tmp_path / "task"),
            harbor_root=_harbor_root(tmp_path / "harbor"),
            output_root=tmp_path / "plans",
            jobs_root=tmp_path / "jobs",
        )
    )
    payload = json.loads((plan / "rollout_plan.json").read_text())
    targets = {
        "plan": plan / "rollout_plan.json",
        "dataset": plan / "dataset/dataset.toml",
        "task": plan / "dataset" / payload["dataset"]["task_relative_paths"][0] / "instruction.md",
    }
    with targets[tampered].open("a") as handle:
        handle.write("\n ")
    destination = tmp_path / "harbor_bundle"
    with pytest.raises(HarborRolloutError, match="hash"):
        publish_rollout_bundle(plan, destination)
    assert not destination.exists()
