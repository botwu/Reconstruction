from __future__ import annotations

import pytest

from traceforge.reconstruction.session_source import tool_timeline


def test_result_blocks_preserve_malformed_json_and_nontext_slots() -> None:
    first = ' \n{"output":"first\\n"}\n '
    malformed = '{"output": "broken [REDACTED]\n'
    last = '{"output":"last\\n"}\n'
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "parallel", "name": "exec", "arguments": "source"}],
        },
        {
            "role": "tool",
            "tool_call_id": "parallel",
            "content": [
                {"type": "text", "text": first},
                {"type": "image", "data": "ignored"},
                {"type": "text", "text": malformed},
                {"type": "text", "text": last},
            ],
        },
    ]
    event = tool_timeline(messages, [])[0]
    assert event["result_blocks"] == [
        {"index": 0, "text": first},
        {"index": 1, "text": None},
        {"index": 2, "text": malformed},
        {"index": 3, "text": last},
    ]
    assert event["result_text"] == (first + malformed + last).strip()
    assert event["pending"] is False


@pytest.mark.parametrize(("content", "expected"), [
    (" \nraw\r\n ", [{"index": 0, "text": " \nraw\r\n "}]),
    ({"type": "text", "text": ""}, [{"index": 0, "text": ""}]),
    ([None, " raw ", {"value": "\tvalue\n"}], [
        {"index": 0, "text": None},
        {"index": 1, "text": " raw "},
        {"index": 2, "text": "\tvalue\n"},
    ]),
])
def test_orphan_result_preserves_original_content_slots(content, expected) -> None:
    event = tool_timeline([{"role": "tool", "tool_call_id": "orphan", "content": content}], [])[0]
    assert event["result_blocks"] == expected
    assert event["orphan_tool_result"] is True


def test_pending_call_has_no_result_slots() -> None:
    event = tool_timeline([
        {"role": "assistant", "tool_calls": [{"id": "pending", "name": "exec"}]},
    ], [])[0]
    assert event["result_blocks"] == []
    assert event["pending"] is True


def test_old_result_id_does_not_overwrite_previously_paired_result() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "original", "name": "read"}]},
        {"role": "tool", "tool_call_id": "original", "content": "first result"},
        {"role": "assistant", "tool_calls": [{"id": "original_renamed", "name": "read"}]},
        {"role": "tool", "tool_call_id": "original", "content": "later result"},
    ]
    first, pending, unmatched = tool_timeline(messages, [])
    assert first["result_text"] == "first result"
    assert first["tool_message_index"] == 1
    assert pending["call_id"] == "original_renamed"
    assert pending["pending"] is True
    assert unmatched["call_id"] == "original"
    assert unmatched["tool_message_index"] == 3
    assert unmatched["result_text"] == "later result"
    assert unmatched["orphan_tool_result"] is True


def test_reused_call_id_can_pair_after_previous_call_finished() -> None:
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "reused", "name": "read"}]},
        {"role": "tool", "tool_call_id": "reused", "content": "first result"},
        {"role": "assistant", "tool_calls": [{"id": "reused", "name": "read"}]},
        {"role": "tool", "tool_call_id": "reused", "content": "second result"},
    ]
    timeline = tool_timeline(messages, [])
    assert [e["tool_message_index"] for e in timeline] == [1, 3]
    assert [e["result_text"] for e in timeline] == ["first result", "second result"]
    assert all(not e["pending"] and not e.get("orphan_tool_result") for e in timeline)


@pytest.mark.parametrize("paired", [True, False])
@pytest.mark.parametrize(("key", "value"), [
    ("is_error", True),
    ("cleared", True),
    ("status", "failed"),
    ("result_status", "cancelled"),
])
def test_tool_result_rejection_state_survives_source_extraction(paired, key, value) -> None:
    messages = []
    if paired:
        messages.append({
            "role": "assistant",
            "tool_calls": [{"id": "result", "name": "exec", "arguments": "source"}],
        })
    body = '  {"exit_code":0,"output":"untrusted"}\n'
    messages.append({
        "role": "tool", "tool_call_id": "result",
        "content": [{"type": "text", "text": body}], key: value,
    })
    event = tool_timeline(messages, [])[0]
    assert event[key] == value
    assert event["result_blocks"] == [{"index": 0, "text": body}]
    assert event["result_text"] == body.strip()
    assert event["pending"] is False


@pytest.mark.parametrize("paired", [True, False])
def test_success_state_is_preserved_without_inventing_rejection(paired) -> None:
    messages = []
    if paired:
        messages.append({
            "role": "assistant", "tool_calls": [{"id": "result", "name": "exec"}],
        })
    state = {
        "is_error": False, "cleared": False, "status": "completed", "result_status": "success",
    }
    messages.append({"role": "tool", "tool_call_id": "result", "content": "body", **state})
    event = tool_timeline(messages, [])[0]
    assert {key: event[key] for key in state} == state


def test_input_text_preserves_banner_and_stdout_in_original_slots() -> None:
    banner = "Script completed\nWall time 0.7 seconds\nOutput:\n"
    stdout = "\r\n--- sample.py ---\r\n  17: body\r\n "
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "parallel", "name": "exec", "arguments": "source"}],
        },
        {
            "role": "tool", "tool_call_id": "parallel",
            "content": [
                {"type": "input_text", "text": banner},
                {"type": "image", "data": "ignored"},
                {"type": "input_text", "text": stdout},
            ],
        },
    ]
    event = tool_timeline(messages, [])[0]
    assert event["result_blocks"] == [
        {"index": 0, "text": banner},
        {"index": 1, "text": None},
        {"index": 2, "text": stdout},
    ]
    assert event["result_text"] == (banner + stdout).strip()
