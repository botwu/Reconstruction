from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "run_session_batch", _REPO / "scripts" / "run_session_batch.py"
)
assert _SPEC is not None and _SPEC.loader is not None
batch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(batch)


def inventory(tmp_path: Path) -> tuple[Path, Path]:
    sessions = tmp_path / "sessions.jsonl"
    sessions.write_text(
        json.dumps(
            {
                "session_id": "r04-one",
                "input": str(tmp_path / "missing-source.jsonl"),
                "rubric": "R04",
                "line_number": 1,
                "line_sha256": "a" * 64,
                "input_status": "PENDING",
            }
        ) + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "source_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "traceforge.session-source-manifest.v1",
                "status": "READY",
                "coverage_complete": True,
                "sessions": str(sessions),
                "pending_sessions": 1,
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    return manifest, config


def result_row(output: Path) -> dict:
    return json.loads((output / "batch_results.jsonl").read_text(encoding="utf-8"))


def test_session_batch_invokes_real_cli_and_records_missing_input(tmp_path: Path) -> None:
    manifest, config = inventory(tmp_path)
    output = tmp_path / "batch"

    report = batch.execute_batch(
        manifest_path=manifest, output_root=output, config=config, repo_root=_REPO
    )

    row = result_row(output)
    assert row["status"] == "PROCESS_ERROR"
    assert row["exit_code"] == 2
    assert row["manifest"] is None
    assert report["status_counts"] == {"PROCESS_ERROR": 1}
    stderr = (output / "runs" / "r04-one" / "batch_stderr.txt").read_text(encoding="utf-8")
    assert "原始 JSONL 不存在" in stderr


@pytest.mark.parametrize(
    ("receipt_status", "exit_code", "expected"),
    [
        ("READY", 0, "PROCESS_ERROR"),
        ("READY", 2, "PROCESS_ERROR"),
        ("SESSION_TASK_REVIEW", 2, "SESSION_TASK_REVIEW"),
        ("INPUT_INVALID", 2, "INPUT_INVALID"),
    ],
)
def test_session_batch_requires_reconstruction_manifest_for_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    receipt_status: str, exit_code: int, expected: str,
) -> None:
    manifest, config = inventory(tmp_path)
    output = tmp_path / "batch"

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        run_root = Path(command[command.index("--output") + 1])
        receipt = run_root / "session_segmentation" / "session_task_segmentation.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"status": receipt_status}), encoding="utf-8")
        return subprocess.CompletedProcess(command, exit_code, stdout="", stderr="")

    monkeypatch.setattr(batch.subprocess, "run", run)
    report = batch.execute_batch(
        manifest_path=manifest, output_root=output, config=config, repo_root=_REPO
    )

    row = result_row(output)
    assert row["status"] == expected
    assert row["manifest"] is None
    assert row["stage_receipt"].endswith("session_task_segmentation.json")
    assert report["status_counts"] == {expected: 1}
