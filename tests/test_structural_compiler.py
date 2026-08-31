"""M1B 结构编译黑盒测试。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from conftest import read_private


def _events_at(
    events: list[dict[str, Any]], message_index: int, event_kind: str | None = None
) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event["message_index"] == message_index
        and (event_kind is None or event["event_kind"] == event_kind)
    ]


def _pairings_by_call_id(run_dir: Path) -> dict[str, dict[str, Any]]:
    return {pairing["tool_call_id"]: pairing for pairing in read_private(run_dir, "tool_pairings")}


def test_two_boundaries_assign_only_observed_ownership(
    compile_dataset: Callable[..., Path], two_boundary_capture: dict[str, Any]
) -> None:
    run_dir = compile_dataset([two_boundary_capture])
    boundaries = read_private(run_dir, "request_boundaries")
    events = read_private(run_dir, "event_occurrences")

    assert len(boundaries) == 2
    first, second = boundaries
    assert (first["owned_message_start_index"], first["terminal_message_index"]) == (2, 2)
    assert (second["owned_message_start_index"], second["terminal_message_index"]) == (3, 6)

    prefix_events = [event for event in events if event["message_index"] in (0, 1)]
    assert len(prefix_events) == 2
    for event in prefix_events:
        assert event["event_scope"] == "PRE_FIRST_OBSERVED_TERMINAL"
        assert event["request_boundary_id"] is None

    first_terminal_events = _events_at(events, 2)
    assert len(first_terminal_events) == 1
    for event in first_terminal_events:
        assert event["event_scope"] == "OBSERVED_REQUEST_WINDOW"
        assert event["request_boundary_id"] == first["request_boundary_id"]

    second_window_events = [event for event in events if 3 <= event["message_index"] < 7]
    assert {event["message_index"] for event in second_window_events} == {3, 4, 5, 6}
    for message_index in range(3, 7):
        for event in _events_at(events, message_index):
            assert event["event_scope"] == "OBSERVED_REQUEST_WINDOW"
            assert event["request_boundary_id"] == second["request_boundary_id"]


def test_assistant_decisions_split_tool_events_and_form_action_batches(
    compile_dataset: Callable[..., Path], two_boundary_capture: dict[str, Any]
) -> None:
    run_dir = compile_dataset([two_boundary_capture])
    events = read_private(run_dir, "event_occurrences")
    batches = read_private(run_dir, "action_batches")

    assert len(events) == 10  # 7 个 message occurrence，加 3 个 tool call occurrence。
    assert len(_events_at(events, 4, "ASSISTANT_MESSAGE")) == 1
    assert len(_events_at(events, 4, "TOOL_CALL")) == 2
    assert len(_events_at(events, 5, "TOOL_RESULT")) == 1
    assert len(batches) == 2

    event_ids = {event["event_occurrence_id"] for event in events}
    batch_sizes = sorted(len(batch["tool_call_event_ids"]) for batch in batches)
    assert batch_sizes == [1, 2]
    for batch in batches:
        assert batch["assistant_event_id"] in event_ids
        assert set(batch["tool_call_event_ids"]) <= event_ids
        assert batch["execution_semantics"] == "UNORDERED_WITHIN_ASSISTANT_DECISION"
    for event in events:
        assert event["integrity_status"] == "COMPLETE"
        assert event["visible_payload_utf8_byte_length"] > 0


def test_reasoning_and_data_url_payload_are_reduced_to_auditable_summaries(
    compile_dataset: Callable[..., Path], two_boundary_capture: dict[str, Any]
) -> None:
    run_dir = compile_dataset([two_boundary_capture])
    output = b"".join(path.read_bytes() for path in sorted(run_dir.rglob("*")) if path.is_file())

    assert "旧推理哨兵".encode() not in output
    assert b"U0VDUkVUX0JBU0U2NA==" not in output
    assert b"data:image/png;base64" not in output

    events = read_private(run_dir, "event_occurrences")
    encoded_events = json.dumps(events, ensure_ascii=False, sort_keys=True)
    assert "reasoning_content" in encoded_events
    assert "image/png" in encoded_events
    assert "sha256" in encoded_events


def test_mixed_data_urls_preserve_surrounding_text_without_leaking_payload(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    base64_sentinel = "U0VDUkVUX0JBU0U2NA=="
    malicious_key = f"键前缀 DATA:image/png;BASE64, \n{base64_sentinel} 键后缀"
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (f"文本前缀 data:image/png;base64, \n{base64_sentinel} 文本后缀"),
                    malicious_key: "安全值",
                }
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-uppercase-data-url",
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "arguments": (
                            f"参数前缀 DATA:image/png;BASE64,\n{base64_sentinel} 参数后缀"
                        ),
                    },
                }
            ],
        },
        {"role": "assistant", "content": "完成。"},
    ]
    capture = capture_factory(messages=messages, terminal_prefix_depths=[len(messages)])
    run_dir = compile_dataset([capture])
    output = b"".join(path.read_bytes() for path in sorted(run_dir.rglob("*")) if path.is_file())

    assert base64_sentinel.encode() not in output
    assert b"data:image/png" not in output.lower()
    for sentinel in ("文本前缀", "文本后缀", "键前缀", "键后缀", "参数前缀", "参数后缀"):
        assert sentinel.encode() in output

    encoded_events = json.dumps(
        read_private(run_dir, "event_occurrences"),
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "TEXT_WITH_DATA_URL_SEGMENTS" in encoded_events
    assert "OBJECT_ENTRIES" in encoded_events


def test_wrapped_base64_data_url_does_not_leak_later_lines(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    first_payload_line = "QUJDREVGR0hJSktM"
    second_payload_line = "TU5PUFFSU1RVVldYWVo="
    inline_payload = "QUJDRA=="
    messages = [
        {
            "role": "user",
            "content": (
                "明确前文 data:image/png;base64,"
                f"{first_payload_line}\n{second_payload_line}。明确后文；"
                f"prefix data:text/plain;base64,{inline_payload} suffix"
            ),
        },
        {"role": "assistant", "content": "完成。"},
    ]
    capture = capture_factory(messages=messages, terminal_prefix_depths=[len(messages)])
    run_dir = compile_dataset([capture], label="wrapped-data-url")
    output = b"".join(path.read_bytes() for path in sorted(run_dir.rglob("*")) if path.is_file())

    assert first_payload_line.encode() not in output
    assert second_payload_line.encode() not in output
    assert inline_payload.encode() not in output
    assert "明确前文".encode() in output
    assert "明确后文".encode() in output
    assert b"prefix" in output
    assert b"suffix" in output


def test_control_strings_with_data_urls_quarantine_without_echoing_values(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    unsafe = "data:image/png;base64,U0VDUkVUX0lERU5USVRZ"
    terminal = [{"role": "assistant", "content": "完成。"}]
    captures: list[dict[str, Any]] = []

    for field in ("capture_id", "thread_id", "account_id", "representation"):
        capture = capture_factory(messages=terminal, terminal_prefix_depths=[1])
        capture["meta"][field] = unsafe
        captures.append(capture)

    request_capture = capture_factory(messages=terminal, terminal_prefix_depths=[1])
    request_capture["meta"]["source_request_ids"] = [unsafe]
    captures.append(request_capture)

    inferred_capture = capture_factory(
        messages=terminal,
        terminal_prefix_depths=[1],
        inferred_tool_definitions=[unsafe],
    )
    captures.append(inferred_capture)

    definition_capture = capture_factory(
        messages=terminal,
        terminal_prefix_depths=[1],
        tools=[{"type": "function", "function": {"name": unsafe, "parameters": {}}}],
    )
    captures.append(definition_capture)

    call_id_capture = capture_factory(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": unsafe,
                        "type": "function",
                        "function": {"name": "fixture_tool", "arguments": {}},
                    }
                ],
            }
        ],
        terminal_prefix_depths=[1],
    )
    captures.append(call_id_capture)

    call_name_capture = capture_factory(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "fixture-call",
                        "type": "function",
                        "function": {"name": unsafe, "arguments": {}},
                    }
                ],
            }
        ],
        terminal_prefix_depths=[1],
    )
    captures.append(call_name_capture)

    result_name_capture = capture_factory(
        messages=[
            {
                "role": "tool",
                "name": unsafe,
                "tool_call_id": "fixture-call",
                "content": "虚构结果",
            },
            {"role": "assistant", "content": "完成。"},
        ],
        terminal_prefix_depths=[2],
    )
    captures.append(result_name_capture)

    for index, capture in enumerate(captures):
        run_dir = compile_dataset([capture], label=f"unsafe-control-{index}")
        output = b"".join(
            path.read_bytes() for path in sorted(run_dir.rglob("*")) if path.is_file()
        )
        [quality] = read_private(run_dir, "capture_quality")

        assert quality["processing_status"] == "QUARANTINED"
        expected_code = (
            "SOURCE_REPRESENTATION_UNSUPPORTED"
            if index == 3
            else "CONTROL_STRING_CONTAINS_DATA_URL"
        )
        assert quality["processing_error"]["code"] == expected_code
        assert unsafe.encode() not in output


def test_unexpected_top_level_key_is_not_echoed_in_quarantine(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    unsafe_key = "data:image/png;base64,VU5FWFBFQ1RFRF9LRVk="
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "完成。"}],
        terminal_prefix_depths=[1],
    )
    capture[unsafe_key] = "不可见值"
    run_dir = compile_dataset([capture], label="unexpected-key")
    output = b"".join(path.read_bytes() for path in sorted(run_dir.rglob("*")) if path.is_file())
    [quality] = read_private(run_dir, "capture_quality")

    assert quality["processing_error"]["code"] == "TOP_LEVEL_SCHEMA_MISMATCH"
    assert unsafe_key.encode() not in output


def test_unknown_visible_fields_are_preserved_as_sanitized_extensions(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    payload = "RVhURU5TSU9OX1BBWUxPQUQ="
    messages = [
        {
            "role": "system",
            "content": "系统正文",
            "system_extension": "系统扩展哨兵",
        },
        {
            "role": "user",
            "content": "用户正文",
            "user_extension": f"扩展前文 data:text/plain;base64,{payload}。扩展后文",
            "reasoning_content": "非 assistant 推理也不得外泄",
        },
        {
            "role": "assistant",
            "content": "",
            "assistant_extension": {"visible": "助手扩展哨兵"},
            "reasoning_content": "助手推理不得外泄",
            "tool_calls": [
                {
                    "id": "extension-call",
                    "type": "function",
                    "provider_extension": "调用扩展哨兵",
                    "function": {
                        "name": "fixture_tool",
                        "arguments": {},
                        "schema_extension": "函数扩展哨兵",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "fixture_tool",
            "tool_call_id": "extension-call",
            "content": "虚构结果",
            "result_extension": "结果扩展哨兵",
        },
        {"role": "assistant", "content": "完成。", "final_extension": "终态扩展哨兵"},
    ]
    capture = capture_factory(messages=messages, terminal_prefix_depths=[len(messages)])
    run_dir = compile_dataset([capture], label="extensions")
    events = read_private(run_dir, "event_occurrences")
    encoded = json.dumps(events, ensure_ascii=False, sort_keys=True)

    for sentinel in (
        "系统扩展哨兵",
        "扩展前文",
        "扩展后文",
        "助手扩展哨兵",
        "调用扩展哨兵",
        "函数扩展哨兵",
        "结果扩展哨兵",
        "终态扩展哨兵",
    ):
        assert sentinel in encoded
    assert payload not in encoded
    assert "非 assistant 推理也不得外泄" not in encoded
    assert "助手推理不得外泄" not in encoded
    assert all(event["integrity_status"] == "COMPLETE" for event in events)


def test_invalid_tool_names_quarantine_the_capture(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    invalid_cases = [
        (
            "call-name",
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-invalid-name",
                            "type": "function",
                            "function": {"name": "", "arguments": {}},
                        }
                    ],
                }
            ],
            "TOOL_CALL_NAME_INVALID",
        ),
        (
            "result-name",
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-invalid-result-name",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": {}},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": 7,
                    "tool_call_id": "call-invalid-result-name",
                    "content": "虚构结果",
                },
                {"role": "assistant", "content": "完成。"},
            ],
            "TOOL_RESULT_NAME_INVALID",
        ),
    ]

    for label, messages, expected_code in invalid_cases:
        capture = capture_factory(
            messages=messages,
            terminal_prefix_depths=[len(messages)],
        )
        run_dir = compile_dataset([capture], label=label)
        [quality] = read_private(run_dir, "capture_quality")

        assert quality["processing_status"] == "QUARANTINED"
        assert quality["processing_error"]["code"] == expected_code
        assert read_private(run_dir, "captures") == []
        assert read_private(run_dir, "event_occurrences") == []


def test_compaction_hashes_are_evidence_and_must_be_an_array(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "完成。"}],
        terminal_prefix_depths=[1],
    )
    capture["meta"]["compaction_hashes"] = ["a" * 64]
    run_dir = compile_dataset([capture], label="compaction-evidence")
    [quality] = read_private(run_dir, "capture_quality")
    assert quality["compaction_status"] == "UNLOCALIZED_COMPACTION_EVIDENCE"

    invalid_capture = capture_factory(
        messages=[{"role": "assistant", "content": "完成。"}],
        terminal_prefix_depths=[1],
    )
    invalid_capture["meta"]["compaction_hashes"] = "不是数组"
    invalid_run_dir = compile_dataset([invalid_capture], label="invalid-compaction-hashes")
    [invalid_quality] = read_private(invalid_run_dir, "capture_quality")
    assert invalid_quality["processing_status"] == "QUARANTINED"
    assert invalid_quality["processing_error"]["code"] == "META_FIELD_INVALID"


def test_schema_quality_combines_inference_and_reported_conflict(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "完成。"}],
        terminal_prefix_depths=[1],
        definition_conflict=True,
        inferred_tool_definitions=["inferred_fixture_tool"],
    )
    run_dir = compile_dataset([capture])
    [catalog] = read_private(run_dir, "tool_catalogs")
    [quality] = read_private(run_dir, "capture_quality")

    assert catalog["definition_conflict"] is True
    assert catalog["inferred_tool_names"] == ["inferred_fixture_tool"]
    assert quality["tool_schema_status"] == "INFERRED_AND_CONFLICT"


def test_terminal_tool_call_takes_precedence_over_nonempty_text(
    compile_dataset: Callable[..., Path], two_boundary_capture: dict[str, Any]
) -> None:
    run_dir = compile_dataset([two_boundary_capture])
    [quality] = read_private(run_dir, "capture_quality")

    assert quality["terminal_status"] == "TOOL_CALL_PENDING"
    assert quality["processing_status"] == "COMPLETE"
    assert quality["processing_error"] is None


def test_pairing_preserves_every_occurrence_and_classifies_anomalies(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    calls = [
        _call("call-one"),
        _call("call-missing"),
        _call("call-duplicate-result"),
        _call("call-name-mismatch"),
        _call("call-before"),
        _call("call-invalid", arguments="{broken"),
        _call("call-duplicate"),
        _call("call-duplicate"),
    ]
    messages = [
        {"role": "user", "content": "执行虚构工具配对测试。"},
        _result("call-orphan"),
        _result("call-before"),
        {"role": "assistant", "content": "", "tool_calls": calls},
        _result("call-one"),
        _result("call-duplicate-result", content="第一次结果"),
        _result("call-duplicate-result", name="different_duplicate_tool", content="第二次结果"),
        _result("call-name-mismatch", name="different_tool"),
        _result("call-invalid"),
        _result("call-duplicate"),
        {"role": "assistant", "content": "工具配对测试结束。"},
    ]
    capture = capture_factory(messages=messages, terminal_prefix_depths=[len(messages)])
    run_dir = compile_dataset([capture])
    pairings = _pairings_by_call_id(run_dir)

    assert len(pairings) == 8
    assert pairings["call-one"]["statuses"] == ["MATCHED_ONE_TO_ONE"]
    assert "RESULT_NOT_OBSERVED" in pairings["call-missing"]["statuses"]
    assert "DUPLICATE_RESULT" in pairings["call-duplicate-result"]["statuses"]
    assert "NAME_MISMATCH" in pairings["call-duplicate-result"]["statuses"]
    assert len(pairings["call-duplicate-result"]["result_event_ids"]) == 2
    assert "ORPHAN_RESULT" in pairings["call-orphan"]["statuses"]
    assert "NAME_MISMATCH" in pairings["call-name-mismatch"]["statuses"]
    assert "RESULT_BEFORE_CALL" in pairings["call-before"]["statuses"]
    assert "INVALID_CALL_ARGUMENTS" in pairings["call-invalid"]["statuses"]
    assert "DUPLICATE_CALL_ID" in pairings["call-duplicate"]["statuses"]
    assert len(pairings["call-duplicate"]["call_event_ids"]) == 2
    assert (
        pairings["call-duplicate-result"]["matched_call_event_id"]
        == (pairings["call-duplicate-result"]["call_event_ids"][0])
    )
    assert (
        pairings["call-duplicate-result"]["matched_result_event_id"]
        == (pairings["call-duplicate-result"]["result_event_ids"][0])
    )


def _call(call_id: str, *, arguments: str = '{"query":"fixture"}') -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "lookup", "arguments": arguments},
    }


def _result(call_id: str, *, name: str = "lookup", content: str = "虚构工具结果") -> dict[str, Any]:
    return {"role": "tool", "name": name, "tool_call_id": call_id, "content": content}
