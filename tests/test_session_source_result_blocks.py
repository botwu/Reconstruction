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
