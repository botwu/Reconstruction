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
