from __future__ import annotations

import importlib.util
import json
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

    def run(command: list[str], **kwargs: object) -> int | None:
        run_root = Path(command[command.index("--output") + 1])
        receipt = run_root / "session_segmentation" / "session_task_segmentation.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"status": receipt_status}), encoding="utf-8")
        return exit_code

    monkeypatch.setattr(batch, "run_batch_process", run)
    report = batch.execute_batch(
        manifest_path=manifest, output_root=output, config=config, repo_root=_REPO
    )

    row = result_row(output)
    assert row["status"] == expected
    assert row["manifest"] is None
    assert row["stage_receipt"].endswith("session_task_segmentation.json")
    assert report["status_counts"] == {expected: 1}

def test_session_batch_records_timeout_and_preserves_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, config = inventory(tmp_path)
    output = tmp_path / "batch"

    def run(command: list[str], **kwargs: object) -> int | None:
        assert kwargs["timeout_seconds"] == 1
        Path(kwargs["stdout_path"]).write_text("partial")
        Path(kwargs["stderr_path"]).write_text("slow")
        return None

    monkeypatch.setattr(batch, "run_batch_process", run)
    report = batch.execute_batch(
        manifest_path=manifest,
        output_root=output,
        config=config,
        repo_root=_REPO,
        session_timeout_seconds=1,
    )

    row = result_row(output)
    assert row["status"] == "PROCESS_TIMEOUT"
    assert row["exit_code"] is None
    assert row["timeout_seconds"] == 1
    assert report["status"] == "COMPLETED_WITH_ERRORS"
    assert (output / "runs" / "r04-one" / "batch_stdout.txt").read_text() == "partial"
    assert (output / "runs" / "r04-one" / "batch_stderr.txt").read_text() == "slow"


def test_session_batch_real_timeout_continues_to_next_session(tmp_path: Path) -> None:
    manifest, config = inventory(tmp_path)
    payload = json.loads(manifest.read_text())
    sessions = Path(payload["sessions"])
    first = json.loads(sessions.read_text())
    sessions.write_text(
        json.dumps(first) + "\n"
        + json.dumps({**first, "session_id": "r04-two", "line_number": 2}) + "\n"
    )
    payload["pending_sessions"] = 2
    manifest.write_text(json.dumps(payload))
    repo = tmp_path / "fake_repo"
    package = repo / "src" / "traceforge"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "def main():\n"
        "    line = sys.argv[sys.argv.index('--line-number') + 1]\n"
        "    print('session ' + line, flush=True)\n"
        "    if line == '1':\n"
        "        time.sleep(30)\n"
        "    root = Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "    (root / 'reconstruction_manifest.json').write_text(json.dumps({'status': 'READY'}))\n"
        "    return 0\n"
    )
    output = tmp_path / "batch"
    report = batch.execute_batch(
        manifest_path=manifest, output_root=output, config=config,
        repo_root=repo, session_timeout_seconds=2,
    )
    rows = [json.loads(line) for line in (output / "batch_results.jsonl").read_text().splitlines()]
    assert [row["status"] for row in rows] == ["PROCESS_TIMEOUT", "READY"]
    assert [row["exit_code"] for row in rows] == [None, 0]
    assert report["completed_sessions"] == 2
    assert report["status"] == "COMPLETED_WITH_ERRORS"
    assert (output / "runs" / "r04-one" / "batch_stdout.txt").read_text() == "session 1\n"
