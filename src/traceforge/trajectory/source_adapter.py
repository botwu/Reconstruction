"""将已解析来源记录适配为 M1B 可接受的显式输入契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RESTORED_LONG_CAPTURE_SCHEMA = "traceforge.restored-long-capture.v1"
R01_SESSIONS_SCHEMA = "traceforge.r01-sessions.v1"
SUPPORTED_SOURCE_SCHEMAS = frozenset(
    {RESTORED_LONG_CAPTURE_SCHEMA, R01_SESSIONS_SCHEMA}
)
_EXPECTED_TOP_LEVEL_KEYS = frozenset({"domain_meta", "messages", "meta", "tools"})
_R01_TOP_LEVEL_KEYS = frozenset({
    "record_id", "messages", "tools", "meta", "turn_quality_meta",
    "task_domain_annotations", "task_domain_session_meta", "rubric_partition",
})


class UnsupportedSourceSchemaError(ValueError):
    """调用方声明了当前编译器不支持的来源契约。"""


class SourceRecordAdaptError(ValueError):
    """单条来源记录不满足已声明的来源契约。"""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RestoredLongCaptureV1:
    """`traceforge.restored-long-capture.v1` 的 typed envelope。"""

    domain_meta: dict[str, Any]
    messages: list[Any]
    meta: dict[str, Any]
    tools: list[Any]


def adapt_source_record(
    value: Any,
    *,
    source_schema: str = RESTORED_LONG_CAPTURE_SCHEMA,
) -> RestoredLongCaptureV1:
    """将显式来源契约适配为 canonical restored-long envelope。"""

    if source_schema == R01_SESSIONS_SCHEMA:
        return _adapt_r01_record(value)
    if source_schema != RESTORED_LONG_CAPTURE_SCHEMA:
        raise UnsupportedSourceSchemaError(
            "source_schema 不是当前 source adapter 支持的显式契约"
        )
    return _adapt_restored_long_record(value)


def _adapt_restored_long_record(value: Any) -> RestoredLongCaptureV1:
    """校验 canonical restored-long envelope。"""

    if not isinstance(value, dict):
        raise SourceRecordAdaptError(
            "TOP_LEVEL_NOT_OBJECT",
            "来源记录顶层必须是 JSON 对象",
        )
    actual_keys = frozenset(value)
    if actual_keys != _EXPECTED_TOP_LEVEL_KEYS:
        missing = sorted(_EXPECTED_TOP_LEVEL_KEYS - actual_keys)
        unexpected_count = len(actual_keys - _EXPECTED_TOP_LEVEL_KEYS)
        raise SourceRecordAdaptError(
            "TOP_LEVEL_SCHEMA_MISMATCH",
            f"缺少字段={missing}, 额外字段数量={unexpected_count}",
        )

    domain_meta = value["domain_meta"]
    messages = value["messages"]
    meta = value["meta"]
    tools = value["tools"]
    if not isinstance(domain_meta, dict):
        raise SourceRecordAdaptError(
            "DOMAIN_META_NOT_OBJECT",
            "domain_meta 必须是 JSON 对象",
        )
    if not isinstance(messages, list) or not messages:
        raise SourceRecordAdaptError(
            "MESSAGES_INVALID",
            "messages 必须是非空 JSON 数组",
        )
    if not isinstance(meta, dict):
        raise SourceRecordAdaptError(
            "META_NOT_OBJECT",
            "meta 必须是 JSON 对象",
        )
    if not isinstance(tools, list):
        raise SourceRecordAdaptError(
            "TOOLS_NOT_ARRAY",
            "tools 必须是 JSON 数组",
        )
    if meta.get("representation") != "restored_long":
        raise SourceRecordAdaptError(
            "SOURCE_REPRESENTATION_UNSUPPORTED",
            "meta/representation 必须声明为 restored_long",
        )
    return RestoredLongCaptureV1(
        domain_meta=domain_meta,
        messages=messages,
        meta=meta,
        tools=tools,
    )


def _adapt_r01_record(value: Any) -> RestoredLongCaptureV1:
    """将 R01 sessions.v1 投影到 structural compiler 的 typed envelope。"""

    if not isinstance(value, dict):
        raise SourceRecordAdaptError("TOP_LEVEL_NOT_OBJECT", "来源记录顶层必须是 JSON 对象")
    actual_keys = frozenset(value)
    if actual_keys != _R01_TOP_LEVEL_KEYS:
        missing = sorted(_R01_TOP_LEVEL_KEYS - actual_keys)
        unexpected_count = len(actual_keys - _R01_TOP_LEVEL_KEYS)
        raise SourceRecordAdaptError(
            "R01_TOP_LEVEL_SCHEMA_MISMATCH",
            f"缺少字段={missing}, 额外字段数量={unexpected_count}",
        )
    messages, tools, meta = value["messages"], value["tools"], value["meta"]
    if not isinstance(messages, list) or not messages:
        raise SourceRecordAdaptError("MESSAGES_INVALID", "messages 必须是非空 JSON 数组")
    if not isinstance(tools, list):
        raise SourceRecordAdaptError("TOOLS_NOT_ARRAY", "tools 必须是 JSON 数组")
    if not isinstance(meta, dict):
        raise SourceRecordAdaptError("META_NOT_OBJECT", "meta 必须是 JSON 对象")
    if meta.get("representation") != "restored_long":
        raise SourceRecordAdaptError(
            "SOURCE_REPRESENTATION_UNSUPPORTED",
            "meta/representation 必须声明为 restored_long",
        )
    request_ids = meta.get("source_request_ids")
    depths = meta.get("terminal_prefix_depths")
    if not isinstance(request_ids, list) or not request_ids:
        raise SourceRecordAdaptError(
            "REQUEST_BOUNDARY_INVALID",
            "meta/source_request_ids 必须是非空数组",
        )
    if not all(isinstance(item, str) and item for item in request_ids):
        raise SourceRecordAdaptError(
            "REQUEST_BOUNDARY_INVALID",
            "meta/source_request_ids 只能包含非空字符串",
        )
    if not isinstance(depths, list) or not depths:
        raise SourceRecordAdaptError(
            "REQUEST_BOUNDARY_INVALID",
            "meta/terminal_prefix_depths 必须是非空数组",
        )
    account_id = meta.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        raise SourceRecordAdaptError("META_FIELD_INVALID", "meta/account_id 必须是非空字符串")
    source_session_id = _r01_source_session_id(meta) or account_id
    canonical_meta = dict(meta)
    canonical_meta.setdefault("capture_id", request_ids[-1])
    canonical_meta.setdefault("thread_id", source_session_id)

    turn_quality = value["turn_quality_meta"]
    task_annotations = value["task_domain_annotations"]
    session_meta = value["task_domain_session_meta"]
    rubric_partition = value["rubric_partition"]
    if not isinstance(turn_quality, dict):
        raise SourceRecordAdaptError(
            "TURN_QUALITY_META_NOT_OBJECT", "turn_quality_meta 必须是 JSON 对象"
        )
    if not isinstance(session_meta, dict):
        raise SourceRecordAdaptError(
            "TASK_DOMAIN_SESSION_META_NOT_OBJECT",
            "task_domain_session_meta 必须是 JSON 对象",
        )
    if not isinstance(task_annotations, list):
        raise SourceRecordAdaptError(
            "TASK_DOMAIN_ANNOTATIONS_NOT_ARRAY",
            "task_domain_annotations 必须是 JSON 数组",
        )
    if not isinstance(rubric_partition, dict):
        raise SourceRecordAdaptError(
            "RUBRIC_PARTITION_NOT_OBJECT", "rubric_partition 必须是 JSON 对象"
        )
    raw_input_audit = turn_quality.get("input_audit")
    input_audit = dict(raw_input_audit) if isinstance(raw_input_audit, dict) else {}
    if "input_truncated" not in input_audit:
        input_audit["input_truncated"] = None
    domain_meta = {
        "schema": R01_SESSIONS_SCHEMA,
        "source_record_id": value["record_id"],
        "input_audit": input_audit,
        "task_domain_session_meta": session_meta,
        "task_domain_annotations": task_annotations,
        "turn_quality_meta": turn_quality,
        "rubric_partition": rubric_partition,
    }
    return RestoredLongCaptureV1(
        domain_meta=domain_meta, messages=messages, meta=canonical_meta, tools=tools
    )


def _r01_source_session_id(meta: dict[str, Any]) -> str | None:
    """读取 R01 relay 的 source session 身份；缺失时返回 None."""

    relay_fields = meta.get("relay_fields")
    if not isinstance(relay_fields, dict):
        return None
    meta_data = relay_fields.get("meta_data")
    if not isinstance(meta_data, dict):
        return None
    raw_log = meta_data.get("raw_log")
    if not isinstance(raw_log, dict):
        return None
    value = raw_log.get("source_session_id")
    return value if isinstance(value, str) and value else None
