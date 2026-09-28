"""命令行入口行为测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.cli import main


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
