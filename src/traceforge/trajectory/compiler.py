"""将单个规范化 capture 编译为可审计的结构事实。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from traceforge.trajectory.contracts import (
    ACTION_BATCH_SCHEMA,
    CAPTURE_QUALITY_SCHEMA,
    CAPTURE_SCHEMA,
    EVENT_SCHEMA,
    REQUEST_BOUNDARY_SCHEMA,
    TOOL_CATALOG_SCHEMA,
    TOOL_PAIRING_SCHEMA,
    ActionBatchV1,
    BoundaryStatus,
    CaptureQualityV1,
    CompactionStatus,
    EventIntegrityStatus,
    EventKind,
    EventOccurrenceV1,
    EventScope,
    NormalizedCaptureV1,
    PrivacyStatus,
    ProcessingStatus,
    RequestBoundaryV1,
    SourceRecordRefV1,
    TerminalStatus,
    ToolCatalogV1,
    ToolPairingRecordV1,
    ToolPairingStatus,
    ToolSchemaStatus,
)
from traceforge.trajectory.json_codec import (
    StrictJsonError,
    canonical_json_bytes,
    sha256_bytes,
    stable_id,
    strict_json_loads,
)
from traceforge.trajectory.source_adapter import RestoredLongCaptureV1

_DATA_URL_HEADER = re.compile(
    r"data:"
    r"(?P<mime>[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+)?"
    r"(?P<parameters>(?:;[a-z0-9!#$&^_.+-]+(?:=[^;,\s\"'<>]*)?)*)"
    r"\s*,",
    flags=re.IGNORECASE,
)
_BASE64_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-"
)
_DATA_URL_DELIMITERS = frozenset(" \t\r\n\"'<>)]}")
_PAIRING_STATUS_ORDER = tuple(ToolPairingStatus)
_REASONING_FIELD = "reasoning_content"


class CaptureCompileError(ValueError):
    """capture 无法忠实映射到 M1B 契约。"""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CompiledCapture:
    """一个 capture 的全部 M1B 派生对象。"""

    capture: NormalizedCaptureV1
    request_boundaries: tuple[RequestBoundaryV1, ...]
    event_occurrences: tuple[EventOccurrenceV1, ...]
    action_batches: tuple[ActionBatchV1, ...]
    tool_pairings: tuple[ToolPairingRecordV1, ...]
    tool_catalog: ToolCatalogV1
    quality: CaptureQualityV1


@dataclass(frozen=True, slots=True)
class _ToolEvent:
    event_id: str
    sequence_number: int
    tool_call_id: str
    tool_name: str | None
    arguments_valid: bool | None


@dataclass(frozen=True, slots=True)
class _BoundaryFacts:
    source_request_ids: tuple[str, ...]
    terminal_prefix_depths: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _DataUrlSpan:
    start: int
    end: int
    mime_type: str
    encoding: str


def compile_capture(
    source_ref: SourceRecordRefV1,
    envelope: RestoredLongCaptureV1,
) -> CompiledCapture:
    """纯函数式编译一个已适配的 restored-long capture。"""

    domain_meta = envelope.domain_meta
    messages = envelope.messages
    meta = envelope.meta
    tools = envelope.tools
    source_capture_id = _required_string(meta, "capture_id")
    thread_id = _required_string(meta, "thread_id")
    account_id = _required_string(meta, "account_id")
    representation = _required_string(meta, "representation")
    capture_occurrence_id = stable_id(
        "capture-occurrence-v1",
        {
            "source_record_id": source_ref.source_record_id,
            "source_capture_id": source_capture_id,
        },
    )
    candidate_group_id = stable_id(
        "capture-candidate-group-v1",
        {"thread_id": thread_id, "account_id": account_id},
    )

    boundary_facts = _validate_boundary_facts(meta, messages, source_capture_id)
    request_boundaries, ownership = _compile_boundaries(
        capture_occurrence_id,
        messages,
        boundary_facts,
    )
    tool_catalog, tool_schema_status = _compile_tool_catalog(
        capture_occurrence_id,
        tools,
        meta,
    )
    events, action_batches, call_events, result_events = _compile_events(
        source_ref,
        capture_occurrence_id,
        messages,
        ownership,
    )
    tool_pairings = _compile_tool_pairings(
        capture_occurrence_id,
        call_events,
        result_events,
    )

    has_compaction = _required_boolean(meta, "has_compaction")
    compaction_count = _required_nonnegative_integer(meta, "compaction_count")
    raw_compaction_hashes = meta.get("compaction_hashes")
    if not isinstance(raw_compaction_hashes, list):
        raise CaptureCompileError(
            "META_FIELD_INVALID",
            "meta/compaction_hashes 必须是数组",
        )
    compaction_status = (
        CompactionStatus.UNLOCALIZED_COMPACTION_EVIDENCE
        if has_compaction or compaction_count > 0 or raw_compaction_hashes
        else CompactionStatus.NOT_OBSERVED
    )
    terminal_status = _terminal_status(messages[-1])
    pairing_statuses = _aggregate_pairing_statuses(tool_pairings)
    reason_codes = _quality_reason_codes(
        domain_meta=domain_meta,
        pairing_statuses=pairing_statuses,
        tool_schema_status=tool_schema_status,
        terminal_status=terminal_status,
        compaction_status=compaction_status,
    )

    boundary_ids = tuple(item.request_boundary_id for item in request_boundaries)
    capture = NormalizedCaptureV1(
        schema_version=CAPTURE_SCHEMA,
        capture_occurrence_id=capture_occurrence_id,
        source_record_id=source_ref.source_record_id,
        source_capture_id=source_capture_id,
        candidate_group_id=candidate_group_id,
        thread_id=thread_id,
        account_id=account_id,
        representation=representation,
        adapter=_sanitize_value(meta.get("adapter"), "/meta/adapter"),
        normalization_version=_sanitize_value(
            meta.get("normalization_version"),
            "/meta/normalization_version",
        ),
        model=_sanitize_value(meta.get("model"), "/meta/model"),
        source_models=_sanitize_value(meta.get("source_models"), "/meta/source_models"),
        source_request_count=len(boundary_facts.source_request_ids),
        request_boundary_ids=boundary_ids,
        message_count=len(messages),
        tool_catalog_id=tool_catalog.tool_catalog_id,
        request_time_start=_sanitize_value(
            meta.get("request_time_start"),
            "/meta/request_time_start",
        ),
        request_time_end=_sanitize_value(
            meta.get("request_time_end"),
            "/meta/request_time_end",
        ),
        response_time_end=_sanitize_value(
            meta.get("response_time_end"),
            "/meta/response_time_end",
        ),
        raw_request_hash=_sanitize_value(
            meta.get("raw_request_hash"),
            "/meta/raw_request_hash",
        ),
        target_hash=_sanitize_value(meta.get("target_hash"), "/meta/target_hash"),
        leaf_response_status=_sanitize_value(
            meta.get("leaf_response_status"),
            "/meta/leaf_response_status",
        ),
        usage=_sanitize_value(meta.get("usage"), "/meta/usage"),
        protocol_adapters=_sanitize_value(
            meta.get("protocol_adapters"),
            "/meta/protocol_adapters",
        ),
        sequence_repairs=_sanitize_value(
            meta.get("sequence_repairs"),
            "/meta/sequence_repairs",
        ),
        has_compaction=has_compaction,
        compaction_count=compaction_count,
        compaction_hashes=_sanitize_value(
            raw_compaction_hashes,
            "/meta/compaction_hashes",
        ),
        domain_meta_sha256=sha256_bytes(canonical_json_bytes(domain_meta)),
        meta_sha256=sha256_bytes(canonical_json_bytes(meta)),
    )

    missing_result_count = sum(
        ToolPairingStatus.RESULT_NOT_OBSERVED in pairing.statuses for pairing in tool_pairings
    )
    duplicate_result_count = sum(
        max(0, len(pairing.result_event_ids) - 1) for pairing in tool_pairings
    )
    quality = CaptureQualityV1(
        schema_version=CAPTURE_QUALITY_SCHEMA,
        source_record_id=source_ref.source_record_id,
        capture_occurrence_id=capture_occurrence_id,
        processing_status=ProcessingStatus.COMPLETE,
        boundary_status=BoundaryStatus.VALID,
        tool_pairing_applicable=bool(tool_pairings),
        tool_pairing_statuses=pairing_statuses,
        tool_schema_status=tool_schema_status,
        terminal_status=terminal_status,
        privacy_status=PrivacyStatus.DERIVATION_POLICY_APPLIED,
        compaction_status=compaction_status,
        processing_error=None,
        reason_codes=reason_codes,
        boundary_count=len(request_boundaries),
        event_count=len(events),
        action_batch_count=len(action_batches),
        tool_call_count=len(call_events),
        tool_result_count=len(result_events),
        missing_result_count=missing_result_count,
        duplicate_result_count=duplicate_result_count,
    )
    return CompiledCapture(
        capture=capture,
        request_boundaries=request_boundaries,
        event_occurrences=events,
        action_batches=action_batches,
        tool_pairings=tool_pairings,
        tool_catalog=tool_catalog,
        quality=quality,
    )


def _validate_boundary_facts(
    meta: Mapping[str, Any],
    messages: Sequence[Any],
    source_capture_id: str,
) -> _BoundaryFacts:
    raw_request_ids = meta.get("source_request_ids")
    raw_depths = meta.get("terminal_prefix_depths")
    raw_count = meta.get("source_request_count")
    if not isinstance(raw_request_ids, list) or not raw_request_ids:
        raise CaptureCompileError(
            "REQUEST_BOUNDARY_INVALID",
            "source_request_ids 必须是非空数组",
        )
    if not all(isinstance(item, str) and item for item in raw_request_ids):
        raise CaptureCompileError(
            "REQUEST_BOUNDARY_INVALID",
            "source_request_ids 只能包含非空字符串",
        )
    for index, request_id in enumerate(raw_request_ids):
        _reject_data_url_in_control_string(
            request_id,
            f"meta/source_request_ids/{index}",
        )
    if not isinstance(raw_depths, list) or not raw_depths:
        raise CaptureCompileError(
            "REQUEST_BOUNDARY_INVALID",
            "terminal_prefix_depths 必须是非空数组",
        )
    if not all(_is_integer(item) and item > 0 for item in raw_depths):
        raise CaptureCompileError(
            "REQUEST_BOUNDARY_INVALID",
            "terminal_prefix_depths 只能包含正整数",
        )
    if not _is_integer(raw_count) or raw_count <= 0:
        raise CaptureCompileError(
            "REQUEST_BOUNDARY_INVALID",
            "source_request_count 必须是正整数",
        )
    if len(raw_request_ids) != len(raw_depths) or len(raw_request_ids) != raw_count:
        raise CaptureCompileError(
            "REQUEST_BOUNDARY_INVALID",
            "request ID、terminal depth 与 source_request_count 数量不一致",
        )
    if any(left >= right for left, right in pairwise(raw_depths)):
        raise CaptureCompileError(
            "REQUEST_BOUNDARY_INVALID",
            "terminal_prefix_depths 必须严格递增",
        )
    if raw_depths[-1] != len(messages):
        raise CaptureCompileError(
            "REQUEST_BOUNDARY_INVALID",
            "最后一个 terminal depth 必须等于 messages 长度",
        )
    for ordinal, depth in enumerate(raw_depths):
        terminal = messages[depth - 1]
        if not isinstance(terminal, dict) or terminal.get("role") != "assistant":
            raise CaptureCompileError(
                "REQUEST_BOUNDARY_INVALID",
                f"第 {ordinal} 个 boundary 没有落在 assistant message",
            )
    if source_capture_id != raw_request_ids[-1]:
        raise CaptureCompileError(
            "REQUEST_BOUNDARY_INVALID",
            "capture_id 必须等于最后一个 source_request_id",
        )
    return _BoundaryFacts(tuple(raw_request_ids), tuple(raw_depths))


def _compile_boundaries(
    capture_occurrence_id: str,
    messages: Sequence[Any],
    facts: _BoundaryFacts,
) -> tuple[
    tuple[RequestBoundaryV1, ...],
    tuple[tuple[EventScope, str | None], ...],
]:
    ownership: list[tuple[EventScope, str | None]] = [
        (EventScope.PRE_FIRST_OBSERVED_TERMINAL, None) for _ in messages
    ]
    boundaries: list[RequestBoundaryV1] = []
    for ordinal, (request_id, depth) in enumerate(
        zip(facts.source_request_ids, facts.terminal_prefix_depths, strict=True)
    ):
        terminal_index = depth - 1
        owned_start = terminal_index if ordinal == 0 else facts.terminal_prefix_depths[ordinal - 1]
        boundary_id = stable_id(
            "request-boundary-v1",
            {
                "capture_occurrence_id": capture_occurrence_id,
                "boundary_ordinal": ordinal,
                "source_request_id": request_id,
                "terminal_prefix_depth": depth,
            },
        )
        terminal_event_id = _event_id(
            capture_occurrence_id,
            terminal_index,
            EventKind.ASSISTANT_MESSAGE,
            None,
        )
        boundaries.append(
            RequestBoundaryV1(
                schema_version=REQUEST_BOUNDARY_SCHEMA,
                request_boundary_id=boundary_id,
                capture_occurrence_id=capture_occurrence_id,
                boundary_ordinal=ordinal,
                source_request_id=request_id,
                owned_message_start_index=owned_start,
                terminal_message_index=terminal_index,
                terminal_prefix_depth=depth,
                terminal_event_id=terminal_event_id,
            )
        )
        for message_index in range(owned_start, depth):
            ownership[message_index] = (EventScope.OBSERVED_REQUEST_WINDOW, boundary_id)
    return tuple(boundaries), tuple(ownership)


def _compile_tool_catalog(
    capture_occurrence_id: str,
    tools: Sequence[Any],
    meta: Mapping[str, Any],
) -> tuple[ToolCatalogV1, ToolSchemaStatus]:
    raw_inferred_names = meta.get("inferred_tool_definitions", [])
    if not isinstance(raw_inferred_names, list) or not all(
        isinstance(item, str) and item for item in raw_inferred_names
    ):
        inferred_names: tuple[str, ...] = ()
        schema_invalid = True
    else:
        inferred_names = tuple(dict.fromkeys(raw_inferred_names))
        schema_invalid = False
        for index, inferred_name in enumerate(inferred_names):
            _reject_data_url_in_control_string(
                inferred_name,
                f"meta/inferred_tool_definitions/{index}",
            )

    inferred_set = frozenset(inferred_names)
    definitions: list[dict[str, Any]] = []
    for index, definition in enumerate(tools):
        pointer = f"/tools/{index}"
        name = _tool_definition_name(definition)
        if name is None:
            schema_invalid = True
        else:
            _reject_data_url_in_control_string(name, f"{pointer}/function/name")
        definitions.append(
            {
                "definition": _sanitize_value(definition, pointer),
                "provenance": (
                    "NORMALIZER_INFERRED"
                    if name is not None and name in inferred_set
                    else "SOURCE_DECLARED"
                ),
                "source_json_pointer": pointer,
            }
        )
    conflict = meta.get("tools_definition_conflict", False)
    if not isinstance(conflict, bool):
        schema_invalid = True
        conflict = bool(conflict)
    if schema_invalid:
        schema_status = ToolSchemaStatus.INVALID
    elif inferred_names and conflict:
        schema_status = ToolSchemaStatus.INFERRED_AND_CONFLICT
    elif inferred_names:
        schema_status = ToolSchemaStatus.INFERRED
    elif conflict:
        schema_status = ToolSchemaStatus.CONFLICT_REPORTED
    else:
        schema_status = ToolSchemaStatus.CONSISTENT

    catalog_sha256 = sha256_bytes(canonical_json_bytes(definitions))
    catalog_id = stable_id(
        "tool-catalog-v1",
        {
            "capture_occurrence_id": capture_occurrence_id,
            "catalog_sha256": catalog_sha256,
        },
    )
    return (
        ToolCatalogV1(
            schema_version=TOOL_CATALOG_SCHEMA,
            tool_catalog_id=catalog_id,
            capture_occurrence_id=capture_occurrence_id,
            definitions=tuple(definitions),
            definition_conflict=conflict,
            inferred_tool_names=inferred_names,
            catalog_sha256=catalog_sha256,
        ),
        schema_status,
    )


def _compile_events(
    source_ref: SourceRecordRefV1,
    capture_occurrence_id: str,
    messages: Sequence[Any],
    ownership: Sequence[tuple[EventScope, str | None]],
) -> tuple[
    tuple[EventOccurrenceV1, ...],
    tuple[ActionBatchV1, ...],
    tuple[_ToolEvent, ...],
    tuple[_ToolEvent, ...],
]:
    events: list[EventOccurrenceV1] = []
    batches: list[ActionBatchV1] = []
    call_events: list[_ToolEvent] = []
    result_events: list[_ToolEvent] = []

    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise CaptureCompileError(
                "MESSAGE_STRUCTURE_INVALID",
                f"messages/{message_index} 必须是 JSON 对象",
            )
        role = message.get("role")
        event_scope, boundary_id = ownership[message_index]
        message_pointer = f"/messages/{message_index}"
        if role in {"system", "user"}:
            event_kind = EventKind.SYSTEM if role == "system" else EventKind.USER
            payload = {
                "content": _compile_content(
                    message.get("content"),
                    f"{message_pointer}/content",
                )
            }
            visible_payload = dict(payload)
            extensions, visible_extensions = _compile_extensions(
                message,
                frozenset({"role", "content"}),
                message_pointer,
            )
            if extensions is not None:
                payload["extensions"] = extensions
            if visible_extensions is not None:
                visible_payload["extensions"] = visible_extensions
            events.append(
                _new_event(
                    source_ref=source_ref,
                    capture_occurrence_id=capture_occurrence_id,
                    sequence_number=len(events),
                    event_kind=event_kind,
                    event_scope=event_scope,
                    request_boundary_id=boundary_id,
                    message_index=message_index,
                    sub_index=None,
                    source_json_pointer=message_pointer,
                    payload=payload,
                    visible_payload=visible_payload,
                )
            )
            continue

        if role == "assistant":
            assistant_id = _event_id(
                capture_occurrence_id,
                message_index,
                EventKind.ASSISTANT_MESSAGE,
                None,
            )
            visible_payload = {
                "content": _compile_content(
                    message.get("content"),
                    f"{message_pointer}/content",
                )
            }
            assistant_payload = dict(visible_payload)
            assistant_payload["reasoning_content"] = _reasoning_summary(
                message,
                message_index,
            )
            extensions, visible_extensions = _compile_extensions(
                message,
                frozenset({"role", "content", "reasoning_content", "tool_calls"}),
                message_pointer,
            )
            if extensions is not None:
                assistant_payload["extensions"] = extensions
            if visible_extensions is not None:
                visible_payload["extensions"] = visible_extensions
            events.append(
                _new_event(
                    source_ref=source_ref,
                    capture_occurrence_id=capture_occurrence_id,
                    sequence_number=len(events),
                    event_kind=EventKind.ASSISTANT_MESSAGE,
                    event_scope=event_scope,
                    request_boundary_id=boundary_id,
                    message_index=message_index,
                    sub_index=None,
                    source_json_pointer=message_pointer,
                    payload=assistant_payload,
                    visible_payload=visible_payload,
                )
            )
            raw_tool_calls = message.get("tool_calls", [])
            if raw_tool_calls is None:
                raw_tool_calls = []
            if not isinstance(raw_tool_calls, list):
                raise CaptureCompileError(
                    "TOOL_CALL_STRUCTURE_INVALID",
                    f"messages/{message_index}/tool_calls 必须是数组",
                )

            tool_call_event_ids: list[str] = []
            for sub_index, tool_call in enumerate(raw_tool_calls):
                event, tool_event = _compile_tool_call_event(
                    source_ref=source_ref,
                    capture_occurrence_id=capture_occurrence_id,
                    sequence_number=len(events),
                    event_scope=event_scope,
                    request_boundary_id=boundary_id,
                    message_index=message_index,
                    sub_index=sub_index,
                    assistant_event_id=assistant_id,
                    tool_call=tool_call,
                )
                events.append(event)
                call_events.append(tool_event)
                tool_call_event_ids.append(event.event_occurrence_id)
            if tool_call_event_ids:
                batches.append(
                    ActionBatchV1(
                        schema_version=ACTION_BATCH_SCHEMA,
                        action_batch_id=stable_id(
                            "action-batch-v1",
                            {
                                "capture_occurrence_id": capture_occurrence_id,
                                "assistant_event_id": assistant_id,
                                "tool_call_event_ids": tool_call_event_ids,
                            },
                        ),
                        capture_occurrence_id=capture_occurrence_id,
                        request_boundary_id=boundary_id,
                        assistant_event_id=assistant_id,
                        tool_call_event_ids=tuple(tool_call_event_ids),
                        execution_semantics="UNORDERED_WITHIN_ASSISTANT_DECISION",
                    )
                )
            continue

        if role == "tool":
            event, tool_event = _compile_tool_result_event(
                source_ref=source_ref,
                capture_occurrence_id=capture_occurrence_id,
                sequence_number=len(events),
                event_scope=event_scope,
                request_boundary_id=boundary_id,
                message_index=message_index,
                message=message,
            )
            events.append(event)
            result_events.append(tool_event)
            continue

        raise CaptureCompileError(
            "MESSAGE_ROLE_UNSUPPORTED",
            f"messages/{message_index}/role 不属于 system、user、assistant、tool",
        )

    return tuple(events), tuple(batches), tuple(call_events), tuple(result_events)


def _compile_tool_call_event(
    *,
    source_ref: SourceRecordRefV1,
    capture_occurrence_id: str,
    sequence_number: int,
    event_scope: EventScope,
    request_boundary_id: str | None,
    message_index: int,
    sub_index: int,
    assistant_event_id: str,
    tool_call: Any,
) -> tuple[EventOccurrenceV1, _ToolEvent]:
    pointer = f"/messages/{message_index}/tool_calls/{sub_index}"
    if not isinstance(tool_call, dict):
        raise CaptureCompileError(
            "TOOL_CALL_STRUCTURE_INVALID",
            f"{pointer} 必须是 JSON 对象",
        )
    call_id = tool_call.get("id")
    function = tool_call.get("function")
    if not isinstance(call_id, str) or not call_id:
        raise CaptureCompileError("TOOL_CALL_ID_INVALID", f"{pointer}/id 必须是非空字符串")
    _reject_data_url_in_control_string(call_id, f"{pointer}/id")
    if not isinstance(function, dict):
        raise CaptureCompileError(
            "TOOL_CALL_STRUCTURE_INVALID",
            f"{pointer}/function 必须是 JSON 对象",
        )
    tool_name = function.get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise CaptureCompileError(
            "TOOL_CALL_NAME_INVALID",
            f"{pointer}/function/name 必须是非空字符串",
        )
    _reject_data_url_in_control_string(tool_name, f"{pointer}/function/name")
    arguments, arguments_valid = _compile_arguments(
        function.get("arguments"),
        f"{pointer}/function/arguments",
    )
    function_extensions, visible_function_extensions = _compile_extensions(
        function,
        frozenset({"name", "arguments"}),
        f"{pointer}/function",
    )
    function_payload = {"name": tool_name, "arguments": arguments}
    visible_function_payload = dict(function_payload)
    if function_extensions is not None:
        function_payload["extensions"] = function_extensions
    if visible_function_extensions is not None:
        visible_function_payload["extensions"] = visible_function_extensions

    extensions, visible_extensions = _compile_extensions(
        tool_call,
        frozenset({"id", "type", "function"}),
        pointer,
    )
    payload = {
        "assistant_event_id": assistant_event_id,
        "tool_call_id": call_id,
        "tool_type": _sanitize_value(tool_call.get("type"), f"{pointer}/type"),
        "function": function_payload,
    }
    visible_payload = {
        "assistant_event_id": assistant_event_id,
        "tool_call_id": call_id,
        "tool_type": _sanitize_value(tool_call.get("type"), f"{pointer}/type"),
        "function": visible_function_payload,
    }
    if extensions is not None:
        payload["extensions"] = extensions
    if visible_extensions is not None:
        visible_payload["extensions"] = visible_extensions
    event = _new_event(
        source_ref=source_ref,
        capture_occurrence_id=capture_occurrence_id,
        sequence_number=sequence_number,
        event_kind=EventKind.TOOL_CALL,
        event_scope=event_scope,
        request_boundary_id=request_boundary_id,
        message_index=message_index,
        sub_index=sub_index,
        source_json_pointer=pointer,
        payload=payload,
        visible_payload=visible_payload,
    )
    return event, _ToolEvent(
        event_id=event.event_occurrence_id,
        sequence_number=sequence_number,
        tool_call_id=call_id,
        tool_name=tool_name,
        arguments_valid=arguments_valid,
    )


def _compile_tool_result_event(
    *,
    source_ref: SourceRecordRefV1,
    capture_occurrence_id: str,
    sequence_number: int,
    event_scope: EventScope,
    request_boundary_id: str | None,
    message_index: int,
    message: Mapping[str, Any],
) -> tuple[EventOccurrenceV1, _ToolEvent]:
    pointer = f"/messages/{message_index}"
    call_id = message.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        raise CaptureCompileError(
            "TOOL_RESULT_ID_INVALID",
            f"{pointer}/tool_call_id 必须是非空字符串",
        )
    _reject_data_url_in_control_string(call_id, f"{pointer}/tool_call_id")
    tool_name = message.get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise CaptureCompileError(
            "TOOL_RESULT_NAME_INVALID",
            f"{pointer}/name 必须是非空字符串",
        )
    _reject_data_url_in_control_string(tool_name, f"{pointer}/name")
    payload = {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "content": _compile_content(message.get("content"), f"{pointer}/content"),
    }
    visible_payload = dict(payload)
    extensions, visible_extensions = _compile_extensions(
        message,
        frozenset({"role", "name", "tool_call_id", "content"}),
        pointer,
    )
    if extensions is not None:
        payload["extensions"] = extensions
    if visible_extensions is not None:
        visible_payload["extensions"] = visible_extensions
    event = _new_event(
        source_ref=source_ref,
        capture_occurrence_id=capture_occurrence_id,
        sequence_number=sequence_number,
        event_kind=EventKind.TOOL_RESULT,
        event_scope=event_scope,
        request_boundary_id=request_boundary_id,
        message_index=message_index,
        sub_index=None,
        source_json_pointer=pointer,
        payload=payload,
        visible_payload=visible_payload,
    )
    return event, _ToolEvent(
        event_id=event.event_occurrence_id,
        sequence_number=sequence_number,
        tool_call_id=call_id,
        tool_name=tool_name,
        arguments_valid=None,
    )


def _compile_tool_pairings(
    capture_occurrence_id: str,
    call_events: Sequence[_ToolEvent],
    result_events: Sequence[_ToolEvent],
) -> tuple[ToolPairingRecordV1, ...]:
    calls_by_id: dict[str, list[_ToolEvent]] = {}
    results_by_id: dict[str, list[_ToolEvent]] = {}
    first_sequence: dict[str, int] = {}
    for event in (*call_events, *result_events):
        first_sequence[event.tool_call_id] = min(
            event.sequence_number,
            first_sequence.get(event.tool_call_id, event.sequence_number),
        )
    for event in call_events:
        calls_by_id.setdefault(event.tool_call_id, []).append(event)
    for event in result_events:
        results_by_id.setdefault(event.tool_call_id, []).append(event)

    records: list[ToolPairingRecordV1] = []
    for call_id in sorted(first_sequence, key=lambda item: (first_sequence[item], item)):
        calls = calls_by_id.get(call_id, [])
        results = results_by_id.get(call_id, [])
        matched_call = calls[0] if calls and results else None
        matched_result = results[0] if calls and results else None
        observed_statuses: set[ToolPairingStatus] = set()
        if len(calls) > 1:
            observed_statuses.add(ToolPairingStatus.DUPLICATE_CALL_ID)
        if len(results) > 1:
            observed_statuses.add(ToolPairingStatus.DUPLICATE_RESULT)
        if calls and not results:
            observed_statuses.add(ToolPairingStatus.RESULT_NOT_OBSERVED)
        if results and not calls:
            observed_statuses.add(ToolPairingStatus.ORPHAN_RESULT)
        if matched_call is not None and matched_result is not None:
            observed_names = {event.tool_name for event in (*calls, *results)}
            if len(observed_names) > 1:
                observed_statuses.add(ToolPairingStatus.NAME_MISMATCH)
            if matched_result.sequence_number < matched_call.sequence_number:
                observed_statuses.add(ToolPairingStatus.RESULT_BEFORE_CALL)
        if any(event.arguments_valid is False for event in calls):
            observed_statuses.add(ToolPairingStatus.INVALID_CALL_ARGUMENTS)
        if len(calls) == 1 and len(results) == 1 and not observed_statuses:
            observed_statuses.add(ToolPairingStatus.MATCHED_ONE_TO_ONE)

        statuses = tuple(status for status in _PAIRING_STATUS_ORDER if status in observed_statuses)
        records.append(
            ToolPairingRecordV1(
                schema_version=TOOL_PAIRING_SCHEMA,
                pairing_id=stable_id(
                    "tool-pairing-v1",
                    {
                        "capture_occurrence_id": capture_occurrence_id,
                        "tool_call_id": call_id,
                    },
                ),
                capture_occurrence_id=capture_occurrence_id,
                tool_call_id=call_id,
                call_event_ids=tuple(item.event_id for item in calls),
                result_event_ids=tuple(item.event_id for item in results),
                matched_call_event_id=matched_call.event_id if matched_call else None,
                matched_result_event_id=matched_result.event_id if matched_result else None,
                statuses=statuses,
            )
        )
    return tuple(records)


def _new_event(
    *,
    source_ref: SourceRecordRefV1,
    capture_occurrence_id: str,
    sequence_number: int,
    event_kind: EventKind,
    event_scope: EventScope,
    request_boundary_id: str | None,
    message_index: int,
    sub_index: int | None,
    source_json_pointer: str,
    payload: dict[str, Any],
    visible_payload: dict[str, Any] | None = None,
) -> EventOccurrenceV1:
    fingerprint_payload = visible_payload if visible_payload is not None else payload
    encoded_fingerprint_payload = canonical_json_bytes(fingerprint_payload)
    return EventOccurrenceV1(
        schema_version=EVENT_SCHEMA,
        event_occurrence_id=_event_id(
            capture_occurrence_id,
            message_index,
            event_kind,
            sub_index,
        ),
        capture_occurrence_id=capture_occurrence_id,
        source_record_id=source_ref.source_record_id,
        sequence_number=sequence_number,
        event_kind=event_kind,
        event_scope=event_scope,
        request_boundary_id=request_boundary_id,
        message_index=message_index,
        sub_index=sub_index,
        source_json_pointer=source_json_pointer,
        visible_payload_utf8_byte_length=len(encoded_fingerprint_payload),
        visible_payload_sha256=sha256_bytes(encoded_fingerprint_payload),
        integrity_status=EventIntegrityStatus.COMPLETE,
        payload=payload,
    )


def _event_id(
    capture_occurrence_id: str,
    message_index: int,
    event_kind: EventKind,
    sub_index: int | None,
) -> str:
    return stable_id(
        "event-occurrence-v1",
        {
            "capture_occurrence_id": capture_occurrence_id,
            "message_index": message_index,
            "event_kind": event_kind,
            "sub_index": sub_index,
        },
    )


def _compile_content(content: Any, pointer: str) -> dict[str, Any]:
    if isinstance(content, str):
        return {
            "kind": "TEXT",
            "value": _sanitize_value(content, pointer),
            "utf8_byte_length": len(content.encode("utf-8")),
            "sha256": sha256_bytes(content.encode("utf-8")),
        }
    if isinstance(content, list):
        return {
            "kind": "CONTENT_BLOCKS",
            "blocks": _sanitize_value(content, pointer),
            "block_count": len(content),
            "sha256": sha256_bytes(canonical_json_bytes(content)),
        }
    raise CaptureCompileError(
        "MESSAGE_CONTENT_INVALID",
        f"{pointer} 必须是字符串或 content block 数组",
    )


def _compile_arguments(arguments: Any, pointer: str) -> tuple[dict[str, Any], bool]:
    if isinstance(arguments, dict):
        canonical = canonical_json_bytes(arguments)
        return (
            {
                "kind": "JSON_OBJECT",
                "value": _sanitize_value(arguments, pointer),
                "canonical_utf8_byte_length": len(canonical),
                "canonical_sha256": sha256_bytes(canonical),
                "source_json_pointer": pointer,
            },
            True,
        )
    if not isinstance(arguments, str):
        return (
            {
                "kind": "INVALID",
                "value": _sanitize_value(arguments, pointer),
                "source_json_pointer": pointer,
            },
            False,
        )
    raw_bytes = arguments.encode("utf-8")
    try:
        parsed = strict_json_loads(raw_bytes)
    except StrictJsonError:
        return (
            {
                "kind": "INVALID_JSON_TEXT",
                "value": _sanitize_value(arguments, pointer),
                "utf8_byte_length": len(raw_bytes),
                "sha256": sha256_bytes(raw_bytes),
                "source_json_pointer": pointer,
            },
            False,
        )
    return (
        {
            "kind": "JSON_VALUE",
            "value": _sanitize_value(parsed, pointer),
            "raw_utf8_byte_length": len(raw_bytes),
            "raw_sha256": sha256_bytes(raw_bytes),
            "source_json_pointer": pointer,
        },
        isinstance(parsed, dict),
    )


def _reasoning_summary(message: Mapping[str, Any], message_index: int) -> dict[str, Any]:
    pointer = f"/messages/{message_index}/reasoning_content"
    if "reasoning_content" not in message:
        return {
            "present": False,
            "utf8_byte_length": 0,
            "sha256": None,
            "source_json_pointer": None,
        }
    return _reasoning_value_summary(message["reasoning_content"], pointer)


def _reasoning_value_summary(reasoning: Any, pointer: str) -> dict[str, Any]:
    """仅保留 reasoning 的存在性与来源审计摘要。"""

    if isinstance(reasoning, str):
        raw = reasoning.encode("utf-8")
    else:
        raw = canonical_json_bytes(reasoning)
    return {
        "present": True,
        "utf8_byte_length": len(raw),
        "sha256": sha256_bytes(raw),
        "source_json_pointer": pointer,
    }


def _compile_extensions(
    source: Mapping[str, Any],
    consumed_keys: frozenset[str],
    pointer: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """保留未消费字段，同时从可见指纹中排除 reasoning 摘要。"""

    retained: dict[str, Any] = {}
    visible: dict[str, Any] = {}
    for key, value in source.items():
        if key in consumed_keys:
            continue
        if key == _REASONING_FIELD:
            retained[key] = _reasoning_value_summary(
                value,
                f"{pointer}/{_REASONING_FIELD}",
            )
            continue
        retained[key] = value
        visible[key] = value
    return (
        _sanitize_value(retained, pointer) if retained else None,
        _sanitize_value(visible, pointer) if visible else None,
    )


def _sanitize_value(value: Any, pointer: str) -> Any:
    if isinstance(value, str):
        return _sanitize_string(value, pointer)
    if isinstance(value, list):
        return [_sanitize_value(item, f"{pointer}/{index}") for index, item in enumerate(value)]
    if isinstance(value, dict):
        if any(isinstance(key, str) and _find_data_url_spans(key) for key in value):
            return {
                "kind": "OBJECT_ENTRIES",
                "entries": [
                    {
                        "key": _sanitize_value(
                            key,
                            f"{pointer}/@entries/{index}/key",
                        ),
                        "value": _sanitize_value(
                            item,
                            f"{pointer}/@entries/{index}/value",
                        ),
                    }
                    for index, (key, item) in enumerate(value.items())
                ],
                "entry_count": len(value),
                "source_json_pointer": pointer,
            }
        return {
            key: _sanitize_value(item, f"{pointer}/{_escape_json_pointer(key)}")
            for key, item in value.items()
        }
    return value


def _sanitize_string(value: str, pointer: str) -> str | dict[str, Any]:
    spans = _find_data_url_spans(value)
    if not spans:
        return value
    if len(spans) == 1 and spans[0].start == 0 and spans[0].end == len(value):
        return _data_url_summary(value, spans[0], pointer)

    segments: list[dict[str, Any]] = []
    cursor = 0
    for span in spans:
        if cursor < span.start:
            segments.append(_text_segment(value[cursor : span.start], cursor, span.start))
        segments.append(_data_url_summary(value, span, pointer))
        cursor = span.end
    if cursor < len(value):
        segments.append(_text_segment(value[cursor:], cursor, len(value)))

    raw = value.encode("utf-8")
    return {
        "kind": "TEXT_WITH_DATA_URL_SEGMENTS",
        "segments": segments,
        "utf8_byte_length": len(raw),
        "sha256": sha256_bytes(raw),
        "source_json_pointer": pointer,
    }


def _find_data_url_spans(value: str) -> tuple[_DataUrlSpan, ...]:
    spans: list[_DataUrlSpan] = []
    cursor = 0
    while match := _DATA_URL_HEADER.search(value, cursor):
        parameters = match.group("parameters")
        encoding = (
            "base64"
            if any(part.casefold() == "base64" for part in parameters.split(";") if part)
            else "percent-encoded"
        )
        payload_cursor = match.end()
        while payload_cursor < len(value) and value[payload_cursor].isspace():
            payload_cursor += 1
        end = payload_cursor
        if encoding == "base64":
            # Base64 Data URL 常被日志或序列化器折行。连续消费内部空白与后续
            # Base64 字符，避免只摘要首段后把余段当成普通文本写出。
            last_payload_end = end
            while end < len(value):
                if value[end] in _BASE64_CHARACTERS:
                    end += 1
                    last_payload_end = end
                    continue
                if value[end].isspace():
                    whitespace_end = end
                    while whitespace_end < len(value) and value[whitespace_end].isspace():
                        whitespace_end += 1
                    if (
                        any(character in "\r\n" for character in value[end:whitespace_end])
                        and whitespace_end < len(value)
                        and value[whitespace_end] in _BASE64_CHARACTERS
                    ):
                        end = whitespace_end
                        continue
                break
            end = last_payload_end
        else:
            while end < len(value) and value[end] not in _DATA_URL_DELIMITERS:
                end += 1
        span_end = max(match.end(), end)
        spans.append(
            _DataUrlSpan(
                start=match.start(),
                end=span_end,
                mime_type=(match.group("mime") or "text/plain").casefold(),
                encoding=encoding,
            )
        )
        cursor = span_end
    return tuple(spans)


def _data_url_summary(value: str, span: _DataUrlSpan, pointer: str) -> dict[str, Any]:
    raw = value[span.start : span.end].encode("utf-8")
    return {
        "kind": "DATA_URL_SUMMARY",
        "mime_type": span.mime_type,
        "encoding": span.encoding,
        "utf8_byte_length": len(raw),
        "sha256": sha256_bytes(raw),
        "source_json_pointer": pointer,
        "source_character_start": span.start,
        "source_character_end": span.end,
    }


def _text_segment(value: str, start: int, end: int) -> dict[str, Any]:
    return {
        "kind": "TEXT",
        "value": value,
        "utf8_byte_length": len(value.encode("utf-8")),
        "source_character_start": start,
        "source_character_end": end,
    }


def _terminal_status(last_message: Any) -> TerminalStatus:
    if not isinstance(last_message, dict) or last_message.get("role") != "assistant":
        return TerminalStatus.INVALID
    tool_calls = last_message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return TerminalStatus.TOOL_CALL_PENDING
    content = last_message.get("content")
    if isinstance(content, str) and content:
        return TerminalStatus.TEXT_OUTCOME
    if isinstance(content, list) and content:
        return TerminalStatus.TEXT_OUTCOME
    return TerminalStatus.EMPTY_OUTCOME


def _aggregate_pairing_statuses(
    pairings: Sequence[ToolPairingRecordV1],
) -> tuple[str, ...]:
    observed = {status for pairing in pairings for status in pairing.statuses}
    return tuple(status for status in _PAIRING_STATUS_ORDER if status in observed)


def _quality_reason_codes(
    *,
    domain_meta: Mapping[str, Any],
    pairing_statuses: Sequence[str],
    tool_schema_status: ToolSchemaStatus,
    terminal_status: TerminalStatus,
    compaction_status: CompactionStatus,
) -> tuple[str, ...]:
    reasons: list[str] = []
    reasons.extend(
        f"TOOL_PAIRING_{status}"
        for status in pairing_statuses
        if status != ToolPairingStatus.MATCHED_ONE_TO_ONE
    )
    if tool_schema_status != ToolSchemaStatus.CONSISTENT:
        reasons.append(f"TOOL_SCHEMA_{tool_schema_status}")
    if terminal_status != TerminalStatus.TEXT_OUTCOME:
        reasons.append(f"TERMINAL_{terminal_status}")
    if compaction_status == CompactionStatus.UNLOCALIZED_COMPACTION_EVIDENCE:
        reasons.append("UNLOCALIZED_COMPACTION_EVIDENCE")
    input_audit = domain_meta.get("input_audit")
    if isinstance(input_audit, dict) and input_audit.get("input_truncated") is True:
        reasons.append("INPUT_TRUNCATED")
    return tuple(reasons)


def _tool_definition_name(definition: Any) -> str | None:
    if not isinstance(definition, dict):
        return None
    function = definition.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) and name else None


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise CaptureCompileError("META_FIELD_INVALID", f"meta/{key} 必须是非空字符串")
    _reject_data_url_in_control_string(result, f"meta/{key}")
    return result


def _reject_data_url_in_control_string(value: str, pointer: str) -> None:
    """控制字符串不能降级为摘要，因此发现 Data URL 时隔离整条记录。"""

    if _find_data_url_spans(value):
        raise CaptureCompileError(
            "CONTROL_STRING_CONTAINS_DATA_URL",
            f"{pointer} 包含禁止写出的 Data URL",
        )


def _required_boolean(value: Mapping[str, Any], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise CaptureCompileError("META_FIELD_INVALID", f"meta/{key} 必须是布尔值")
    return result


def _required_nonnegative_integer(value: Mapping[str, Any], key: str) -> int:
    result = value.get(key)
    if not _is_integer(result) or result < 0:
        raise CaptureCompileError("META_FIELD_INVALID", f"meta/{key} 必须是非负整数")
    return result


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _escape_json_pointer(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")
