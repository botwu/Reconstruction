from pathlib import Path

import pytest

from traceforge.reconstruction.terminal_universe_environment import (
    EnvironmentReconstructionError,
    build_completion_prompt,
    materialize_environment,
    replay_initial_workspace,
    select_max_exposed_trajectory,
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


def test_stage3_selection_requires_independent_sufficiency():
    from traceforge.reconstruction.terminal_universe_environment import select_sufficient_candidate

    candidates = [
        {"decision": "READY", "confidence": 0.99, "uncertainties": []},
        {"decision": "READY", "confidence": 0.8, "uncertainties": []},
        {"decision": "REVIEW", "confidence": 1.0, "uncertainties": []},
    ]
    judges = [
        {"label": "INSUFFICIENT", "decision": "REVIEW"},
        {"label": "SUFFICIENT", "decision": "READY"},
        {"label": "SUFFICIENT", "decision": "READY"},
    ]
    selected, audit = select_sufficient_candidate(candidates, judges)
    assert selected == 1
    assert audit["eligible_count"] == 1


def test_stage3_skips_unreconstructable_and_selects_later_candidate():
    from traceforge.reconstruction.terminal_universe_environment import select_sufficient_candidate

    candidates = [
        {"decision": "SKIPPED_UNRECONSTRUCTABLE", "reason_codes": ["REFERENCED_ASSET_MISSING"]},
        {"decision": "READY", "confidence": 0.7, "uncertainties": []},
    ]
    judges = [
        {"label": "REVIEW", "decision": "REVIEW"},
        {"label": "SUFFICIENT", "decision": "READY"},
    ]
    selected, audit = select_sufficient_candidate(candidates, judges)
    assert selected == 1
    assert audit["rejected"][0]["reason"] == "SKIPPED_UNRECONSTRUCTABLE"
    assert audit["rejected"][0]["reason_codes"] == ["REFERENCED_ASSET_MISSING"]


def test_stage3_returns_no_candidate_when_label_unknown():
    from traceforge.reconstruction.terminal_universe_environment import select_sufficient_candidate

    selected, audit = select_sufficient_candidate(
        [{"decision": "READY", "confidence": 1.0}],
        [{"label": "UNKNOWN", "decision": "REVIEW"}],
    )
    assert selected is None
    assert audit["status"] == "NO_SUFFICIENT_CANDIDATE"


def test_seed_selection_uses_maximum_replay_exposure_not_reward():
    small = replay_initial_workspace(events())
    rich_events = [*events()[:2],
        {
            "sequence_number": 3,
            "event_kind": "TOOL_CALL",
            "event_occurrence_id": "e4",
            "payload": {"tool_name": "read", "arguments": {"path": "README.md"}, "call_id": "c2"},
        },
        {
            "sequence_number": 4,
            "event_kind": "TOOL_RESULT",
            "event_occurrence_id": "e5",
            "payload": {"call_id": "c2", "content": "project documentation"},
        },
        {
            "sequence_number": 5,
            "event_kind": "TOOL_CALL",
            "event_occurrence_id": "e6",
            "payload": {"tool_name": "write", "arguments": {"path": "src/app.py"}},
        },
    ]
    rich = replay_initial_workspace(rich_events)
    selected = select_max_exposed_trajectory(
        [
            {"trajectory_id": "small", "repository": "r", "base_commit": "c", "problem_statement": "p", "reward": 1, "replay": small},
            {"trajectory_id": "rich", "repository": "r", "base_commit": "c", "problem_statement": "p", "reward": 0, "replay": rich},
        ]
    )
    assert [item["trajectory_id"] for item in selected] == ["rich"]
