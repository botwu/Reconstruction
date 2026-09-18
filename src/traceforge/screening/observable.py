"""可观察轨迹序列化：按 user span 切分，发给模型全部 span，超长字段机械截断。

移植自 datafilter_v2 turn_quality 的序列化约定，不引入 datafilter 依赖。
筛选目标是整条 capture 里能重建的失败任务，因此不 omit 更早的 user-span。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_MAX_INPUT_CHARS = 120_000
_PRIVATE_REASONING_FIELDS = frozenset(
    {"reasoning", "reasoning_content", "thinking", "thinking_content"}
)


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


def _bounded(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        edge = max(8, limit // 2)
        omitted = len(value) - edge * 2
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return (
            value[:edge]
            + f"…[omitted_chars={omitted} hash={digest}]…"
            + value[-edge:]
        )
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(serialized) <= limit:
        return value
    return {
        "type": "omitted_large_value",
        "serialized_chars": len(serialized),
        "hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16],
    }


def _event(
    index: int,
    message: Any,
    *,
    content_limit: int | None = None,
    include_private_reasoning: bool = False,
) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {"message_index": index, "invalid_message": repr(message)[:500]}
    excluded = {"metadata"}
    if not include_private_reasoning:
        excluded.update(_PRIVATE_REASONING_FIELDS)
    value = {key: item for key, item in message.items() if key not in excluded}
    if content_limit:
        for key in ("content", "reasoning_content"):
            if key in value:
                value[key] = _bounded(value[key], content_limit)
        compact_calls = []
        for call in value.get("tool_calls") or ():
            if not isinstance(call, dict):
                compact_calls.append(call)
                continue
            compact_call = dict(call)
            function = call.get("function")
            if isinstance(function, dict):
                compact_call["function"] = dict(function)
                if "arguments" in function:
                    compact_call["function"]["arguments"] = _bounded(
                        function["arguments"], content_limit
                    )
            compact_calls.append(compact_call)
        if "tool_calls" in value:
            value["tool_calls"] = compact_calls
    return {"message_index": index, **value}


def _features_block(features: dict[str, Any]) -> dict[str, Any]:
    return {
        "has_user_task_like_turn": features.get("has_user_task_like_turn"),
        "has_agent_attempt": features.get("has_agent_attempt"),
        "has_tool_activity": features.get("has_tool_activity"),
        "has_failure_or_unfinished_signal": features.get("has_failure_or_unfinished_signal"),
        "message_count": features.get("message_count"),
        "source_request_count": features.get("source_request_count"),
        "leaf_response_status": features.get("leaf_response_status"),
        "reason_hints": list(features.get("reason_hints") or ()),
    }


def _serialize_evidence(
    messages: list[dict[str, Any]],
    spans: list[TurnSpan],
    *,
    content_limit: int | None,
    features: dict[str, Any],
    span_audit: dict[str, Any],
) -> dict[str, Any]:
    shared_end = min((span.message_start for span in spans), default=0)
    return {
        "source_ref": features.get("source_ref"),
        "evidence": "observable_trajectory_only",
        "private_thinking_reasoning": "omitted_and_unscored",
        "shared_context": [
            _event(i, messages[i], content_limit=content_limit, include_private_reasoning=False)
            for i in range(shared_end)
        ],
        "spans": [
            {
                **span.as_dict(),
                "messages": [
                    _event(
                        i,
                        messages[i],
                        content_limit=content_limit,
                        include_private_reasoning=False,
                    )
                    for i in range(span.message_start, span.message_end)
                ],
            }
            for span in spans
        ],
        "span_ids": [span.span_id for span in spans],
        "span_audit": {
            "message_count": span_audit.get("message_count"),
            "span_count": span_audit.get("span_count"),
            "shared_context_end": span_audit.get("shared_context_end"),
            "tool_linkage_errors": list(span_audit.get("tool_linkage_errors") or ()),
        },
        "features": _features_block(features),
    }


def build_observable_evidence(
    raw_line: str,
    features: dict[str, Any],
    *,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
) -> dict[str, Any]:
    """把 capture 序列化为模型可打分的可观察证据。

    发送 shared_context + 全部 user-span 的可观察消息。默认不 omit 任何任务。
    超长时只对所有 span 同步机械截断 content/tool；user 句尽量保留。
    """

    try:
        value = json.loads(raw_line)
    except json.JSONDecodeError:
        value = None
    messages = value.get("messages") if isinstance(value, dict) else []
    if not isinstance(messages, list):
        messages = []
    typed_messages = [item for item in messages if isinstance(item, dict)]
    spans, span_audit = build_spans(typed_messages)
    if not spans:
        return {
            "source_ref": features.get("source_ref"),
            "evidence": "observable_trajectory_only",
            "private_thinking_reasoning": "omitted_and_unscored",
            "shared_context": [],
            "spans": [],
            "span_ids": [],
            "span_audit": span_audit,
            "features": _features_block(features),
            "serialization": {"content_limit": None, "truncated": False, "oversized": False},
        }

    payload = _serialize_evidence(
        typed_messages,
        spans,
        content_limit=None,
        features=features,
        span_audit=span_audit,
    )
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # The complete session is the screening evidence.  Do not mechanically
    # truncate a tool result or remove shared context: doing so can hide a
    # failure or make a cross-span relation impossible to prove.  Callers may
    # use ``oversized`` for cost routing, but the evidence object stays intact.
    payload["serialization"] = {
        "content_limit": None,
        "truncated": False,
        "oversized": len(text) > max_input_chars,
        "serialized_chars": len(text),
    }
    return payload
