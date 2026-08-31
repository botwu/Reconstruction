"""冻结 event payload reader 的正常路径和关键拒绝路径。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from conftest import read_private

from traceforge.trajectory.event_payload import (
    AssistantMessagePayload,
    ContentBlocks,
    EventPayloadReadError,
    InvalidArguments,
    InvalidJsonTextArguments,
    JsonObjectArguments,
    JsonValueArguments,
    SystemPayload,
    TextContent,
    ToolCallPayload,
    ToolResultPayload,
    UserPayload,
    read_event_payload,
)

_DIGEST = "a" * 64
_ARGUMENT_POINTER = "/messages/3/tool_calls/0/function/arguments"


def _text_content(value: Any = "可见文本") -> dict[str, Any]:
    return {
        "kind": "TEXT",
        "value": value,
        "utf8_byte_length": 12,
        "sha256": _DIGEST,
    }


def _tool_call_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "assistant_event_id": "assistant-event",
        "tool_call_id": "call-1",
        "tool_type": "function",
        "function": {"name": "lookup", "arguments": arguments},
    }


def test_reader_explicitly_dispatches_all_compiler_event_payloads(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    run_dir = compile_dataset([two_boundary_capture], label="typed-event-payload")
    events = read_private(run_dir, "event_occurrences")
    expected_types = {
        "SYSTEM": SystemPayload,
        "USER": UserPayload,
        "ASSISTANT_MESSAGE": AssistantMessagePayload,
        "TOOL_CALL": ToolCallPayload,
        "TOOL_RESULT": ToolResultPayload,
    }

    typed = [read_event_payload(event["event_kind"], event["payload"]) for event in events]

    assert {type(item) for item in typed} == set(expected_types.values())
    for event, item in zip(events, typed, strict=True):
        assert isinstance(item, expected_types[event["event_kind"]])

    assistant = next(item for item in typed if isinstance(item, AssistantMessagePayload))
    assert isinstance(assistant.content, TextContent)
    assert assistant.reasoning_content.present is True
    tool_result = next(item for item in typed if isinstance(item, ToolResultPayload))
    assert isinstance(tool_result.content, ContentBlocks)
    with pytest.raises(FrozenInstanceError):
        assistant.extensions = {}  # type: ignore[misc]


@pytest.mark.parametrize(
    ("arguments", "expected_type"),
    [
        (
            {
                "kind": "JSON_OBJECT",
                "value": {"query": "alpha"},
                "source_json_pointer": _ARGUMENT_POINTER,
            },
            JsonObjectArguments,
        ),
        (
            {
                "kind": "JSON_VALUE",
                "value": ["alpha"],
                "source_json_pointer": _ARGUMENT_POINTER,
            },
            JsonValueArguments,
        ),
        (
            {
                "kind": "INVALID",
                "value": None,
                "source_json_pointer": _ARGUMENT_POINTER,
            },
            InvalidArguments,
        ),
        (
            {
                "kind": "INVALID_JSON_TEXT",
                "value": "not-json",
                "utf8_byte_length": 8,
                "sha256": _DIGEST,
                "source_json_pointer": _ARGUMENT_POINTER,
            },
            InvalidJsonTextArguments,
        ),
    ],
)
def test_reader_accepts_only_the_four_frozen_argument_kinds(
    arguments: dict[str, Any], expected_type: type[Any]
) -> None:
    payload = read_event_payload("TOOL_CALL", _tool_call_payload(arguments))

    assert isinstance(payload, ToolCallPayload)
    assert isinstance(payload.function.arguments, expected_type)


def test_reader_accepts_absent_reasoning_summary_without_inference() -> None:
    raw_payload = {
        "content": _text_content(),
        "reasoning_content": {
            "present": False,
            "utf8_byte_length": 0,
            "sha256": None,
            "source_json_pointer": None,
        },
        "extensions": {"vendor": {"kept": True}},
    }
    before = deepcopy(raw_payload)

    payload = read_event_payload("ASSISTANT_MESSAGE", raw_payload)

    assert isinstance(payload, AssistantMessagePayload)
    assert payload.reasoning_content.sha256 is None
    assert payload.extensions == {"vendor": {"kept": True}}
    assert raw_payload == before


@pytest.mark.parametrize(
    ("event_kind", "payload", "reason_code", "path"),
    [
        ("FUTURE_EVENT", {"content": _text_content()}, "EVENT_KIND_UNSUPPORTED", "/event_kind"),
        (None, {"content": _text_content()}, "EVENT_KIND_UNSUPPORTED", "/event_kind"),
        ("SYSTEM", [], "OBJECT_REQUIRED", "/payload"),
        ("USER", {}, "FIELD_REQUIRED", "/payload/content"),
        (
            "SYSTEM",
            {"content": {**_text_content(), "kind": "FUTURE_CONTENT"}},
            "KIND_UNSUPPORTED",
            "/payload/content/kind",
        ),
        (
            "ASSISTANT_MESSAGE",
            {"content": _text_content()},
            "FIELD_REQUIRED",
            "/payload/reasoning_content",
        ),
        (
            "ASSISTANT_MESSAGE",
            {
                "content": _text_content(),
                "reasoning_content": {
                    "present": False,
                    "utf8_byte_length": 0,
                    "sha256": _DIGEST,
                    "source_json_pointer": None,
                },
            },
            "VALUE_INVALID",
            "/payload/reasoning_content/sha256",
        ),
        (
            "TOOL_CALL",
            _tool_call_payload({"kind": "FUTURE_ARGUMENTS"}),
            "KIND_UNSUPPORTED",
            "/payload/function/arguments/kind",
        ),
        (
            "TOOL_RESULT",
            {
                "tool_call_id": "call-1",
                "tool_name": "lookup",
                "content": {
                    "kind": "CONTENT_BLOCKS",
                    "blocks": [],
                    "block_count": 1,
                },
            },
            "VALUE_INVALID",
            "/payload/content/block_count",
        ),
    ],
)
def test_reader_reports_stable_code_and_path(
    event_kind: Any,
    payload: Any,
    reason_code: str,
    path: str,
) -> None:
    with pytest.raises(EventPayloadReadError) as error:
        read_event_payload(event_kind, payload)

    assert error.value.reason_code == reason_code
    assert error.value.code == reason_code
    assert error.value.path == path
    assert error.value.detail


def test_reader_does_not_echo_untrusted_extra_field_name_or_value() -> None:
    sentinel = "sensitive-business-literal"
    payload = {"content": _text_content(), sentinel: sentinel}

    with pytest.raises(EventPayloadReadError) as error:
        read_event_payload("SYSTEM", payload)

    assert error.value.reason_code == "FIELD_UNEXPECTED"
    assert error.value.path == "/payload"
    assert sentinel not in str(error.value)
