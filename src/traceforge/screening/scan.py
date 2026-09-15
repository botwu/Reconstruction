"""从原始 JSONL 行抽取筛选特征，不编译事件、不复制大段正文。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("value")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    if isinstance(content, dict):
        value = content.get("value") or content.get("text")
        if isinstance(value, str):
            return value.strip()
    return ""


def _has_tool_calls(message: dict[str, Any]) -> bool:
    calls = message.get("tool_calls")
    if isinstance(calls, list) and calls:
        return True
    function_call = message.get("function_call")
    return isinstance(function_call, dict) and bool(function_call)


def scan_source_record(raw_line: str, *, line_number: int) -> dict[str, Any]:
    """扫描一行原始 JSONL。解析失败仍返回记录，由规则层拒绝。"""

    line_sha256 = hashlib.sha256(raw_line.encode("utf-8")).hexdigest()
    source_ref = f"jsonl:{line_number}:{line_sha256[:16]}"
    try:
        value = json.loads(raw_line)
    except json.JSONDecodeError:
        return {
            "source_ref": source_ref,
            "line_number": line_number,
            "line_sha256": line_sha256,
            "parse_ok": False,
            "has_user_task_like_turn": False,
            "has_agent_attempt": False,
            "has_tool_activity": False,
            "has_failure_or_unfinished_signal": False,
            "message_count": 0,
            "user_count": 0,
            "assistant_count": 0,
            "tool_message_count": 0,
            "source_request_count": 0,
            "last_assistant_empty": False,
            "leaf_response_status": None,
            "reason_hints": ("PARSE_FAILED",),
        }
    if not isinstance(value, dict):
        return {
            "source_ref": source_ref,
            "line_number": line_number,
            "line_sha256": line_sha256,
            "parse_ok": False,
            "has_user_task_like_turn": False,
            "has_agent_attempt": False,
            "has_tool_activity": False,
            "has_failure_or_unfinished_signal": False,
            "message_count": 0,
            "user_count": 0,
            "assistant_count": 0,
            "tool_message_count": 0,
            "source_request_count": 0,
            "last_assistant_empty": False,
            "leaf_response_status": None,
            "reason_hints": ("TOP_LEVEL_NOT_OBJECT",),
        }

    messages = value.get("messages")
    messages = messages if isinstance(messages, list) else []
    meta = value.get("meta") if isinstance(value.get("meta"), dict) else {}
    user_count = 0
    assistant_count = 0
    tool_message_count = 0
    has_tool_calls = False
    last_assistant_empty = False
    pending_tool = False
    open_calls = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "user" and _message_text(message):
            user_count += 1
        elif role == "assistant":
            assistant_count += 1
            last_assistant_empty = not _message_text(message) and not _has_tool_calls(message)
            if _has_tool_calls(message):
                has_tool_calls = True
                calls = message.get("tool_calls")
                open_calls += len(calls) if isinstance(calls, list) else 1
        elif role == "tool":
            tool_message_count += 1
            if open_calls:
                open_calls -= 1
    pending_tool = open_calls > 0

    leaf_error = meta.get("leaf_response_error")
    incomplete = meta.get("leaf_incomplete_details")
    finish_reasons = meta.get("finish_reasons")
    finish_has_error = False
    if isinstance(finish_reasons, list):
        finish_has_error = any(
            isinstance(item, str) and item.lower() in {"error", "cancelled", "length"}
            for item in finish_reasons
        )
    request_count = meta.get("source_request_count")
    source_request_count = request_count if isinstance(request_count, int) else 0
    has_failure = bool(
        leaf_error
        or incomplete
        or last_assistant_empty
        or pending_tool
        or finish_has_error
    )
    hints: list[str] = []
    if leaf_error:
        hints.append("LEAF_RESPONSE_ERROR")
    if incomplete:
        hints.append("LEAF_INCOMPLETE")
    if last_assistant_empty:
        hints.append("LAST_ASSISTANT_EMPTY")
    if pending_tool:
        hints.append("PENDING_TOOL_RESULT")
    if finish_has_error:
        hints.append("FINISH_REASON_ERROR")
    record_id = value.get("record_id")
    capture_id = meta.get("capture_id")
    return {
        "source_ref": source_ref,
        "line_number": line_number,
        "line_sha256": line_sha256,
        "record_id": record_id if isinstance(record_id, str) else None,
        "capture_id": capture_id if isinstance(capture_id, str) else None,
        "thread_id": meta.get("thread_id") if isinstance(meta.get("thread_id"), str) else None,
        "parse_ok": True,
        "has_user_task_like_turn": user_count > 0,
        "has_agent_attempt": assistant_count > 0 or has_tool_calls or tool_message_count > 0,
        "has_tool_activity": has_tool_calls or tool_message_count > 0,
        "has_failure_or_unfinished_signal": has_failure,
        "message_count": len(messages),
        "user_count": user_count,
        "assistant_count": assistant_count,
        "tool_message_count": tool_message_count,
        "source_request_count": source_request_count,
        "last_assistant_empty": last_assistant_empty,
        "leaf_response_status": (
            meta.get("leaf_response_status")
            if isinstance(meta.get("leaf_response_status"), str)
            else None
        ),
        "reason_hints": tuple(hints),
    }
