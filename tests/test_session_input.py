from __future__ import annotations

import hashlib

import pytest

from traceforge.cli import main
from traceforge.reconstruction.session_source import ReconstructionSourceError, load_raw_line


def test_input_reads_physical_line_and_verifies_original_bytes(tmp_path):
    raw = '{"messages": [{"role": "user", "content": "原始任务"}]}\n'
    path = tmp_path / "sessions.jsonl"
    path.write_text("\n" + raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    assert load_raw_line(path, line_number=2, line_sha256=digest) == raw
    with pytest.raises(ReconstructionSourceError, match="sha256"):
        load_raw_line(path, line_number=2, line_sha256="wrong")


@pytest.mark.parametrize("arguments", [["screening", "run"], ["reconstruct", "run"], ["reconstruct", "source"]])
def test_removed_screening_entries_cannot_be_invoked(arguments):
    with pytest.raises(SystemExit) as caught:
        main(arguments)
    assert caught.value.code == 2
