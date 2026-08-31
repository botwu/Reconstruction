"""将已解析来源记录适配为 M1B 可接受的显式输入契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RESTORED_LONG_CAPTURE_SCHEMA = "traceforge.restored-long-capture.v1"
_EXPECTED_TOP_LEVEL_KEYS = frozenset({"domain_meta", "messages", "meta", "tools"})


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


def adapt_source_record(value: Any) -> RestoredLongCaptureV1:
    """校验单条来源 envelope；错误信息不回显不可信字段值。"""

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
