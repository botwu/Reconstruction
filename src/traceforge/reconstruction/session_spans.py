"""按原始用户消息建立任务分组所用的稳定 span 索引。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

@dataclass(frozen=True)
class TurnSpan:
    span_id: str
    message_start: int
    message_end: int
    user_message_indices: tuple[int, ...]
    truncated_target: bool = False

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["user_message_indices"] = list(self.user_message_indices)
        return value


def _content_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _role(message: Any) -> str:
    return str(message.get("role", "")) if isinstance(message, dict) else ""


def build_spans(messages: list[dict[str, Any]]) -> tuple[list[TurnSpan], dict[str, Any]]:
    """在 user 请求边界切分，不改源 messages。

    连续 user 合并为一个 request；其后的 assistant/tool 直到下一 user 同属该 span。
    """

    spans: list[TurnSpan] = []
    start: int | None = None
    users: list[int] = []
    previous_was_user = False
    for index, message in enumerate(messages):
        is_user = _role(message) == "user"
        if is_user and start is not None and not previous_was_user:
            spans.append(_make_span(messages, start, index, users))
            start, users = index, [index]
        elif is_user:
            if start is None:
                start = index
            users.append(index)
        previous_was_user = is_user
    if start is not None:
        spans.append(_make_span(messages, start, len(messages), users))

    first = spans[0].message_start if spans else len(messages)
    return spans, {
        "message_count": len(messages),
        "shared_context_end": first,
        "span_count": len(spans),
        "tool_linkage_errors": _tool_linkage_errors(messages, spans),
    }


def _make_span(
    messages: list[dict[str, Any]], start: int, end: int, users: list[int]
) -> TurnSpan:
    digest = _content_digest({"start": start, "end": end, "messages": messages[start:end]})[:16]
    has_assistant = any(_role(item) == "assistant" for item in messages[start:end])
    return TurnSpan(f"span_{digest}", start, end, tuple(users), not has_assistant)


def _tool_linkage_errors(
    messages: list[dict[str, Any]], spans: list[TurnSpan]
) -> list[dict[str, Any]]:
    span_at: dict[int, str] = {}
    for span in spans:
        for index in range(span.message_start, span.message_end):
            span_at[index] = span.span_id
    calls: dict[str, tuple[int, str | None]] = {}
    errors: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if _role(message) == "assistant":
            for call in message.get("tool_calls") or ():
                if isinstance(call, dict) and call.get("id"):
                    calls[str(call["id"])] = (index, span_at.get(index))
        if _role(message) == "tool" and message.get("tool_call_id"):
            call_id = str(message["tool_call_id"])
            if call_id not in calls:
                errors.append(
                    {
                        "code": "orphan_tool_result",
                        "message_index": index,
                        "tool_call_id": call_id,
                    }
                )
            elif calls[call_id][1] != span_at.get(index):
                errors.append(
                    {
                        "code": "tool_link_crosses_span",
                        "message_index": index,
                        "tool_call_id": call_id,
                        "call_message_index": calls[call_id][0],
                    }
                )
    return errors
