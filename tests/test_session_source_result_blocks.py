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
