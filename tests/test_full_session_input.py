"""完整 session 聚合输入的回归测试。"""

from __future__ import annotations

import json

from traceforge.trajectory.evidence_join import build_session_input


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )


def test_build_session_input_keeps_all_captures_and_chronology(tmp_path):
    captures = tmp_path / "captures.jsonl"
    events = tmp_path / "events.jsonl"
    output = tmp_path / "task_input.json"
    _write_jsonl(
        captures,
        [
            {
                "capture_occurrence_id": "c1",
                "candidate_group_id": "session-1",
                "request_time_start": "2026-01-01T00:00:00Z",
                "response_time_end": "2026-01-01T00:00:01Z",
            },
            {
                "capture_occurrence_id": "c2",
                "candidate_group_id": "session-1",
                "request_time_start": "2026-01-01T00:00:02Z",
                "response_time_end": "2026-01-01T00:00:03Z",
            },
        ],
    )
    shared_system = {
        "event_kind": "SYSTEM",
        "sequence_number": 1,
        "payload": {"content": {"value": "shared system context"}},
        "visible_payload_sha256": "sys",
    }
    _write_jsonl(
        events,
        [
            {**shared_system, "capture_occurrence_id": "c1", "event_occurrence_id": "e1"},
            {
                "capture_occurrence_id": "c1",
                "event_occurrence_id": "e2",
                "event_kind": "USER",
                "sequence_number": 2,
                "payload": {"content": {"value": "历史任务约束"}},
            },
            {**shared_system, "capture_occurrence_id": "c2", "event_occurrence_id": "e3"},
            {
                "capture_occurrence_id": "c2",
                "event_occurrence_id": "e4",
                "event_kind": "USER",
                "sequence_number": 2,
                "payload": {"content": {"value": "当前待完成任务"}},
            },
            {
                "capture_occurrence_id": "c2",
                "event_occurrence_id": "e5",
                "event_kind": "TOOL_CALL",
                "sequence_number": 3,
                "payload": {
                    "tool_call_id": "call-1",
                    "function": {"name": "terminal", "arguments": {"value": "pwd"}},
                },
            },
            {
                "capture_occurrence_id": "c2",
                "event_occurrence_id": "e6",
                "event_kind": "TOOL_RESULT",
                "sequence_number": 4,
                "payload": {"content": {"value": "/workspace"}, "tool_call_id": "call-1"},
            },
        ],
    )

    result = build_session_input(
        event_occurrences_path=events,
        capture_id="c2",
        captures_path=captures,
        output_path=output,
    )

    assert result["selection"]["session_capture_count"] == 2
    assert result["selection"]["raw_session_event_count"] == 6
    assert result["selection"]["deduplicated_session_event_count"] == 5
    assert result["selection"]["truncated"] is False
    texts = [item.get("text") for item in result["evidence"]]
    assert texts.index("历史任务约束") < texts.index("当前待完成任务")
    assert "当前待完成任务" in result["task_query_candidates"]
    shared = next(item for item in result["evidence"] if item.get("event_kind") == "SYSTEM")
    assert shared["_source_occurrence_ids"] == ["e1", "e3"]
