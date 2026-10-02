"""原始 session 的消息、工具索引与来源持久化。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.reconstruction.env_replay import normalize_file_ops


class ReconstructionSourceError(ValueError):
    """无法从原始 session 构造重建源。"""


def indexed_session(raw_session: dict[str, Any]) -> dict[str, Any]:
    """完整原文主动交付给重建者，外层索引不覆盖原消息中的同名字段。"""
    if not isinstance(raw_session, dict):
        raise ReconstructionSourceError("原始 session 必须是对象")
    messages = raw_session.get("messages", [])
    if not isinstance(messages, list):
        raise ReconstructionSourceError("原始 session.messages 必须是列表")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ReconstructionSourceError(f"原始 session.messages[{index}] 必须是对象")
    return {
        "session_fields": copy.deepcopy({
            key: value for key, value in raw_session.items() if key != "messages"
        }),
        "messages": [{"message_index": index, "message": copy.deepcopy(message)}
                     for index, message in enumerate(messages)],
    }


def source_session_message_indices(
    messages: list[dict[str, Any]], raw_session: dict[str, Any],
) -> tuple[int, ...]:
    """逐值核对完整原轨迹，兼容 terminal 行和 search JSON 两种交付封装。"""
    expected = indexed_session(raw_session)
    found: list[int] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ReconstructionSourceError("作者会话含无效消息")
        if message.get("role") != "user":
            continue
        content = message_text(message)
        candidates = [line.removeprefix("SOURCE_SESSION=")
                      for line in content.splitlines() if line.startswith("SOURCE_SESSION=")]
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("SOURCE_SESSION") == expected:
            found.append(index)
            continue
        for value in candidates:
            try:
                matched = json.loads(value) == expected
            except json.JSONDecodeError:
                matched = False
            if matched:
                found.append(index)
                break
    return tuple(found)


def message_text(message: dict[str, Any]) -> str:
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


def _result_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """保留原始 content 槽位与文本空白，供并行结果按位置对应。"""
    content = message.get("content")
    if content is None:
        return []
    blocks = content if isinstance(content, list) else [content]
    result: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        text = block if isinstance(block, str) else None
        if isinstance(block, dict) and block.get("type") in (None, "text", "input_text", "output_text"):
            for field in ("text", "value"):
                if isinstance(block.get(field), str):
                    text = block[field]
                    break
        result.append({"index": index, "text": text})
    return result


def _tool_result_state(message: dict[str, Any]) -> dict[str, Any]:
    """保留原始成功或拒绝状态，由 Replay 统一判断是否可用。"""
    return {
        key: message[key]
        for key in ("is_error", "cleared", "status", "result_status")
        if key in message
    }


def _role(message: Any) -> str:
    return str(message.get("role", "")) if isinstance(message, dict) else ""


def _tool_name(call: dict[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return str(call.get("name") or "")


def _tool_arguments(call: dict[str, Any]) -> Any:
    function = call.get("function")
    raw = function.get("arguments") if isinstance(function, dict) else call.get("arguments")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _call_id(call: dict[str, Any]) -> str:
    value = call.get("id") or call.get("tool_call_id")
    return str(value) if value else ""


def _span_at(spans: list[Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for span in spans:
        for index in range(span.message_start, span.message_end):
            result[index] = span.span_id
    return result


def tool_timeline(messages: list[dict[str, Any]], spans: list[Any]) -> list[dict[str, Any]]:
    index_to_span = _span_at(spans)
    pending: dict[str, dict[str, Any]] = {}
    timeline: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if _role(message) == "assistant":
            for call in message.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                call_id = _call_id(call)
                item = {
                    "call_id": call_id,
                    "name": _tool_name(call),
                    "arguments": _tool_arguments(call),
                    "result_text": None,
                    "result_blocks": [],
                    "pending": True,
                    "span_id": index_to_span.get(index),
                    "assistant_message_index": index,
                }
                timeline.append(item)
                if call_id:
                    pending[call_id] = item
        if _role(message) == "tool":
            call_id = str(message.get("tool_call_id") or "")
            # 已配对返回不可被后续旧 ID 覆盖；无法匹配的返回单独保留。
            item = pending.pop(call_id, None)
            if item is None:
                timeline.append({
                    "call_id": call_id,
                    "name": str(message.get("name") or message.get("tool_name") or ""),
                    "arguments": None,
                    "result_text": message_text(message),
                    "result_blocks": _result_blocks(message),
                    "pending": False,
                    "span_id": index_to_span.get(index),
                    "orphan_tool_result": True,
                    "tool_message_index": index,
                    **_tool_result_state(message),
                })
            else:
                item["result_text"] = message_text(message)
                item["result_blocks"] = _result_blocks(message)
                item.update(_tool_result_state(message))
                item["pending"] = False
                item["tool_message_index"] = index
    return timeline


def timeline_has_file_ops(timeline: list[dict[str, Any]]) -> bool:
    return any(op.get("kind") in {"read", "write"} for op in normalize_file_ops(timeline))


def span_records(spans: list[Any], messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for span in spans:
        users = [message_text(messages[i]) for i in span.user_message_indices if 0 <= i < len(messages)]
        result[span.span_id] = {
            "span_id": span.span_id,
            "message_start": span.message_start,
            "message_end": span.message_end,
            "message_indices": list(range(span.message_start, span.message_end)),
            "user_message_indices": list(span.user_message_indices),
            "user_texts": [x for x in users if x],
        }
    return result


def load_raw_line(input_path: str | Path, *, line_number: int, line_sha256: str | None = None) -> str:
    path = Path(input_path)
    if not path.is_file():
        raise ReconstructionSourceError(f"原始 JSONL 不存在：{path}")
    with path.open(encoding="utf-8") as handle:
        for index, raw in enumerate(handle, start=1):
            if index == line_number:
                if line_sha256 and hashlib.sha256(raw.encode()).hexdigest() != line_sha256:
                    raise ReconstructionSourceError(f"line_number={line_number} 的 sha256 与输入清单不一致")
                return raw
    raise ReconstructionSourceError(f"原始 JSONL 没有第 {line_number} 行")


def write_reconstruction_source(source: dict[str, Any], output_dir: str | Path) -> Path:
    root = Path(output_dir); root.mkdir(parents=True, exist_ok=True)
    path = root / "reconstruction_source.json"
    path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
