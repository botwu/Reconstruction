"""EventOccurrence payload 的唯一、显式 typed reader。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn

from traceforge.trajectory.contracts import EventKind

_SHA256_LENGTH = 64
_SHA256_CHARACTERS = frozenset("0123456789abcdef")
_ERROR_MESSAGES = {
    "EVENT_KIND_UNSUPPORTED": "event kind 不属于冻结枚举",
    "OBJECT_REQUIRED": "该路径必须是 JSON 对象",
    "FIELD_REQUIRED": "缺少冻结契约必需字段",
    "FIELD_UNEXPECTED": "存在冻结契约未声明字段",
    "VALUE_INVALID": "字段值不满足冻结契约",
    "KIND_UNSUPPORTED": "kind 不属于冻结枚举",
}


class EventPayloadReadError(ValueError):
    """payload 不满足冻结契约；异常只携带稳定 code 和 JSON path。"""

    def __init__(self, reason_code: str, path: str) -> None:
        self.code = reason_code
        self.reason_code = reason_code
        self.path = path
        self.detail = _ERROR_MESSAGES[reason_code]
        super().__init__(f"{reason_code}: {path}: {self.detail}")


type SanitizedText = str | dict[str, Any]


@dataclass(frozen=True, slots=True)
class TextContent:
    """字符串 content 的发布形态；value 也可能是 Data URL 摘要。"""

    value: SanitizedText
    utf8_byte_length: int
    sha256: str
    kind: Literal["TEXT"] = field(default="TEXT", init=False)


@dataclass(frozen=True, slots=True)
class ContentBlocks:
    """list 型 content 的发布形态，保留块结构。"""

    blocks: list[Any]
    block_count: int
    kind: Literal["CONTENT_BLOCKS"] = field(default="CONTENT_BLOCKS", init=False)


type ContentPayload = TextContent | ContentBlocks


@dataclass(frozen=True, slots=True)
class ReasoningSummary:
    """assistant reasoning 的固定审计摘要，不包含 reasoning 原文。"""

    present: bool
    utf8_byte_length: int
    sha256: str | None
    source_json_pointer: str | None


@dataclass(frozen=True, slots=True)
class JsonObjectArguments:
    """来源 arguments 本身就是 JSON object。"""

    value: dict[str, Any]
    source_json_pointer: str
    kind: Literal["JSON_OBJECT"] = field(default="JSON_OBJECT", init=False)


@dataclass(frozen=True, slots=True)
class JsonValueArguments:
    """来源 arguments 是可解析 JSON 文本。"""

    value: Any
    source_json_pointer: str
    kind: Literal["JSON_VALUE"] = field(default="JSON_VALUE", init=False)


@dataclass(frozen=True, slots=True)
class InvalidArguments:
    """来源 arguments 既不是 object，也不是字符串。"""

    value: Any
    source_json_pointer: str
    kind: Literal["INVALID"] = field(default="INVALID", init=False)


@dataclass(frozen=True, slots=True)
class InvalidJsonTextArguments:
    """来源 arguments 是不能解析为 JSON 的字符串。"""

    value: SanitizedText
    utf8_byte_length: int
    sha256: str
    source_json_pointer: str
    kind: Literal["INVALID_JSON_TEXT"] = field(default="INVALID_JSON_TEXT", init=False)


type ToolArguments = (
    JsonObjectArguments | JsonValueArguments | InvalidArguments | InvalidJsonTextArguments
)


@dataclass(frozen=True, slots=True)
class ToolFunction:
    """tool call function 的冻结发布形态。"""

    name: str
    arguments: ToolArguments
    extensions: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class SystemPayload:
    content: ContentPayload
    extensions: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class UserPayload:
    content: ContentPayload
    extensions: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class AssistantMessagePayload:
    content: ContentPayload
    reasoning_content: ReasoningSummary
    extensions: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ToolCallPayload:
    assistant_event_id: str
    tool_call_id: str
    tool_type: Any
    function: ToolFunction
    extensions: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ToolResultPayload:
    tool_call_id: str
    tool_name: str
    content: ContentPayload
    extensions: dict[str, Any] | None


type EventPayload = (
    SystemPayload | UserPayload | AssistantMessagePayload | ToolCallPayload | ToolResultPayload
)


def read_event_payload(event_kind: EventKind | str, payload: Any) -> EventPayload:
    """按 event kind 显式读取 payload；不猜测、修复或转换其他 schema。"""

    if not isinstance(event_kind, str) or event_kind not in {
        EventKind.SYSTEM,
        EventKind.USER,
        EventKind.ASSISTANT_MESSAGE,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
    }:
        _fail("EVENT_KIND_UNSUPPORTED", "/event_kind")
    value = _object(payload, "/payload")
    if event_kind == EventKind.SYSTEM:
        return _read_system(value)
    if event_kind == EventKind.USER:
        return _read_user(value)
    if event_kind == EventKind.ASSISTANT_MESSAGE:
        return _read_assistant(value)
    if event_kind == EventKind.TOOL_CALL:
        return _read_tool_call(value)
    if event_kind == EventKind.TOOL_RESULT:
        return _read_tool_result(value)
    _fail("EVENT_KIND_UNSUPPORTED", "/event_kind")


def _read_system(payload: dict[str, Any]) -> SystemPayload:
    _fields(payload, required=frozenset({"content"}), optional=frozenset({"extensions"}))
    return SystemPayload(
        content=_read_content(payload["content"], "/payload/content"),
        extensions=_extensions(payload),
    )


def _read_user(payload: dict[str, Any]) -> UserPayload:
    _fields(payload, required=frozenset({"content"}), optional=frozenset({"extensions"}))
    return UserPayload(
        content=_read_content(payload["content"], "/payload/content"),
        extensions=_extensions(payload),
    )


def _read_assistant(payload: dict[str, Any]) -> AssistantMessagePayload:
    _fields(
        payload,
        required=frozenset({"content", "reasoning_content"}),
        optional=frozenset({"extensions"}),
    )
    return AssistantMessagePayload(
        content=_read_content(payload["content"], "/payload/content"),
        reasoning_content=_read_reasoning_summary(
            payload["reasoning_content"],
            "/payload/reasoning_content",
        ),
        extensions=_extensions(payload),
    )


def _read_tool_call(payload: dict[str, Any]) -> ToolCallPayload:
    _fields(
        payload,
        required=frozenset({"assistant_event_id", "tool_call_id", "tool_type", "function"}),
        optional=frozenset({"extensions"}),
    )
    return ToolCallPayload(
        assistant_event_id=_nonempty_string(
            payload["assistant_event_id"],
            "/payload/assistant_event_id",
        ),
        tool_call_id=_nonempty_string(payload["tool_call_id"], "/payload/tool_call_id"),
        tool_type=payload["tool_type"],
        function=_read_tool_function(payload["function"], "/payload/function"),
        extensions=_extensions(payload),
    )


def _read_tool_result(payload: dict[str, Any]) -> ToolResultPayload:
    _fields(
        payload,
        required=frozenset({"tool_call_id", "tool_name", "content"}),
        optional=frozenset({"extensions"}),
    )
    return ToolResultPayload(
        tool_call_id=_nonempty_string(payload["tool_call_id"], "/payload/tool_call_id"),
        tool_name=_nonblank_string(payload["tool_name"], "/payload/tool_name"),
        content=_read_content(payload["content"], "/payload/content"),
        extensions=_extensions(payload),
    )


def _read_content(value: Any, path: str) -> ContentPayload:
    content = _object(value, path)
    kind = content.get("kind")
    if kind == "TEXT":
        _fields(
            content,
            required=frozenset({"kind", "value", "utf8_byte_length", "sha256"}),
            optional=frozenset(),
            path=path,
        )
        text_value = content["value"]
        if not isinstance(text_value, (str, dict)):
            _fail("VALUE_INVALID", f"{path}/value")
        return TextContent(
            value=text_value,
            utf8_byte_length=_nonnegative_int(
                content["utf8_byte_length"],
                f"{path}/utf8_byte_length",
            ),
            sha256=_sha256(content["sha256"], f"{path}/sha256"),
        )
    if kind == "CONTENT_BLOCKS":
        _fields(
            content,
            required=frozenset({"kind", "blocks", "block_count"}),
            optional=frozenset(),
            path=path,
        )
        blocks = content["blocks"]
        if not isinstance(blocks, list):
            _fail("VALUE_INVALID", f"{path}/blocks")
        block_count = _nonnegative_int(content["block_count"], f"{path}/block_count")
        if block_count != len(blocks):
            _fail("VALUE_INVALID", f"{path}/block_count")
        return ContentBlocks(
            blocks=blocks,
            block_count=block_count,
        )
    _fail("KIND_UNSUPPORTED", f"{path}/kind")


def _read_reasoning_summary(value: Any, path: str) -> ReasoningSummary:
    summary = _object(value, path)
    _fields(
        summary,
        required=frozenset({"present", "utf8_byte_length", "sha256", "source_json_pointer"}),
        optional=frozenset(),
        path=path,
    )
    present = summary["present"]
    if not isinstance(present, bool):
        _fail("VALUE_INVALID", f"{path}/present")
    length = _nonnegative_int(summary["utf8_byte_length"], f"{path}/utf8_byte_length")
    digest = summary["sha256"]
    pointer = summary["source_json_pointer"]
    if present:
        return ReasoningSummary(
            present=True,
            utf8_byte_length=length,
            sha256=_sha256(digest, f"{path}/sha256"),
            source_json_pointer=_json_pointer(pointer, f"{path}/source_json_pointer"),
        )
    if length != 0:
        _fail("VALUE_INVALID", f"{path}/utf8_byte_length")
    if digest is not None:
        _fail("VALUE_INVALID", f"{path}/sha256")
    if pointer is not None:
        _fail("VALUE_INVALID", f"{path}/source_json_pointer")
    return ReasoningSummary(
        present=False,
        utf8_byte_length=0,
        sha256=None,
        source_json_pointer=None,
    )


def _read_tool_function(value: Any, path: str) -> ToolFunction:
    function = _object(value, path)
    _fields(
        function,
        required=frozenset({"name", "arguments"}),
        optional=frozenset({"extensions"}),
        path=path,
    )
    return ToolFunction(
        name=_nonblank_string(function["name"], f"{path}/name"),
        arguments=_read_tool_arguments(function["arguments"], f"{path}/arguments"),
        extensions=_extensions(function, path),
    )


def _read_tool_arguments(value: Any, path: str) -> ToolArguments:
    arguments = _object(value, path)
    kind = arguments.get("kind")
    if kind == "JSON_OBJECT":
        _fields(
            arguments,
            required=frozenset({"kind", "value", "source_json_pointer"}),
            optional=frozenset(),
            path=path,
        )
        object_value = arguments["value"]
        if not isinstance(object_value, dict):
            _fail("VALUE_INVALID", f"{path}/value")
        return JsonObjectArguments(
            value=object_value,
            source_json_pointer=_json_pointer(
                arguments["source_json_pointer"],
                f"{path}/source_json_pointer",
            ),
        )
    if kind == "JSON_VALUE":
        _fields(
            arguments,
            required=frozenset({"kind", "value", "source_json_pointer"}),
            optional=frozenset(),
            path=path,
        )
        return JsonValueArguments(
            value=arguments["value"],
            source_json_pointer=_json_pointer(
                arguments["source_json_pointer"],
                f"{path}/source_json_pointer",
            ),
        )
    if kind == "INVALID":
        _fields(
            arguments,
            required=frozenset({"kind", "value", "source_json_pointer"}),
            optional=frozenset(),
            path=path,
        )
        return InvalidArguments(
            value=arguments["value"],
            source_json_pointer=_json_pointer(
                arguments["source_json_pointer"],
                f"{path}/source_json_pointer",
            ),
        )
    if kind == "INVALID_JSON_TEXT":
        _fields(
            arguments,
            required=frozenset(
                {"kind", "value", "utf8_byte_length", "sha256", "source_json_pointer"}
            ),
            optional=frozenset(),
            path=path,
        )
        text_value = arguments["value"]
        if not isinstance(text_value, (str, dict)):
            _fail("VALUE_INVALID", f"{path}/value")
        return InvalidJsonTextArguments(
            value=text_value,
            utf8_byte_length=_nonnegative_int(
                arguments["utf8_byte_length"],
                f"{path}/utf8_byte_length",
            ),
            sha256=_sha256(arguments["sha256"], f"{path}/sha256"),
            source_json_pointer=_json_pointer(
                arguments["source_json_pointer"],
                f"{path}/source_json_pointer",
            ),
        )
    _fail("KIND_UNSUPPORTED", f"{path}/kind")


def _fields(
    value: dict[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str],
    path: str = "/payload",
) -> None:
    missing = required - value.keys()
    if missing:
        _fail("FIELD_REQUIRED", f"{path}/{min(missing)}")
    if value.keys() - required - optional:
        # 不把未受信的额外字段名拼进 path 或异常消息。
        _fail("FIELD_UNEXPECTED", path)


def _extensions(
    value: dict[str, Any],
    path: str = "/payload",
) -> dict[str, Any] | None:
    if "extensions" not in value:
        return None
    extensions = value["extensions"]
    if not isinstance(extensions, dict):
        _fail("VALUE_INVALID", f"{path}/extensions")
    return extensions


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("OBJECT_REQUIRED", path)
    return value


def _nonnegative_int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail("VALUE_INVALID", path)
    return value


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("VALUE_INVALID", path)
    return value


def _nonblank_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("VALUE_INVALID", path)
    return value


def _json_pointer(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        _fail("VALUE_INVALID", path)
    return value


def _sha256(value: Any, path: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in _SHA256_CHARACTERS for character in value)
    ):
        _fail("VALUE_INVALID", path)
    return value


def _fail(reason_code: str, path: str) -> NoReturn:
    raise EventPayloadReadError(reason_code, path)
