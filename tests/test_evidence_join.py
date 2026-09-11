import json

from traceforge.trajectory.evidence_join import (
    build_query_task_input,
    build_task_input,
)


def test_capture_and_query(tmp_path):
    p = tmp_path / "e.jsonl"
    rows = [
        {
            "capture_occurrence_id": "c",
            "event_occurrence_id": "u",
            "event_kind": "USER",
            "sequence_number": 1,
            "source_json_pointer": "/messages/1",
            "payload": {"content": {"value": "q"}},
        },
        {
            "capture_occurrence_id": "c",
            "event_occurrence_id": "t",
            "event_kind": "TOOL_CALL",
            "sequence_number": 2,
            "source_json_pointer": "/messages/2",
            "payload": {},
        },
    ]
    p.write_text("\n".join(json.dumps(x) for x in rows))
    out = build_query_task_input(
        event_occurrences_path=p, capture_id="c", output_path=tmp_path / "o"
    )
    assert (
        out["task_query_candidates"] == ["q"] and out["selection"]["pending_tool_call_count"] == 1
    )


def test_missing(tmp_path):
    p = tmp_path / "e"
    p.write_text("")
    r = tmp_path / "r"
    r.write_text(json.dumps([{"source_id": "x"}]))
    out = build_task_input(
        evidence_path=r, event_occurrences_path=p, capture_id="c", output_path=tmp_path / "o"
    )
    assert out["quality"]["requires_review"]


def test_pending_tool_result_requires_review(tmp_path):
    p = tmp_path / "e"
    p.write_text(
        json.dumps(
            {
                "capture_occurrence_id": "c",
                "event_occurrence_id": "u",
                "event_kind": "USER",
                "sequence_number": 1,
                "source_json_pointer": "/m/1",
                "payload": {"content": {"value": "q"}},
            }
        )
        + "\n"
        + json.dumps(
            {
                "capture_occurrence_id": "c",
                "event_occurrence_id": "t",
                "event_kind": "TOOL_CALL",
                "sequence_number": 2,
                "source_json_pointer": "/m/2",
                "payload": {},
            }
        )
    )
    out = build_query_task_input(
        event_occurrences_path=p, capture_id="c", output_path=tmp_path / "o"
    )
    assert out["quality"]["requires_review"] is True
    assert "PENDING_TOOL_RESULT" in out["quality"]["reason_codes"]


def test_query_preserves_attempt_phases_without_calling_result_failure(tmp_path):
    p = tmp_path / "e"
    rows = [
        {
            "capture_occurrence_id": "c",
            "event_occurrence_id": "u0",
            "event_kind": "USER",
            "sequence_number": 1,
            "source_json_pointer": "/m/1",
            "payload": {"content": {"value": "context"}},
        },
        {
            "capture_occurrence_id": "c",
            "event_occurrence_id": "u1",
            "event_kind": "USER",
            "sequence_number": 2,
            "source_json_pointer": "/m/2",
            "payload": {"content": {"value": "target"}},
        },
        {
            "capture_occurrence_id": "c",
            "event_occurrence_id": "a1",
            "event_kind": "TOOL_CALL",
            "sequence_number": 3,
            "source_json_pointer": "/m/3",
            "payload": {"tool_call_id": "call-1", "function": {"name": "lookup"}},
        },
        {
            "capture_occurrence_id": "c",
            "event_occurrence_id": "r1",
            "event_kind": "TOOL_RESULT",
            "sequence_number": 4,
            "source_json_pointer": "/m/4",
            "payload": {"tool_call_id": "call-1", "content": {"value": "ok"}},
        },
        {
            "capture_occurrence_id": "c",
            "event_occurrence_id": "m1",
            "event_kind": "ASSISTANT_MESSAGE",
            "sequence_number": 5,
            "source_json_pointer": "/m/5",
            "payload": {"content": {"value": "done"}},
        },
    ]
    p.write_text("\n".join(json.dumps(row) for row in rows))
    pairings = tmp_path / "pairs"
    pairings.write_text(
        json.dumps(
            {
                "capture_occurrence_id": "c",
                "tool_call_id": "call-1",
                "matched_result_event_id": "r1",
                "statuses": ["MATCHED_ONE_TO_ONE"],
            }
        )
        + "\n"
    )
    out = build_query_task_input(
        event_occurrences_path=p,
        capture_id="c",
        output_path=tmp_path / "o",
        tool_pairings_path=pairings,
    )
    by_kind = {row["event_kind"]: row for row in out["evidence"]}
    assert by_kind["USER"]["phase"] == "TARGET_REQUEST"
    assert by_kind["TOOL_CALL"]["phase"] == "ATTEMPT_ACTION"
    assert by_kind["TOOL_RESULT"]["phase"] == "ATTEMPT_OBSERVATION"
    assert by_kind["ASSISTANT_MESSAGE"]["phase"] == "ATTEMPT_RESPONSE"
    assert by_kind["TOOL_CALL"]["role"] == "ATTEMPT"
    assert out["selection"]["pending_tool_call_count"] == 0


def test_query_marks_snapshot_and_truncation(tmp_path):
    p = tmp_path / "e"
    rows = [
        {
            "capture_occurrence_id": "c",
            "event_occurrence_id": "u",
            "event_kind": "USER",
            "sequence_number": 1,
            "source_json_pointer": "/m/1",
            "payload": {"content": {"value": "target"}},
        },
        {
            "capture_occurrence_id": "c",
            "event_occurrence_id": "a",
            "event_kind": "ASSISTANT_MESSAGE",
            "sequence_number": 2,
            "source_json_pointer": "/m/2",
            "payload": {"content": {"value": "answer"}},
        },
    ]
    p.write_text("\n".join(json.dumps(row) for row in rows))
    capture = tmp_path / "captures"
    capture.write_text(
        json.dumps(
            {
                "capture_occurrence_id": "c",
                "source_request_count": 1,
                "leaf_response_status": "completed",
            }
        )
        + "\n"
    )
    out = build_query_task_input(
        event_occurrences_path=p,
        capture_id="c",
        output_path=tmp_path / "o",
        max_events=1,
        captures_path=capture,
    )
    assert out["selection"]["truncated"] is True
    assert out["provenance"]["snapshot"] is True
    assert out["quality"]["requires_review"] is True
    assert "INPUT_TRUNCATED" in out["quality"]["reason_codes"]
