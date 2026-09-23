from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "run_terminal_batch", _REPO / "scripts" / "run_terminal_batch.py"
)
assert _SPEC is not None and _SPEC.loader is not None
batch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(batch)


@pytest.mark.parametrize(
    ("return_code", "expected"), [(0, "READY"), (None, "PROCESS_TIMEOUT")]
)
def test_terminal_batch_records_result_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, return_code: int | None, expected: str
) -> None:
    args = argparse.Namespace(
        python="python", config="config.yaml", hermes_home="hermes", harbor_root="harbor",
        rollout_trials=2, rollout_timeout_seconds=900, rollout_max_iterations=60,
        sandbox=True, execute_red=True, execute_rollout=True,
        repo_root=_REPO, session_timeout_seconds=1,
    )

    def run(command: list[str], **kwargs: object) -> int | None:
        assert kwargs["timeout_seconds"] == 1
        log = kwargs["stdout_path"]
        assert isinstance(log, Path)
        log.write_text("partial log\n")
        (log.parent / "reconstruction_manifest.json").write_text(
            json.dumps({"status": "READY", "stopped_at": None})
        )
        return return_code

    monkeypatch.setattr(batch, "run_batch_process", run)
    row = batch._run_one(
        0, {"input": "source.jsonl", "records": "records.jsonl", "line_number": 1},
        args, tmp_path,
    )
    assert row["status"] == expected
    assert row["return_code"] == return_code
    assert row["timeout_seconds"] == 1
    assert Path(row["manifest"]).is_file()
    assert (Path(row["output"]) / "run.log").read_text() == "partial log\n"
