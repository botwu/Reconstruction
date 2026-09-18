"""派生轨迹共用的隐私识别、脱敏与深度边界。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from traceforge.trajectory.json_codec import canonical_json_bytes, sha256_bytes

REASONING_FIELD = "reasoning_content"
PRIVATE_REASONING_FIELDS = frozenset(
    {"reasoning", "reasoning_content", "thinking", "thinking_content"}
)
PRIVACY_ENVELOPE_MARKER = "__traceforge_privacy_envelope__"
ESCAPED_OBJECT_V1 = "traceforge.privacy.escaped-object.v1"
DATA_URL_SUMMARY_V1 = "traceforge.privacy.data-url-summary.v1"
TEXT_WITH_DATA_URL_SEGMENTS_V1 = "traceforge.privacy.text-with-data-url-segments.v1"
TEXT_SEGMENT_V1 = "traceforge.privacy.text-segment.v1"
MAX_DERIVED_NESTING_DEPTH = 80

_PRIVACY_ENVELOPE_KINDS = frozenset(
    {
        ESCAPED_OBJECT_V1,
        DATA_URL_SUMMARY_V1,
        TEXT_WITH_DATA_URL_SEGMENTS_V1,
        TEXT_SEGMENT_V1,
    }
)
_RFC_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_DATA_URL_HEADER = re.compile(
    rf"data:"
    rf"(?P<mime>{_RFC_TOKEN}/{_RFC_TOKEN})?"
    rf"(?P<parameters>(?:\s*;\s*{_RFC_TOKEN}(?:\s*=\s*{_RFC_TOKEN})?)*)"
    rf"\s*,",
    flags=re.IGNORECASE,
)
_BASE64_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-"
)
_DATA_URL_DELIMITERS = frozenset(" \t\r\n\"'<>)]}")
_HEX_CHARACTERS = frozenset("0123456789abcdefABCDEF")
_INVALID_JSON_POINTER_ESCAPE = re.compile(r"~(?![01])")


class PrivacyTransformError(ValueError):
    """隐私派生无法在冻结边界内安全完成。"""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


@dataclass(frozen=True, slots=True)
class DataUrlSpan:
    """一个已识别 Data URL 在原字符串中的半开区间。"""

    start: int
    end: int
    mime_type: str
    encoding: str


@dataclass(frozen=True, slots=True)
class PrivacyViolation:
    """不回显业务值的派生隐私违规。"""

    code: str
    pointer: str


def find_data_url_spans(value: str) -> tuple[DataUrlSpan, ...]:
    """识别完整 Data URL，支持 RFC token、RFC2231 参数与百分号转义。"""

    spans: list[DataUrlSpan] = []
    cursor = 0
    while match := _DATA_URL_HEADER.search(value, cursor):
        parameter_names = {
            part.split("=", 1)[0].strip().casefold()
            for part in match.group("parameters").split(";")
            if part.strip()
        }
        encoding = "base64" if "base64" in parameter_names else "percent-encoded"
        payload_start = match.end()
        if encoding == "base64":
            payload_end = _consume_base64_payload(value, payload_start)
        else:
            payload_end = payload_start
            while payload_end < len(value) and value[payload_end] not in _DATA_URL_DELIMITERS:
                payload_end += 1
        span_end = max(match.end(), payload_end)
        spans.append(
            DataUrlSpan(
                start=match.start(),
                end=span_end,
                mime_type=(match.group("mime") or "text/plain").casefold(),
                encoding=encoding,
            )
        )
        cursor = span_end
    return tuple(spans)


def contains_data_url(value: str) -> bool:
    """判断字符串是否包含完整 Data URL，而不是孤立的参数片段。"""

    return bool(find_data_url_spans(value))


def privacy_envelope_kind(value: Any) -> str | None:
    """严格识别编译器生成的 envelope；普通业务对象不会被猜测。"""

    if not isinstance(value, dict):
        return None
    kind = value.get(PRIVACY_ENVELOPE_MARKER)
    if kind not in _PRIVACY_ENVELOPE_KINDS:
        return None
    if kind == ESCAPED_OBJECT_V1:
        return kind if _is_escaped_object_envelope(value) else None
    if kind == DATA_URL_SUMMARY_V1:
        return kind if _is_data_url_summary_envelope(value) else None
    if kind == TEXT_SEGMENT_V1:
        return kind if _is_text_segment_envelope(value) else None
    return kind if _is_segmented_text_envelope(value) else None


def find_privacy_violations(value: Any, pointer: str = "") -> tuple[PrivacyViolation, ...]:
    """迭代检查派生值，拒绝原始 Data URL、reasoning 与伪造 envelope。"""

    violations: list[PrivacyViolation] = []
    pending: list[tuple[Any, str]] = [(value, pointer)]
    while pending:
        current, current_pointer = pending.pop()
        if isinstance(current, str):
            if contains_data_url(current):
                violations.append(PrivacyViolation("RAW_DATA_URL", current_pointer))
            continue
        if isinstance(current, list):
            pending.extend(
                (item, f"{current_pointer}/{index}") for index, item in enumerate(current)
            )
            continue
        if not isinstance(current, dict):
            continue

        envelope_kind = privacy_envelope_kind(current)
        if PRIVACY_ENVELOPE_MARKER in current and envelope_kind is None:
            violations.append(PrivacyViolation("INVALID_PRIVACY_ENVELOPE", current_pointer))
        if envelope_kind == ESCAPED_OBJECT_V1:
            _check_escaped_object_reasoning(current, current_pointer, violations)

        for index, (key, item) in enumerate(current.items()):
            item_pointer = f"{current_pointer}/@item/{index}"
            if isinstance(key, str) and contains_data_url(key):
                violations.append(PrivacyViolation("RAW_DATA_URL", item_pointer))
            if key == REASONING_FIELD and not is_reasoning_summary(item):
                violations.append(PrivacyViolation("RAW_REASONING", item_pointer))
            # 摘要也必须继续扫描，不能让恶意 pointer 借摘要形态绕过 Data URL 检查。
            pending.append((item, item_pointer))
    return tuple(violations)


def is_reasoning_summary(value: Any) -> bool:
    """判断值是否符合唯一 reasoning 摘要形态。"""

    if not isinstance(value, dict) or set(value) != {
        "present",
        "utf8_byte_length",
        "sha256",
        "source_json_pointer",
    }:
        return False
    present = value.get("present")
    byte_length = value.get("utf8_byte_length")
    if not isinstance(present, bool) or not _is_nonnegative_integer(byte_length):
        return False
    if present:
        digest = value.get("sha256")
        source_pointer = value.get("source_json_pointer")
        return (
            _is_sha256(digest)
            and _is_json_pointer(source_pointer)
            and not contains_data_url(source_pointer)
        )
    return (
        byte_length == 0
        and value.get("sha256") is None
        and value.get("source_json_pointer") is None
    )


def summarize_reasoning_value(reasoning: Any, pointer: str) -> dict[str, Any]:
    """把 reasoning 原值转换为固定的存在性、长度、摘要和来源指针。"""

    try:
        if isinstance(reasoning, str):
            raw = reasoning.encode("utf-8")
        else:
            preflight_derived_value(reasoning)
            raw = canonical_json_bytes(reasoning)
    except RecursionError as exc:
        raise _recursion_transform_error() from exc
    return {
        "present": True,
        "utf8_byte_length": len(raw),
        "sha256": sha256_bytes(raw),
        "source_json_pointer": pointer,
    }


def sanitize_value(value: Any, pointer: str) -> Any:
    """摘要 Data URL 与任意位置 reasoning；超深结构以稳定错误终止。"""

    try:
        return _sanitize_value(value, pointer, 0)
    except RecursionError as exc:
        raise _recursion_transform_error() from exc


def omit_private_reasoning(value: Any) -> Any:
    """删除 thinking/reasoning 原文；完整 session 仍保留可见消息与工具。"""

    if isinstance(value, dict):
        return {
            key: omit_private_reasoning(item)
            for key, item in value.items()
            if key not in PRIVATE_REASONING_FIELDS
        }
    if isinstance(value, list):
        return [omit_private_reasoning(item) for item in value]
    return value


def visible_value_without_reasoning(value: Any) -> Any:
    """移除 reasoning 摘要，得到事件与目录指纹的可见视图。"""

    try:
        return _visible_value_without_reasoning(value, 0)
    except RecursionError as exc:
        raise _recursion_transform_error() from exc


def preflight_derived_value(value: Any) -> None:
    """在发布前确认派生值深度受控且可由规范 JSON 编码。"""

    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        _enforce_depth(depth)
        if isinstance(current, dict):
            pending.extend((key, depth + 1) for key in current)
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend((item, depth + 1) for item in current)
    try:
        canonical_json_bytes(value)
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise PrivacyTransformError(
            "DERIVED_VALUE_NOT_SERIALIZABLE",
            "派生值不满足规范 JSON 编码契约",
        ) from exc


def _sanitize_value(value: Any, pointer: str, depth: int) -> Any:
    _enforce_depth(depth)
    if isinstance(value, str):
        return _sanitize_string(value, pointer)
    if isinstance(value, list):
        return [
            _sanitize_value(item, f"{pointer}/{index}", depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return _sanitize_mapping(value, pointer, depth)
    return value


def _visible_value_without_reasoning(value: Any, depth: int) -> Any:
    _enforce_depth(depth)
    if isinstance(value, list):
        return [_visible_value_without_reasoning(item, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value

    if privacy_envelope_kind(value) == ESCAPED_OBJECT_V1:
        visible_entries = [
            {
                "key": _visible_value_without_reasoning(entry["key"], depth + 2),
                "value": _visible_value_without_reasoning(entry["value"], depth + 2),
            }
            for entry in value["entries"]
            if entry["key"] != REASONING_FIELD
        ]
        return {
            PRIVACY_ENVELOPE_MARKER: ESCAPED_OBJECT_V1,
            "entries": visible_entries,
            "entry_count": len(visible_entries),
            "source_json_pointer": value["source_json_pointer"],
        }
    return {
        key: _visible_value_without_reasoning(item, depth + 1)
        for key, item in value.items()
        if key != REASONING_FIELD
    }


def _sanitize_mapping(value: dict[Any, Any], pointer: str, depth: int) -> dict[Any, Any]:
    must_escape = PRIVACY_ENVELOPE_MARKER in value or any(
        isinstance(key, str) and contains_data_url(key) for key in value
    )
    if must_escape:
        entries = []
        for index, (key, item) in enumerate(value.items()):
            item_pointer = (
                f"{pointer}/{REASONING_FIELD}"
                if key == REASONING_FIELD
                else f"{pointer}/@entries/{index}/value"
            )
            entries.append(
                {
                    "key": _sanitize_value(
                        key,
                        f"{pointer}/@entries/{index}/key",
                        depth + 2,
                    ),
                    "value": (
                        summarize_reasoning_value(item, item_pointer)
                        if key == REASONING_FIELD
                        else _sanitize_value(item, item_pointer, depth + 2)
                    ),
                }
            )
        return {
            PRIVACY_ENVELOPE_MARKER: ESCAPED_OBJECT_V1,
            "entries": entries,
            "entry_count": len(entries),
            "source_json_pointer": pointer,
        }
    return {
        key: (
            summarize_reasoning_value(item, f"{pointer}/{REASONING_FIELD}")
            if key == REASONING_FIELD
            else _sanitize_value(
                item,
                f"{pointer}/{_escape_json_pointer(key)}",
                depth + 1,
            )
        )
        for key, item in value.items()
    }


def _check_escaped_object_reasoning(
    value: dict[Any, Any],
    pointer: str,
    violations: list[PrivacyViolation],
) -> None:
    for index, entry in enumerate(value["entries"]):
        if entry["key"] == REASONING_FIELD and not is_reasoning_summary(entry["value"]):
            violations.append(PrivacyViolation("RAW_REASONING", f"{pointer}/@entry/{index}"))


def _sanitize_string(value: str, pointer: str) -> str | dict[str, Any]:
    spans = find_data_url_spans(value)
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
        PRIVACY_ENVELOPE_MARKER: TEXT_WITH_DATA_URL_SEGMENTS_V1,
        "segments": segments,
        "utf8_byte_length": len(raw),
        "sha256": sha256_bytes(raw),
        "source_json_pointer": pointer,
    }


def _consume_base64_payload(value: str, start: int) -> int:
    cursor = start
    while cursor < len(value) and value[cursor].isspace():
        cursor += 1
    last_payload_end = cursor
    while cursor < len(value):
        unit_end = _base64_unit_end(value, cursor)
        if unit_end is not None:
            cursor = unit_end
            last_payload_end = cursor
            continue
        if value[cursor] in "\r\n":
            whitespace_end = cursor
            while whitespace_end < len(value) and value[whitespace_end].isspace():
                whitespace_end += 1
            if _base64_unit_end(value, whitespace_end) is not None:
                cursor = whitespace_end
                continue
        break
    return last_payload_end


def _base64_unit_end(value: str, index: int) -> int | None:
    if index >= len(value):
        return None
    if value[index] in _BASE64_CHARACTERS:
        return index + 1
    if (
        value[index] == "%"
        and index + 2 < len(value)
        and value[index + 1] in _HEX_CHARACTERS
        and value[index + 2] in _HEX_CHARACTERS
    ):
        return index + 3
    return None


def _data_url_summary(value: str, span: DataUrlSpan, pointer: str) -> dict[str, Any]:
    raw = value[span.start : span.end].encode("utf-8")
    return {
        PRIVACY_ENVELOPE_MARKER: DATA_URL_SUMMARY_V1,
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
        PRIVACY_ENVELOPE_MARKER: TEXT_SEGMENT_V1,
        "value": value,
        "utf8_byte_length": len(value.encode("utf-8")),
        "source_character_start": start,
        "source_character_end": end,
    }


def _is_escaped_object_envelope(value: dict[Any, Any]) -> bool:
    if set(value) != {
        PRIVACY_ENVELOPE_MARKER,
        "entries",
        "entry_count",
        "source_json_pointer",
    }:
        return False
    entries = value.get("entries")
    return (
        isinstance(entries, list)
        and _is_nonnegative_integer(value.get("entry_count"))
        and value["entry_count"] == len(entries)
        and _is_json_pointer(value.get("source_json_pointer"))
        and all(isinstance(entry, dict) and set(entry) == {"key", "value"} for entry in entries)
    )


def _is_data_url_summary_envelope(value: dict[Any, Any]) -> bool:
    if set(value) != {
        PRIVACY_ENVELOPE_MARKER,
        "mime_type",
        "encoding",
        "utf8_byte_length",
        "sha256",
        "source_json_pointer",
        "source_character_start",
        "source_character_end",
    }:
        return False
    start = value.get("source_character_start")
    end = value.get("source_character_end")
    byte_length = value.get("utf8_byte_length")
    return (
        isinstance(value.get("mime_type"), str)
        and bool(value["mime_type"])
        and value.get("encoding") in {"base64", "percent-encoded"}
        and _is_nonnegative_integer(byte_length)
        and byte_length > 0
        and _is_sha256(value.get("sha256"))
        and _is_json_pointer(value.get("source_json_pointer"))
        and _is_nonnegative_integer(start)
        and _is_nonnegative_integer(end)
        and start < end
    )


def _is_segmented_text_envelope(value: dict[Any, Any]) -> bool:
    if set(value) != {
        PRIVACY_ENVELOPE_MARKER,
        "segments",
        "utf8_byte_length",
        "sha256",
        "source_json_pointer",
    }:
        return False
    segments = value.get("segments")
    byte_length = value.get("utf8_byte_length")
    return (
        isinstance(segments, list)
        and bool(segments)
        and all(
            privacy_envelope_kind(segment) in {DATA_URL_SUMMARY_V1, TEXT_SEGMENT_V1}
            for segment in segments
        )
        and any(privacy_envelope_kind(segment) == DATA_URL_SUMMARY_V1 for segment in segments)
        and _is_nonnegative_integer(byte_length)
        and byte_length > 0
        and _is_sha256(value.get("sha256"))
        and _is_json_pointer(value.get("source_json_pointer"))
    )


def _is_text_segment_envelope(value: dict[Any, Any]) -> bool:
    if set(value) != {
        PRIVACY_ENVELOPE_MARKER,
        "value",
        "utf8_byte_length",
        "source_character_start",
        "source_character_end",
    }:
        return False
    start = value.get("source_character_start")
    end = value.get("source_character_end")
    return (
        isinstance(value.get("value"), str)
        and not contains_data_url(value["value"])
        and _is_nonnegative_integer(value.get("utf8_byte_length"))
        and value["utf8_byte_length"] == len(value["value"].encode("utf-8"))
        and _is_nonnegative_integer(start)
        and _is_nonnegative_integer(end)
        and start < end
    )


def _enforce_depth(depth: int) -> None:
    if depth <= MAX_DERIVED_NESTING_DEPTH:
        return
    raise PrivacyTransformError(
        "PRIVACY_TRANSFORM_DEPTH_EXCEEDED",
        f"派生结构深度超过固定上限 {MAX_DERIVED_NESTING_DEPTH}",
    )


def _recursion_transform_error() -> PrivacyTransformError:
    return PrivacyTransformError(
        "PRIVACY_TRANSFORM_DEPTH_EXCEEDED",
        f"派生结构深度超过固定上限 {MAX_DERIVED_NESTING_DEPTH}",
    )


def _escape_json_pointer(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _is_json_pointer(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not _INVALID_JSON_POINTER_ESCAPE.search(value)
    )


def _is_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
