from pathlib import Path

import pytest

from traceforge.reconstruction.terminal_universe_environment import (
    EnvironmentReconstructionError,
    build_completion_prompt,
    materialize_environment,
    replay_initial_workspace,
    validate_completion_candidate,
)


def events():
    return [
        {
            "sequence_number": 1,
            "event_kind": "TOOL_CALL",
            "event_occurrence_id": "e1",
            "payload": {"tool_name": "read", "arguments": {"path": "src/app.py"}, "call_id": "c1"},
        },
        {
            "sequence_number": 2,
            "event_kind": "TOOL_RESULT",
            "event_occurrence_id": "e2",
            "payload": {"call_id": "c1", "content": "print('ok')"},
        },
        {
            "sequence_number": 3,
            "event_kind": "TOOL_CALL",
            "event_occurrence_id": "e3",
            "payload": {"tool_name": "write", "arguments": {"path": "src/app.py"}},
        },
    ]


def test_replay_withholds_mutations_and_materializes_first_read(tmp_path: Path):
    replay = replay_initial_workspace(events(), tmp_path / "replay")
    assert [item.path for item in replay.files] == ["src/app.py"]
    assert replay.files[0].content == "print('ok')"
    assert replay.withheld_changes[0].path == "src/app.py"
    assert (tmp_path / "replay/src/app.py").read_text() == "print('ok')"


def test_completion_rejects_hidden_path_and_unknown_evidence(tmp_path: Path):
    replay = replay_initial_workspace(events())
    candidate = {
        "decision": "READY",
        "files": [{"path": "tests/test.py", "content": "x", "evidence_ref_ids": ["ev"]}],
    }
    assert validate_completion_candidate(candidate, replay, {"ev"})[0] is False
    with pytest.raises(EnvironmentReconstructionError):
        materialize_environment(replay, candidate, tmp_path / "env", evidence_refs={"ev"})


def test_completion_prompt_exposes_paper_contract():
    replay = replay_initial_workspace(events())
    prompt = build_completion_prompt(
        {"core_objective": "inspect"}, replay, [{"evidence_ref_id": "ev"}], max_candidates=5
    )
    assert "solvable, but NOT solved" in prompt
    assert "at most 5 candidates" in prompt
    assert "/app" in prompt
