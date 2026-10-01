"""命令行入口行为测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.cli import main


@pytest.mark.parametrize("domain", ["search", "terminal"])
def test_raw_run_only_requires_ags_for_terminal(tmp_path, monkeypatch, capsys, domain):
    import traceforge.cli as cli

    source = tmp_path / "session.jsonl"
    source.write_text(json.dumps({"messages": [{"role": "user", "content": "执行任务"}]}) + "\n")
    calls = []
    monkeypatch.setattr(cli, "build_ags_runtime_factory", lambda **kw: calls.append(kw))

    def stop_before_models(*args, **kwargs):
        raise ValueError("模型配置待补充")

    monkeypatch.setattr(cli, "resolve_role_matrix", stop_before_models)
    assert main([
        "reconstruct", "raw-run", "--input", str(source), "--domain", domain,
        "--line-number", "1", "--output", str(tmp_path / "out"),
        "--config", str(tmp_path / "config.yaml"),
    ]) == 2
    assert len(calls) == (1 if domain == "terminal" else 0)
    assert "模型配置待补充" in capsys.readouterr().err


@pytest.mark.parametrize("status,expected", [
    ("READY", 0), ("READY_VARIANT", 0), ("COMPLETED", 0),
    ("REVIEW", 2), ("BLOCKED", 2), ("PENDING_EXECUTION", 2), (None, 2),
])
def test_reconstruction_exit_reports_actual_manifest_state(tmp_path, capsys, status, expected):
    from traceforge.cli import _reconstruction_exit_code

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"status": status, "stopped_at": "intent"}))
    assert _reconstruction_exit_code(path) == expected
    output = capsys.readouterr()
    assert output.out.strip() == str(path)
    assert ("重建未完成" in output.err) == bool(expected)


def test_failure_analysis_review_batch_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    source = tmp_path / "analysis.jsonl"
    source.write_text(
        json.dumps(
            {
                "analysis_id": "a1",
                "decision": "REVIEW",
                "outcome": "FAILURE",
                "recoverability": "HIGH",
                "confidence": 0.8,
                "rubric": {"task_identifiability": 3},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    exit_code = main(
        [
            "failure-analysis",
            "review-batch",
            "--input-jsonl",
            str(source),
            "--output",
            str(tmp_path / "out"),
            "--limit",
            "1",
        ]
    )
    assert exit_code == 0
    output = Path(capsys.readouterr().out.strip())
    assert (output / "review_batch.json").is_file()


def test_removed_compile_command_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["trajectory", "compile"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("config_arg,overrides,expected", [
    (False, [], (14400, 500)),
    (True, [], (14400, 500)),
    (True, ["--timeout-seconds", "15000", "--max-iterations", "600"], (15000, 600)),
])
def test_prepare_rollout_uses_reviewed_budget(
    tmp_path, monkeypatch, capsys, config_arg, overrides, expected
):
    import traceforge.cli as cli

    config = tmp_path / "config.yaml"
    config.write_text('roles:\n  {"rollout":{"timeout_seconds":14400,"max_iterations":500}}\n')
    monkeypatch.setattr(cli, "DEFAULT_RUNTIME_CONFIG", config)
    captured = []

    def build_plan(value):
        captured.append(value)
        return tmp_path / "plan"

    monkeypatch.setattr(cli, "build_rollout_plan", build_plan)
    arguments = [
        "harbor-ags", "prepare-rollout", "--task-dir", str(tmp_path / "task"),
        "--harbor-root", str(tmp_path / "harbor"), "--output", str(tmp_path / "output"),
        "--jobs-root", str(tmp_path / "jobs"),
    ]
    if config_arg:
        arguments += ["--config", str(config)]
    assert cli.main(arguments + overrides) == 0
    assert len(captured) == 1
    assert (captured[0].timeout_seconds, captured[0].agent_max_iterations) == expected
    assert capsys.readouterr().out.strip() == str(tmp_path / "plan")


@pytest.mark.parametrize("override", [
    ["--timeout-seconds", "900"],
    ["--max-iterations", "30"],
])
def test_prepare_rollout_rejects_budget_downgrade(tmp_path, monkeypatch, capsys, override):
    import traceforge.cli as cli

    config = tmp_path / "config.yaml"
    config.write_text('roles:\n  {"rollout":{"timeout_seconds":14400,"max_iterations":500}}\n')
    calls = []
    monkeypatch.setattr(cli, "build_rollout_plan", lambda value: calls.append(value))
    assert cli.main([
        "harbor-ags", "prepare-rollout", "--task-dir", str(tmp_path / "task"),
        "--harbor-root", str(tmp_path / "harbor"), "--output", str(tmp_path / "output"),
        "--jobs-root", str(tmp_path / "jobs"), "--config", str(config), *override,
    ]) == 2
    assert calls == []
    assert "lower than reviewed config budget" in capsys.readouterr().err


@pytest.mark.parametrize("override,expected", [
    (None, "anthropic/anthropic/claude-opus-4-8/awsb_L/sfa"),
    ("anthropic/explicit-model", "anthropic/explicit-model"),
])
def test_raw_run_preserves_configured_gateway_model_id(tmp_path, monkeypatch, override, expected):
    import traceforge.cli as cli

    source = tmp_path / "session.jsonl"
    source.write_text(json.dumps({"messages": [{"role": "user", "content": "执行任务"}]}) + "\n")
    config = tmp_path / "config.yaml"
    config.write_text('roles:\n  {"rollout":{"channel":"claude",'
                      '"model":"anthropic/claude-opus-4-8/awsb_L/sfa"}}\n')
    monkeypatch.setattr(cli, "build_hermes_runtime", lambda **kwargs: object())
    monkeypatch.setattr(cli, "build_chat_model", lambda **kwargs: object())
    received = []

    def reconstruct(**kwargs):
        received.append(kwargs["verification_config"])
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"status": "COMPLETED"}))
        return path

    monkeypatch.setattr(cli, "run_raw_session_reconstruction", reconstruct)
    args = ["reconstruct", "raw-run", "--input", str(source), "--domain", "search",
            "--line-number", "1", "--output", str(tmp_path / "out"), "--config", str(config)]
    if override is not None:
        args += ["--rollout-model", override]
    assert cli.main(args) == 0
    assert received[0].rollout_model == expected
