"""Hermes、Anthropic 与 ATIF 证据的规范化和对账。"""

from __future__ import annotations

import base64
import binascii
import copy
import datetime as _dt
import hashlib
import json
import shutil
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .capture import assemble_anthropic_sse
from .exceptions import TrajectoryCaptureError

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

JsonSource = Path | str | Mapping[str, Any] | Sequence[Mapping[str, Any]]


class EvidenceError(TrajectoryCaptureError):
    """证据结构无法规范化或对账。"""


def _utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _validate_raw_body(value: Any, *, location: str) -> tuple[list[dict[str, Any]], bytes | None]:
    errors: list[dict[str, Any]] = []
    if not isinstance(value, Mapping):
        return [{"code": "CAPTURE_BODY_SHAPE", "location": location}], None
    encoded = value.get("raw_base64")
    if not isinstance(encoded, str):
        return [{"code": "CAPTURE_RAW_BODY_MISSING", "location": location}], None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return [{"code": "CAPTURE_RAW_BODY_BASE64", "location": location}], None
    if value.get("size_bytes") != len(raw):
        errors.append({"code": "CAPTURE_BODY_SIZE_MISMATCH", "location": location})
    if value.get("sha256") != hashlib.sha256(raw).hexdigest():
        errors.append({"code": "CAPTURE_BODY_HASH_MISMATCH", "location": location})
    if "json" in value:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append({"code": "CAPTURE_BODY_JSON_MISMATCH", "location": location})
        else:
            if parsed != value.get("json"):
                errors.append({"code": "CAPTURE_BODY_JSON_MISMATCH", "location": location})
    return errors, raw


def _superseded_transport_exchange_ids(exchanges: Sequence[Any]) -> set[str]:
    """Recover only empty transport failures followed by the same raw request.

    Every attempt still undergoes capture validation. A retry only discharges
    completeness errors; it cannot erase received content or evidence defects.
    """
    successful_after: dict[tuple[Any, ...], _dt.datetime] = {}
    superseded: set[str] = set()
    for exchange in reversed(exchanges):
        if not isinstance(exchange, Mapping):
            continue
        request_errors, request_raw = _validate_raw_body(
            exchange.get("request"), location="retry:request"
        )
        headers = exchange.get("request_headers")
        if (
            request_errors
            or request_raw is None
            or not isinstance(headers, Mapping)
            or any(
                not isinstance(exchange.get(name), str)
                for name in ("method", "path", "upstream_url")
            )
        ):
            continue
        try:
            started = _dt.datetime.fromisoformat(exchange["started_at"])
            finished = _dt.datetime.fromisoformat(exchange["finished_at"])
            if started.utcoffset() is None or finished.utcoffset() is None or finished < started:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        identity = (
            request_raw,
            exchange.get("method"),
            exchange.get("path"),
            exchange.get("upstream_url"),
            canonical_json_sha256(headers),
        )
        if (
            exchange.get("response_status") == 200
            and exchange.get("complete") is True
            and exchange.get("error_code") is None
        ):
            successful_after[identity] = started
            continue
        exchange_id = exchange.get("exchange_id")
        response = exchange.get("response")
        if not (
            isinstance(exchange_id, str)
            and identity in successful_after
            and finished <= successful_after[identity]
            and exchange.get("response_status") == 200
            and exchange.get("streaming") is True
            and exchange.get("complete") is False
            and exchange.get("error_code") == "UPSTREAM_TRANSPORT_ERROR"
            and exchange.get("sse_event_count") == 0
            and isinstance(response, Mapping)
            and response.get("message") == assemble_anthropic_sse([])
        ):
            continue
        body_errors, response_raw = _validate_raw_body(
            response.get("body"), location="retry:response"
        )
        if not body_errors and response_raw == b"":
            superseded.add(exchange_id)
    return superseded


def is_assistant_response(call: Any) -> bool:
    """完整成功的 Anthropic assistant 回复；HTTP 错误不是额外一轮。"""
    if not isinstance(call, Mapping):
        return False
    response = call.get("response")
    status = call.get("status")
    return (
        isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300
        and call.get("complete") is True and not call.get("error_code")
        and isinstance(response, Mapping) and response.get("type") == "message"
        and response.get("role") == "assistant"
        and isinstance(response.get("content"), list) and bool(response["content"])
        and all(isinstance(block, Mapping) for block in response["content"])
    )


def response_history_matches(actual: Any, expected: Any) -> bool:
    """仅容许既有 adapter 的 thinking/cache/signature 运输投影。"""
    if actual == expected:
        return True
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    projected = []
    injected = 0
    for block in actual:
        if not isinstance(block, Mapping):
            projected.append(block)
            continue
        normalized = dict(block)
        if normalized.get("type") == "thinking":
            continue
        if "cache_control" in normalized:
            if (
                normalized.get("type") != "text"
                or normalized["cache_control"] != {"type": "ephemeral"}
            ):
                return False
            injected += 1
            normalized.pop("cache_control")
        projected.append(normalized)
    expected_projection = []
    for block in expected:
        if not isinstance(block, Mapping):
            expected_projection.append(block)
        elif block.get("type") != "thinking":
            normalized = dict(block)
            normalized.pop("signature", None)
            expected_projection.append(normalized)
    return injected <= 1 and projected == expected_projection


_SUMMARY_END = (
    "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
)


def compaction_summary_text(text: Any) -> bool:
    return (
        isinstance(text, str)
        and text.startswith(("[CONTEXT COMPACTION — REFERENCE ONLY]", "[CONTEXT SUMMARY]:"))
        and _SUMMARY_END in text
    )


def _summary_blocks(message: Any) -> list[str]:
    if not isinstance(message, Mapping) or message.get("role") not in {"user", "assistant"}:
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [content] if compaction_summary_text(content) else []
    if not isinstance(content, list):
        return []
    return [
        str(block["text"]) for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
        and compaction_summary_text(block.get("text"))
    ]


def request_assistant_contents(messages: Any) -> list[Any]:
    if not isinstance(messages, list):
        return []
    result = []
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        # 独立 assistant 摘要没有对应模型回复；合并了真实尾部的摘要不猜测拆分。
        if _summary_blocks(message) and isinstance(content, list) and all(
            isinstance(block, Mapping) and block.get("type") == "text"
            and compaction_summary_text(block.get("text"))
            and str(block["text"]).rstrip().endswith(_SUMMARY_END)
            for block in content
        ):
            continue
        result.append(content)
    return result


def compaction_windows(calls: Any) -> dict[int, dict[str, list[int]]]:
    """只记录由新摘要和成功响应历史收缩共同证明的压缩边界。"""
    if not isinstance(calls, list):
        return {}
    windows: dict[int, dict[str, list[int]]] = {}
    visible: list[int] = []
    previous_messages: list[Any] = []
    seen_summaries: set[str] = set()
    for index, call in enumerate(calls):
        request = call.get("request") if isinstance(call, Mapping) else None
        messages = request.get("messages") if isinstance(request, Mapping) else None
        messages = messages if isinstance(messages, list) else []
        summaries = {text for message in messages for text in _summary_blocks(message)}
        new_summary = any(
            text not in seen_summaries
            for message in messages[1:] for text in _summary_blocks(message)
        )
        actual = request_assistant_contents(messages)
        if (
            new_summary and visible and messages and previous_messages
            and messages[0] == previous_messages[0]
            and len(actual) < len(visible)
            and len(messages) < len(previous_messages) + 2
        ):
            retained = []
            cursor = 0
            for content in actual:
                while cursor < len(visible) and not response_history_matches(
                    content, calls[visible[cursor]]["response"]["content"]
                ):
                    cursor += 1
                if cursor == len(visible):
                    break
                retained.append(visible[cursor])
                cursor += 1
            # 压缩必须保留最近真实回复；未知 assistant 内容不得靠摘要兜底。
            if len(retained) == len(actual) and retained and retained[-1] == visible[-1]:
                windows[index] = {
                    "retained": retained,
                    "compacted": [item for item in visible if item not in retained],
                }
                visible = list(retained)
        seen_summaries.update(summaries)
        previous_messages = messages
        if is_assistant_response(call):
            visible.append(index)
    return windows


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int | None]:
    """保留 Anthropic 四个 usage 字段，不将未知值改写为零。"""

    normalized: dict[str, int | None] = {}
    usage = usage or {}
    if isinstance(usage.get("total"), Mapping):
        usage = usage["total"]  # type: ignore[assignment]
    for field_name in USAGE_FIELDS:
        value = usage.get(field_name)
        if value is None:
            normalized[field_name] = None
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EvidenceError(f"usage.{field_name} 必须是非负整数或 null")
        else:
            normalized[field_name] = value
    return normalized


def aggregate_usage(
    usages: Iterable[Mapping[str, Any] | None],
) -> dict[str, int | None]:
    """严格汇总 usage：任一调用缺失某字段，该总计仍为 null。"""

    normalized = [normalize_usage(usage) for usage in usages]
    if not normalized:
        return {field_name: None for field_name in USAGE_FIELDS}
    result: dict[str, int | None] = {}
    for field_name in USAGE_FIELDS:
        values = [item[field_name] for item in normalized]
        result[field_name] = (
            sum(value for value in values if value is not None)
            if all(value is not None for value in values)
            else None
        )
    return result


def atif_prompt_tokens(usage: Mapping[str, int | None]) -> int | None:
    """按 input + cache creation + cache read 计算 ATIF prompt_tokens。"""

    values = [
        usage.get("input_tokens"),
        usage.get("cache_creation_input_tokens"),
        usage.get("cache_read_input_tokens"),
    ]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values if value is not None)


def _load_json_source(source: JsonSource) -> Any:
    if isinstance(source, Mapping):
        return copy.deepcopy(dict(source))
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)):
        return copy.deepcopy(list(source))
    path = Path(source)
    if not path.exists():
        raise EvidenceError(f"证据文件不存在: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        records = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EvidenceError(f"{path}:{line_number} 不是有效 JSONL") from exc
        return records
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{path} 不是有效 JSON") from exc


def _records(source: JsonSource) -> list[dict[str, Any]]:
    value = _load_json_source(source)
    if isinstance(value, Mapping):
        for key in ("messages", "events", "exchanges"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [copy.deepcopy(dict(item)) for item in nested if isinstance(item, Mapping)]
        return [copy.deepcopy(dict(value))]
    if not isinstance(value, list):
        raise EvidenceError("证据必须是 JSON 对象或数组")
    return [copy.deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]


def _exchange_request(exchange: Mapping[str, Any]) -> dict[str, Any]:
    request = exchange.get("request")
    if isinstance(request, Mapping):
        parsed = request.get("json")
        if isinstance(parsed, Mapping):
            return copy.deepcopy(dict(parsed))
        # 允许调用方直接传入 Anthropic request body。
        if "messages" in request or "model" in request:
            return copy.deepcopy(dict(request))
    return {}


def _exchange_response(exchange: Mapping[str, Any]) -> dict[str, Any]:
    response = exchange.get("response")
    if not isinstance(response, Mapping):
        return {}
    message = response.get("message")
    if isinstance(message, Mapping):
        return copy.deepcopy(dict(message))
    parsed = response.get("json")
    if isinstance(parsed, Mapping):
        return copy.deepcopy(dict(parsed))
    body = response.get("body")
    if isinstance(body, Mapping) and isinstance(body.get("json"), Mapping):
        return copy.deepcopy(dict(body["json"]))
    if "content" in response or "usage" in response:
        return copy.deepcopy(dict(response))
    return {}


def _session_messages(session: JsonSource) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    value = _load_json_source(session)
    metadata: dict[str, Any] = {}
    if isinstance(value, Mapping):
        raw_messages = value.get("messages")
        if not isinstance(raw_messages, list):
            raw_messages = value.get("conversation")
        if not isinstance(raw_messages, list):
            raise EvidenceError("Hermes session 对象缺少 messages")
        metadata = {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in {"messages", "conversation"}
        }
    elif isinstance(value, list):
        # hermes-session.jsonl 是单行的 {meta,messages} 对象，而通用
        # JSONL loader 会将它表示为只含一个元素的数组。
        if (
            len(value) == 1
            and isinstance(value[0], Mapping)
            and isinstance(value[0].get("messages"), list)
        ):
            raw_messages = value[0]["messages"]
            metadata = {
                key: copy.deepcopy(item) for key, item in value[0].items() if key != "messages"
            }
        else:
            raw_messages = value
    else:
        raise EvidenceError("Hermes session 必须是消息数组或含 messages 的对象")
    return (
        [copy.deepcopy(dict(item)) for item in raw_messages if isinstance(item, Mapping)],
        metadata,
    )


def _content_text(content: Any, *, exclude_thinking: bool = False) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        block_type = content.get("type")
        if exclude_thinking and block_type in {
            "thinking",
            "redacted_thinking",
            "tool_use",
            "tool_result",
        }:
            return ""
        for key in ("text", "content", "output"):
            if isinstance(content.get(key), str):
                return str(content[key])
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    if isinstance(content, list):
        return "".join(_content_text(item, exclude_thinking=exclude_thinking) for item in content)
    return str(content)


def _assistant_reasoning(message: Mapping[str, Any]) -> str | None:
    value = message.get("anthropic_reasoning_content")
    if isinstance(value, str) and value:
        return value
    parts: list[str] = []
    response = message.get("anthropic_response")
    content = response.get("content") if isinstance(response, Mapping) else None
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "thinking":
                continue
            thinking = block.get("thinking")
            if isinstance(thinking, str):
                parts.append(thinking)
    return "".join(parts) or None


def _anthropic_tools_to_atif(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    converted: list[dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "function" and isinstance(item.get("function"), Mapping):
            converted.append(copy.deepcopy(dict(item)))
            continue
        function: dict[str, Any] = {
            "name": str(item.get("name", "")),
            "parameters": copy.deepcopy(item.get("input_schema") or {"type": "object"}),
        }
        if item.get("description") is not None:
            function["description"] = str(item["description"])
        converted.append({"type": "function", "function": function})
    return converted


def _tool_calls(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            function = function if isinstance(function, Mapping) else {}
            call_id = str(call.get("id") or call.get("tool_call_id") or "")
            if not call_id or call_id in seen:
                continue
            arguments = function.get("arguments", call.get("arguments", {}))
            extra: dict[str, Any] = {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    extra["raw_arguments"] = arguments
                    arguments = {}
            if not isinstance(arguments, Mapping):
                extra["raw_arguments"] = copy.deepcopy(arguments)
                arguments = {}
            projected = {
                "tool_call_id": call_id,
                "function_name": str(function.get("name") or call.get("name") or ""),
                "arguments": copy.deepcopy(dict(arguments)),
            }
            if extra:
                projected["extra"] = extra
            calls.append(projected)
            seen.add(call_id)
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                continue
            call_id = str(block.get("id") or "")
            if not call_id or call_id in seen:
                continue
            arguments = block.get("input")
            extra = {}
            if not isinstance(arguments, Mapping):
                extra["raw_arguments"] = copy.deepcopy(arguments)
                arguments = {}
            projected = {
                "tool_call_id": call_id,
                "function_name": str(block.get("name") or ""),
                "arguments": copy.deepcopy(dict(arguments)),
            }
            if extra:
                projected["extra"] = extra
            calls.append(projected)
            seen.add(call_id)
    return calls


def _tool_results(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    role = message.get("role")
    results: list[dict[str, Any]] = []
    if role == "tool":
        results.append(
            {
                "source_call_id": str(message.get("tool_call_id") or message.get("id") or ""),
                "content": _content_text(message.get("content")),
            }
        )
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                continue
            result = {
                "source_call_id": str(block.get("tool_use_id") or block.get("tool_call_id") or ""),
                "content": _content_text(block.get("content")),
            }
            extra = {
                key: copy.deepcopy(value)
                for key, value in block.items()
                if key not in {"type", "tool_use_id", "tool_call_id", "content"}
            }
            if extra:
                result["extra"] = extra
            results.append(result)
    return results


def _first_user_content(messages: Iterable[Mapping[str, Any]]) -> Any:
    for message in messages:
        if message.get("role") == "user":
            return copy.deepcopy(message.get("content"))
    return None


def _user_prompt_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            return None
        text = block.get("text")
        if not isinstance(text, str):
            return None
        texts.append(text)
    return "".join(texts)


def build_full_trajectory(
    exchanges: JsonSource,
    hermes_session: JsonSource | None = None,
    *,
    task_input: JsonSource | None = None,
    metadata: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构建同时保留模型边界和工具边界的无损规范化视图。"""

    if hermes_session is None:
        logs_dir = Path(exchanges) if isinstance(exchanges, (Path, str)) else None
        if logs_dir is None or not logs_dir.is_dir():
            raise EvidenceError("单参数 build_full_trajectory 需要一个包含采集文件的日志目录")
        exchange_path = logs_dir / "anthropic-exchanges.jsonl"
        if not exchange_path.is_file():
            exchange_path = logs_dir / "capture" / "anthropic-exchanges.jsonl"
        session_path = logs_dir / "hermes-session.jsonl"
        if not session_path.is_file():
            session_path = logs_dir / "hermes-result.json"
        task_input_path = logs_dir / "task-input.json"
        if task_input is None and task_input_path.is_file():
            task_input = task_input_path
        exchanges = exchange_path
        hermes_session = session_path
    exchange_records = _records(exchanges)
    messages, session_metadata = _session_messages(hermes_session)
    normalized_task_input: dict[str, Any] | None = None
    if task_input is not None:
        loaded_task_input = _load_json_source(task_input)
        if not isinstance(loaded_task_input, Mapping):
            raise EvidenceError("task-input.json 必须是 JSON 对象")
        normalized_task_input = copy.deepcopy(dict(loaded_task_input))
    first_request = _exchange_request(exchange_records[0]) if exchange_records else {}
    system_prompt = first_request.get("system")
    if system_prompt is None:
        for message in messages:
            if message.get("role") == "system":
                system_prompt = copy.deepcopy(message.get("content"))
                break
    tools = copy.deepcopy(first_request.get("tools") or [])
    calls: list[dict[str, Any]] = []
    call_usages: list[dict[str, int | None]] = []
    for index, exchange in enumerate(exchange_records):
        request = _exchange_request(exchange)
        response = _exchange_response(exchange)
        usage = normalize_usage(response.get("usage") if isinstance(response, Mapping) else None)
        call_usages.append(usage)
        calls.append(
            {
                "call_index": index,
                "exchange_id": exchange.get("exchange_id"),
                "request_id": exchange.get("request_id"),
                "started_at": exchange.get("started_at"),
                "finished_at": exchange.get("finished_at"),
                "duration_ms": exchange.get("duration_ms"),
                "status": exchange.get("response_status"),
                "streaming": bool(exchange.get("streaming")),
                "complete": bool(exchange.get("complete")),
                "error_code": exchange.get("error_code"),
                "request": request,
                "response": response,
                "usage": usage,
                "raw_exchange_sha256": canonical_json_sha256(exchange),
            }
        )
    # Hermes session 是工具执行边界，Anthropic response 是模型边界。在不
    # 改写 session 原字段的前提下，按 tool_call_id 关联两者。上下文压缩后
    # capture 仍保存完整历史，但 Hermes 只保留压缩后的可见消息，不能再按
    # assistant 的位置绑定 capture。
    capture_tool_ids: dict[int, frozenset[str]] = {}
    capture_text_hashes: dict[int, str] = {}
    for index, call in enumerate(calls):
        response = call.get("response")
        response = response if isinstance(response, Mapping) else {}
        capture_tool_ids[index] = frozenset(
            item["tool_call_id"] for item in _tool_calls(response)
        )
        capture_text_hashes[index] = canonical_json_sha256(
            _content_text(response.get("content"), exclude_thinking=True)
        )
    used_call_indices: set[int] = set()
    assistant_call_indices: list[int | None] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        message_tool_ids = frozenset(
            item["tool_call_id"] for item in _tool_calls(message)
        )
        candidates = [
            index
            for index, tool_ids in capture_tool_ids.items()
            if index not in used_call_indices
            and message_tool_ids
            and tool_ids == message_tool_ids
        ]
        if not candidates and not message_tool_ids:
            message_hash = canonical_json_sha256(
                _content_text(message.get("content"), exclude_thinking=True)
            )
            candidates = [
                index
                for index, text_hash in capture_text_hashes.items()
                if index not in used_call_indices
                and not capture_tool_ids[index]
                and text_hash == message_hash
            ]
        # 最后的无 tools assistant 可能只有一份压缩后的文本边界；只在
        # 没有稳定 ID/hash 时使用未占用 capture，绝不重排已经绑定的工具调用。
        if not candidates:
            candidates = [
                index for index in range(len(calls)) if index not in used_call_indices
            ]
        call_index = candidates[0] if candidates else None
        assistant_call_indices.append(call_index)
        if call_index is None:
            continue
        used_call_indices.add(call_index)
        message["_anthropic_call_index"] = call_index
        response = calls[call_index]["response"]
        message["anthropic_response"] = copy.deepcopy(response)
        message["anthropic_usage"] = copy.deepcopy(calls[call_index]["usage"])
        thinking_parts = []
        content = response.get("content") if isinstance(response, Mapping) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "thinking":
                    thinking = block.get("thinking")
                    if isinstance(thinking, str):
                        thinking_parts.append(thinking)
        if thinking_parts:
            message["anthropic_reasoning_content"] = "".join(thinking_parts)
            if not message.get("reasoning_content") and not message.get("reasoning"):
                message["reasoning_content"] = "".join(thinking_parts)

    first_system_hash = canonical_json_sha256(system_prompt)
    first_tools_hash = canonical_json_sha256(tools)
    harness_drift: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        request = call.get("request")
        if not isinstance(request, Mapping):
            continue
        system_hash = canonical_json_sha256(request.get("system"))
        tools_hash = canonical_json_sha256(request.get("tools") or [])
        response = call.get("response")
        content = response.get("content") if isinstance(response, Mapping) else None
        has_tool_use = isinstance(content, list) and any(
            isinstance(block, Mapping) and block.get("type") in {"tool_use", "tool_call"}
            for block in content
        )
        terminal_without_tools = (
            index == len(calls) - 1
            and not request.get("tools")
            and not request.get("tool_choice")
            and not has_tool_use
            and bool(call.get("complete"))
        )
        if terminal_without_tools:
            # Hermes deliberately sends a final text-only request after the
            # last tool result. Its adapter drops tool definitions and uses a
            # compact system prompt; this is a normal terminal boundary.
            continue
        if system_hash != first_system_hash or tools_hash != first_tools_hash:
            harness_drift.append(
                {
                    "call_index": index,
                    "code": "HARNESS_DRIFT",
                    "system_prompt_sha256": system_hash,
                    "tool_definitions_sha256": tools_hash,
                }
            )
    model = first_request.get("model")
    full_metadata = copy.deepcopy(dict(metadata or {}))
    full_metadata.setdefault("provider", "anthropic")
    full_metadata.setdefault("model", model)
    if calls and full_metadata.get("endpoint") is None:
        full_metadata["endpoint"] = exchange_records[0].get("upstream_url")
    nested_meta = session_metadata.get("meta")
    nested_meta = nested_meta if isinstance(nested_meta, Mapping) else {}
    rendered_prompt: str | None = None
    if normalized_task_input is not None:
        instruction_record = normalized_task_input.get("instruction")
        instruction_record = (
            instruction_record if isinstance(instruction_record, Mapping) else {}
        )
        rendered_record = instruction_record.get("rendered_user_prompt")
        if isinstance(rendered_record, Mapping):
            value = rendered_record.get("content")
            rendered_prompt = value if isinstance(value, str) else None
    session_first_user = _user_prompt_text(
        _first_user_content(message for message in messages if isinstance(message, Mapping))
    )
    request_messages = first_request.get("messages")
    request_messages = request_messages if isinstance(request_messages, list) else []
    request_first_user = _user_prompt_text(
        _first_user_content(
            message for message in request_messages if isinstance(message, Mapping)
        )
    )
    input_alignment = {
        "task_input_present": normalized_task_input is not None,
        "rendered_user_prompt_present": rendered_prompt is not None,
        "hermes_first_user_matches": (
            rendered_prompt is not None and session_first_user == rendered_prompt
        ),
        "anthropic_first_user_matches": (
            rendered_prompt is not None and request_first_user == rendered_prompt
        ),
        "system_prompt_captured": system_prompt is not None,
        "tool_definitions_captured": isinstance(tools, list) and bool(tools),
    }
    task_input_hash = (
        canonical_json_sha256(normalized_task_input)
        if normalized_task_input is not None
        else None
    )
    return {
        "schema_version": "traceforge-lossless-trajectory-v1",
        "created_at": _utc_now(),
        "session_id": session_metadata.get("session_id")
        or session_metadata.get("id")
        or nested_meta.get("session_id")
        or full_metadata.get("session_id"),
        "model": model,
        "system_prompt": copy.deepcopy(system_prompt),
        "tools": tools,
        "task_input": normalized_task_input,
        "input_alignment": input_alignment,
        "messages": messages,
        "anthropic_calls": calls,
        "usage": {
            "calls": call_usages,
            "total": aggregate_usage(call_usages),
        },
        "metadata": full_metadata,
        "hermes_session_metadata": session_metadata,
        "harness_drift": harness_drift,
        "artifacts": copy.deepcopy(dict(artifacts or {})),
        "hashes": {
            "system_prompt_sha256": canonical_json_sha256(system_prompt),
            "tool_definitions_sha256": canonical_json_sha256(tools),
            "messages_sha256": canonical_json_sha256(messages),
            "task_input_sha256": task_input_hash,
        },
    }


def project_atif_v17(full_trajectory: Mapping[str, Any], *, include_report: bool = False) -> Any:
    """将规范化视图投影为 ATIF v1.7，并记录每个有损合并。"""

    messages = full_trajectory.get("messages")
    if not isinstance(messages, list):
        raise EvidenceError("trajectory.full.messages 必须是数组")
    calls = full_trajectory.get("anthropic_calls")
    calls = calls if isinstance(calls, list) else []
    losses: list[dict[str, Any]] = []
    system_prompt = full_trajectory.get("system_prompt")
    tools = full_trajectory.get("tools")
    task_input = full_trajectory.get("task_input")
    task_input = task_input if isinstance(task_input, Mapping) else None
    atif_tools = _anthropic_tools_to_atif(tools)
    if tools and atif_tools != tools:
        losses.append(
            {
                "field": "tools",
                "reason": "Anthropic tool schema converted to ATIF/OpenAI function schema",
                "source_sha256": canonical_json_sha256(tools),
            }
        )
    if calls:
        losses.append(
            {
                "field": "anthropic_calls",
                "reason": (
                    "raw Anthropic request/response envelopes remain in trajectory.full.json; "
                    "ATIF carries normalized steps and exchange IDs"
                ),
                "source_sha256": canonical_json_sha256(calls),
            }
        )
    if task_input is not None:
        losses.append(
            {
                "field": "task_input.workspace",
                "reason": (
                    "initial workspace manifest remains in trajectory.full.json; "
                    "ATIF carries its digest and the rendered user step"
                ),
                "source_sha256": canonical_json_sha256(task_input),
            }
        )

    task_instruction = task_input.get("instruction") if task_input is not None else None
    task_instruction = task_instruction if isinstance(task_instruction, Mapping) else {}
    rendered_record = task_instruction.get("rendered_user_prompt")
    rendered_record = rendered_record if isinstance(rendered_record, Mapping) else {}
    workspace_record = task_input.get("workspace") if task_input is not None else None
    workspace_record = workspace_record if isinstance(workspace_record, Mapping) else {}

    metadata = full_trajectory.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    agent_version = metadata.get("hermes_version") or metadata.get("agent_version") or "unknown"
    atif: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "agent": {
            "name": "hermes",
            "version": str(agent_version),
            "model_name": full_trajectory.get("model") or metadata.get("model"),
            "tool_definitions": atif_tools,
            "extra": {
                "provider": metadata.get("provider", "anthropic"),
                "system_prompt_sha256": full_trajectory.get("hashes", {}).get(
                    "system_prompt_sha256"
                )
                if isinstance(full_trajectory.get("hashes"), Mapping)
                else None,
            },
        },
        "steps": [],
        "extra": {
            "source_schema": full_trajectory.get("schema_version"),
            "tool_definitions_sha256": canonical_json_sha256(tools or []),
            "task_input_sha256": (
                canonical_json_sha256(task_input) if task_input is not None else None
            ),
            "rendered_user_prompt_sha256": rendered_record.get("sha256"),
            "workspace_initial_tree_sha256": workspace_record.get("tree_sha256"),
            "harness_drift": copy.deepcopy(full_trajectory.get("harness_drift") or []),
        },
    }
    if full_trajectory.get("session_id") is not None:
        atif["session_id"] = str(full_trajectory["session_id"])
    trajectory_id = metadata.get("trajectory_id") or metadata.get("trial_id")
    if trajectory_id is not None:
        atif["trajectory_id"] = str(trajectory_id)

    steps: list[dict[str, Any]] = atif["steps"]
    call_to_step: dict[str, dict[str, Any]] = {}
    assistant_index = 0

    def append_step(
        source: str, message: str, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "source": source,
            "message": message,
        }
        if extra:
            step["extra"] = extra
        steps.append(step)
        return step

    if system_prompt is not None:
        if not isinstance(system_prompt, str):
            losses.append(
                {
                    "field": "system_prompt",
                    "reason": "structured Anthropic system blocks flattened to text",
                    "source_sha256": canonical_json_sha256(system_prompt),
                }
            )
        append_step("system", _content_text(system_prompt))

    for message_index, raw_message in enumerate(messages):
        if not isinstance(raw_message, Mapping):
            losses.append({"field": f"messages[{message_index}]", "reason": "non-object omitted"})
            continue
        role = raw_message.get("role")
        tool_results = _tool_results(raw_message)
        if tool_results:
            for result in tool_results:
                source_call_id = result.get("source_call_id")
                step = call_to_step.get(str(source_call_id))
                if step is None:
                    losses.append(
                        {
                            "field": f"messages[{message_index}]",
                            "reason": "orphan tool result cannot be attached to an ATIF agent step",
                            "tool_call_id": source_call_id,
                        }
                    )
                    continue
                observation = step.setdefault("observation", {"results": []})
                observation["results"].append(result)

        if role == "tool":
            continue
        if role == "user" and tool_results:
            content = raw_message.get("content")
            non_tool_blocks = []
            if isinstance(content, list):
                non_tool_blocks = [
                    block
                    for block in content
                    if not (isinstance(block, Mapping) and block.get("type") == "tool_result")
                ]
            if not non_tool_blocks:
                continue
            message_text = _content_text(non_tool_blocks)
        else:
            message_text = _content_text(
                raw_message.get("content"), exclude_thinking=role == "assistant"
            )

        if role == "system":
            losses.append(
                {
                    "field": f"messages[{message_index}]",
                    "reason": (
                        "session system message ignored; top-level captured system "
                        "is authoritative"
                    ),
                }
            )
        elif role == "user":
            append_step("user", message_text, {"source_message_index": message_index})
        elif role == "assistant":
            raw_call_index = raw_message.get("_anthropic_call_index")
            call_index = (
                raw_call_index
                if isinstance(raw_call_index, int) and 0 <= raw_call_index < len(calls)
                else assistant_index
            )
            anthropic_response = raw_message.get("anthropic_response")
            response_content = (
                anthropic_response.get("content")
                if isinstance(anthropic_response, Mapping)
                else None
            )
            if isinstance(response_content, list) and any(
                isinstance(block, Mapping) and block.get("signature") is not None
                for block in response_content
            ):
                losses.append(
                    {
                        "field": f"messages[{message_index}].anthropic_response.content.signature",
                        "reason": (
                            "Anthropic thinking signatures are retained only in the full view"
                        ),
                    }
                )
            omitted_fields = sorted(
                key
                for key in raw_message
                if key
                not in {
                    "role",
                    "content",
                    "reasoning",
                    "reasoning_content",
                    "anthropic_reasoning_content",
                    "tool_calls",
                    "anthropic_usage",
                    "anthropic_response",
                    "_anthropic_call_index",
                }
            )
            if omitted_fields:
                losses.append(
                    {
                        "field": f"messages[{message_index}]",
                        "reason": "Hermes-native fields are not first-class ATIF fields",
                        "omitted_fields": omitted_fields,
                    }
                )
            step = append_step(
                "agent",
                message_text,
                {
                    "source_message_index": message_index,
                    "anthropic_exchange_id": calls[call_index].get("exchange_id")
                    if call_index < len(calls) and isinstance(calls[call_index], Mapping)
                    else None,
                    "anthropic_call_index": call_index,
                },
            )
            step["model_name"] = full_trajectory.get("model") or metadata.get("model")
            step["llm_call_count"] = 1
            reasoning = _assistant_reasoning(raw_message)
            if reasoning is not None:
                step["reasoning_content"] = reasoning
            projected_calls = _tool_calls(raw_message)
            if projected_calls:
                step["tool_calls"] = projected_calls
                for call in projected_calls:
                    call_id = str(call["tool_call_id"])
                    if call_id in call_to_step:
                        losses.append(
                            {
                                "field": f"messages[{message_index}].tool_calls",
                                "reason": "duplicate tool_call_id",
                                "tool_call_id": call_id,
                            }
                        )
                    call_to_step[call_id] = step
            usage = None
            if call_index < len(calls) and isinstance(calls[call_index], Mapping):
                usage = calls[call_index].get("usage")
            normalized = normalize_usage(usage if isinstance(usage, Mapping) else None)
            metrics: dict[str, Any] = {
                "extra": {
                    "anthropic_usage": normalized,
                    "cache_creation_input_tokens": normalized["cache_creation_input_tokens"],
                }
            }
            prompt_tokens = atif_prompt_tokens(normalized)
            if prompt_tokens is not None:
                metrics["prompt_tokens"] = prompt_tokens
            if normalized["output_tokens"] is not None:
                metrics["completion_tokens"] = normalized["output_tokens"]
            if normalized["cache_read_input_tokens"] is not None:
                metrics["cached_tokens"] = normalized["cache_read_input_tokens"]
            step["metrics"] = metrics
            assistant_index += 1
        else:
            losses.append(
                {
                    "field": f"messages[{message_index}].role",
                    "reason": f"unsupported role {role!r} omitted",
                }
            )

    agent_steps = [step for step in steps if step["source"] == "agent"]
    prompt_values = [step.get("metrics", {}).get("prompt_tokens") for step in agent_steps]
    completion_values = [step.get("metrics", {}).get("completion_tokens") for step in agent_steps]
    cached_values = [step.get("metrics", {}).get("cached_tokens") for step in agent_steps]
    final_metrics: dict[str, Any] = {"total_steps": len(steps)}
    if prompt_values and all(isinstance(value, int) for value in prompt_values):
        final_metrics["total_prompt_tokens"] = sum(prompt_values)
    if completion_values and all(isinstance(value, int) for value in completion_values):
        final_metrics["total_completion_tokens"] = sum(completion_values)
    if cached_values and all(isinstance(value, int) for value in cached_values):
        final_metrics["total_cached_tokens"] = sum(cached_values)
    atif["final_metrics"] = final_metrics
    report = {
        "schema_version": "traceforge-atif-projection-report-v1",
        "source_schema": full_trajectory.get("schema_version"),
        "target_schema": "ATIF-v1.7",
        "generated_at": _utc_now(),
        "losses": losses,
        "source_message_count": len(messages),
        "atif_step_count": len(steps),
        "llm_call_count": assistant_index,
        "source_sha256": canonical_json_sha256(full_trajectory),
        "projection_sha256": canonical_json_sha256(atif),
    }
    # 投影允许“已声明”的表达损失；断链和重复 ID 由 reconciliation 严格判失败。
    report["ok"] = not any(
        item.get("reason")
        in {"duplicate tool_call_id", "orphan tool result cannot be attached to an ATIF agent step"}
        for item in losses
    )
    return (atif, report) if include_report else atif


def _call_result_ids_from_messages(
    messages: Iterable[Mapping[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    calls: list[str] = []
    results: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for call in _tool_calls(message):
            call_id = str(call["tool_call_id"])
            if call_id in seen:
                duplicates.append(call_id)
            else:
                calls.append(call_id)
                seen.add(call_id)
        for result in _tool_results(message):
            result_id = str(result.get("source_call_id") or "")
            if result_id and result_id not in results:
                results.append(result_id)
    return calls, results, duplicates


def _capture_call_result_ids(
    calls: Iterable[Mapping[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    tool_calls: list[str] = []
    results: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for call in calls:
        response = call.get("response")
        if isinstance(response, Mapping):
            for projected in _tool_calls(response):
                call_id = str(projected["tool_call_id"])
                if call_id in seen:
                    duplicates.append(call_id)
                else:
                    tool_calls.append(call_id)
                    seen.add(call_id)
        request = call.get("request")
        if isinstance(request, Mapping) and isinstance(request.get("messages"), list):
            for message in request["messages"]:
                if not isinstance(message, Mapping):
                    continue
                for result in _tool_results(message):
                    result_id = str(result.get("source_call_id") or "")
                    # Anthropic 每轮会重发历史，相同 result ID 不是采集冲突。
                    if result_id and result_id not in results:
                        results.append(result_id)
    return tool_calls, results, duplicates


def _atif_call_result_ids(
    atif: Mapping[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    calls: list[str] = []
    results: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    steps = atif.get("steps")
    if not isinstance(steps, list):
        return calls, results, duplicates
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        raw_calls = step.get("tool_calls")
        if isinstance(raw_calls, list):
            for call in raw_calls:
                if not isinstance(call, Mapping):
                    continue
                call_id = str(call.get("tool_call_id") or "")
                if not call_id:
                    continue
                if call_id in seen:
                    duplicates.append(call_id)
                else:
                    calls.append(call_id)
                    seen.add(call_id)
        observation = step.get("observation")
        if isinstance(observation, Mapping) and isinstance(observation.get("results"), list):
            for result in observation["results"]:
                if not isinstance(result, Mapping):
                    continue
                result_id = str(result.get("source_call_id") or "")
                if result_id and result_id not in results:
                    results.append(result_id)
    return calls, results, duplicates


def reconcile_evidence(
    full_trajectory: Mapping[str, Any], atif: Mapping[str, Any],
    *, exchanges: JsonSource | None = None,
) -> dict[str, Any]:
    """严格对账模型调用、Hermes 事件、工具对和 ATIF 步骤。

    Hermes 在上下文压缩后只保留当前窗口的 assistant 消息，而 capture
    保留全部 Anthropic 请求。稳定的绑定键是 tool_call_id；压缩掉的
    capture 调用仍写入 full trajectory，并在 warnings 中说明。
    """

    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    messages = full_trajectory.get("messages")
    messages = messages if isinstance(messages, list) else []
    anthropic_calls = full_trajectory.get("anthropic_calls")
    anthropic_calls = anthropic_calls if isinstance(anthropic_calls, list) else []
    session_call_ids, session_result_ids, session_duplicates = _call_result_ids_from_messages(
        message for message in messages if isinstance(message, Mapping)
    )
    capture_call_ids, capture_result_ids, capture_duplicates = _capture_call_result_ids(
        call for call in anthropic_calls if isinstance(call, Mapping)
    )
    atif_call_ids, atif_result_ids, atif_duplicates = _atif_call_result_ids(atif)

    for boundary, duplicates in (
        ("capture", capture_duplicates),
        ("hermes", session_duplicates),
        ("atif", atif_duplicates),
    ):
        if duplicates:
            issues.append(
                {
                    "code": "DUPLICATE_TOOL_CALL_ID",
                    "boundary": boundary,
                    "ids": sorted(set(duplicates)),
                }
            )
    boundaries = {
        "capture": (set(capture_call_ids), set(capture_result_ids)),
        "hermes": (set(session_call_ids), set(session_result_ids)),
        "atif": (set(atif_call_ids), set(atif_result_ids)),
    }
    for boundary, (call_ids, result_ids) in boundaries.items():
        missing = sorted(call_ids - result_ids)
        orphan = sorted(result_ids - call_ids)
        if missing:
            issues.append({"code": "MISSING_TOOL_RESULT", "boundary": boundary, "ids": missing})
        if orphan:
            issues.append({"code": "ORPHAN_TOOL_RESULT", "boundary": boundary, "ids": orphan})

    assistant_messages = [
        message
        for message in messages
        if isinstance(message, Mapping) and message.get("role") == "assistant"
    ]
    atif_agent_steps = [
        step
        for step in (atif.get("steps") or [])
        if isinstance(step, Mapping) and step.get("source") == "agent"
    ]
    represented_indices: list[int] = []
    for message in assistant_messages:
        index = message.get("_anthropic_call_index")
        if isinstance(index, int) and 0 <= index < len(anthropic_calls):
            represented_indices.append(index)
    represented_index_set = set(represented_indices)
    windows = compaction_windows(anthropic_calls)
    compaction_detected = bool(windows)
    compacted_indices = {
        index for window in windows.values() for index in window["compacted"]
    }
    successful_indices = {
        index for index, call in enumerate(anthropic_calls) if is_assistant_response(call)
    }
    missing_successes = successful_indices - represented_index_set - compacted_indices
    if missing_successes:
        issues.append({
            "code": "SUCCESSFUL_RESPONSE_MAPPING_MISSING",
            "call_indices": sorted(missing_successes),
        })
    invalid_mappings = represented_index_set - successful_indices
    if invalid_mappings or len(represented_indices) != len(represented_index_set):
        issues.append({
            "code": "ASSISTANT_CAPTURE_MAPPING_INVALID",
            "call_indices": sorted(invalid_mappings),
        })
    represented_capture_calls = [
        anthropic_calls[index] for index in represented_indices
        if isinstance(anthropic_calls[index], Mapping)
    ]
    represented_capture_ids, _, _ = _capture_call_result_ids(represented_capture_calls)
    represented_capture_id_set = set(represented_capture_ids)
    if compaction_detected:
        capture_only = sorted(set(capture_call_ids) - represented_capture_id_set)
        if capture_only:
            warnings.append(
                {
                    "code": "CAPTURE_CALLS_COMPACTED",
                    "count": len(capture_only),
                    "tool_call_ids": capture_only,
                }
            )
        if represented_capture_id_set != set(session_call_ids):
            issues.append(
                {
                    "code": "TOOL_CALL_BOUNDARY_MISMATCH",
                    "left": "represented_capture",
                    "right": "hermes",
                    "left_only": sorted(represented_capture_id_set - set(session_call_ids)),
                    "right_only": sorted(set(session_call_ids) - represented_capture_id_set),
                }
            )
    elif set(capture_call_ids) != set(session_call_ids):
        issues.append(
            {
                "code": "TOOL_CALL_BOUNDARY_MISMATCH",
                "left": "capture",
                "right": "hermes",
                "left_only": sorted(set(capture_call_ids) - set(session_call_ids)),
                "right_only": sorted(set(session_call_ids) - set(capture_call_ids)),
            }
        )
    if set(session_call_ids) != set(atif_call_ids):
        issues.append(
            {
                "code": "TOOL_CALL_BOUNDARY_MISMATCH",
                "left": "hermes",
                "right": "atif",
                "left_only": sorted(set(session_call_ids) - set(atif_call_ids)),
                "right_only": sorted(set(atif_call_ids) - set(session_call_ids)),
            }
        )
    incomplete_indices = [
        index for index, call in enumerate(anthropic_calls)
        if isinstance(call, Mapping) and not bool(call.get("complete"))
    ]
    if incomplete_indices:
        retried_indices: set[int] = set()
        if exchanges is not None:
            raw_exchanges = _records(exchanges)
            if len(raw_exchanges) == len(anthropic_calls) and all(
                isinstance(call, Mapping)
                and call.get("raw_exchange_sha256") == canonical_json_sha256(raw)
                for call, raw in zip(anthropic_calls, raw_exchanges, strict=True)
            ):
                superseded = _superseded_transport_exchange_ids(raw_exchanges)
                retried_indices = {
                    index for index in incomplete_indices
                    if anthropic_calls[index].get("exchange_id") in superseded
                    and index not in represented_index_set
                }
        if retried_indices:
            warnings.append({
                "code": "RETRIED_EMPTY_TRANSPORT_CAPTURE",
                "call_indices": sorted(retried_indices),
            })
        remaining = sorted(set(incomplete_indices) - retried_indices)
        if remaining:
            issues.append({
                "code": "INCOMPLETE_MODEL_EXCHANGE",
                "call_indices": remaining,
            })
    harness_drift = full_trajectory.get("harness_drift")
    if isinstance(harness_drift, list) and harness_drift:
        issues.append({"code": "HARNESS_DRIFT", "calls": copy.deepcopy(harness_drift)})

    metadata = full_trajectory.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    task_input = full_trajectory.get("task_input")
    requires_task_input = metadata.get("input_profile") == "traceforge-task-bundle-v1"
    if requires_task_input and not isinstance(task_input, Mapping):
        issues.append({"code": "TASK_INPUT_MISSING"})
    alignment = full_trajectory.get("input_alignment")
    if isinstance(task_input, Mapping):
        required_alignment = (
            "task_input_present",
            "rendered_user_prompt_present",
            "hermes_first_user_matches",
            "anthropic_first_user_matches",
            "system_prompt_captured",
            "tool_definitions_captured",
        )
        if not isinstance(alignment, Mapping):
            issues.append({"code": "TASK_INPUT_ALIGNMENT_MISSING"})
        else:
            failed = [name for name in required_alignment if alignment.get(name) is not True]
            if failed:
                issues.append({"code": "TASK_INPUT_MISMATCH", "checks": failed})

    if len(assistant_messages) != len(atif_agent_steps):
        issues.append(
            {
                "code": "LLM_CALL_COUNT_MISMATCH",
                "capture": len(anthropic_calls),
                "hermes": len(assistant_messages),
                "atif": len(atif_agent_steps),
            }
        )
    elif len(anthropic_calls) != len(assistant_messages):
        warnings.append(
            {
                "code": "CAPTURE_HERMES_COUNT_DIFFERENCE",
                "capture": len(anthropic_calls),
                "hermes": len(assistant_messages),
                "represented_capture_calls": len(represented_indices),
            }
        )

    for ordinal, (message, step) in enumerate(zip(assistant_messages, atif_agent_steps)):
        index = message.get("_anthropic_call_index")
        if not isinstance(index, int) or index < 0 or index >= len(anthropic_calls):
            issues.append(
                {
                    "code": "ASSISTANT_CAPTURE_MAPPING_MISSING",
                    "assistant_index": ordinal,
                }
            )
            continue
        call = anthropic_calls[index]
        if not isinstance(call, Mapping):
            continue
        raw_usage = normalize_usage(
            call.get("usage") if isinstance(call.get("usage"), Mapping) else None
        )
        metrics = step.get("metrics")
        metrics = metrics if isinstance(metrics, Mapping) else {}
        extra = metrics.get("extra")
        extra = extra if isinstance(extra, Mapping) else {}
        atif_raw_usage = extra.get("anthropic_usage")
        if atif_raw_usage != raw_usage:
            issues.append(
                {
                    "code": "USAGE_RAW_MISMATCH",
                    "call_index": index,
                    "capture": raw_usage,
                    "atif": copy.deepcopy(atif_raw_usage),
                }
            )
        expected_metrics = {
            "prompt_tokens": atif_prompt_tokens(raw_usage),
            "completion_tokens": raw_usage["output_tokens"],
            "cached_tokens": raw_usage["cache_read_input_tokens"],
        }
        for field_name, expected in expected_metrics.items():
            actual = metrics.get(field_name)
            if expected is None:
                if actual is not None:
                    issues.append(
                        {
                            "code": "USAGE_NULL_SEMANTICS_VIOLATION",
                            "call_index": index,
                            "field": field_name,
                            "actual": actual,
                        }
                    )
            elif actual != expected:
                issues.append(
                    {
                        "code": "USAGE_PROJECTION_MISMATCH",
                        "call_index": index,
                        "field": field_name,
                        "expected": expected,
                        "actual": actual,
                    }
                )

    recorded_usage = full_trajectory.get("usage")
    recorded_total = normalize_usage(
        recorded_usage if isinstance(recorded_usage, Mapping) else None
    )
    expected_total = aggregate_usage(
        call.get("usage") if isinstance(call, Mapping) else None for call in anthropic_calls
    )
    if recorded_total != expected_total:
        issues.append(
            {
                "code": "USAGE_TOTAL_MISMATCH",
                "recorded": recorded_total,
                "expected": expected_total,
            }
        )
    represented_total = aggregate_usage(
        anthropic_calls[index].get("usage")
        if isinstance(anthropic_calls[index], Mapping)
        else None
        for index in represented_indices
    )
    final_metrics = atif.get("final_metrics")
    final_metrics = final_metrics if isinstance(final_metrics, Mapping) else {}
    expected_final_metrics = {
        "total_prompt_tokens": atif_prompt_tokens(represented_total),
        "total_completion_tokens": represented_total["output_tokens"],
        "total_cached_tokens": represented_total["cache_read_input_tokens"],
    }
    for field_name, expected in expected_final_metrics.items():
        actual = final_metrics.get(field_name)
        if expected is None:
            if actual is not None:
                issues.append(
                    {
                        "code": "USAGE_NULL_SEMANTICS_VIOLATION",
                        "field": f"final_metrics.{field_name}",
                        "actual": actual,
                    }
                )
        elif actual != expected:
            issues.append(
                {
                    "code": "USAGE_TOTAL_PROJECTION_MISMATCH",
                    "field": field_name,
                    "expected": expected,
                    "actual": actual,
                }
            )

    hashes = full_trajectory.get("hashes")
    raw_tool_hash = hashes.get("tool_definitions_sha256") if isinstance(hashes, Mapping) else None
    atif_extra = atif.get("extra")
    projected_tool_hash = (
        atif_extra.get("tool_definitions_sha256") if isinstance(atif_extra, Mapping) else None
    )
    if raw_tool_hash != projected_tool_hash:
        issues.append(
            {
                "code": "TOOL_SCHEMA_HASH_MISMATCH",
                "full": raw_tool_hash,
                "atif": projected_tool_hash,
            }
        )

    messages_by_call: dict[int, Mapping[str, Any]] = {}
    steps_by_call: dict[int, Mapping[str, Any]] = {}
    for message, step in zip(assistant_messages, atif_agent_steps):
        index = message.get("_anthropic_call_index")
        if isinstance(index, int):
            messages_by_call[index] = message
            steps_by_call[index] = step
    mappings = []
    for index, call in enumerate(anthropic_calls):
        message = messages_by_call.get(index)
        step = steps_by_call.get(index)
        mappings.append(
            {
                "call_index": index,
                "exchange_id": call.get("exchange_id") if isinstance(call, Mapping) else None,
                "hermes_message_sha256": (
                    canonical_json_sha256(message) if message is not None else None
                ),
                "atif_step_id": step.get("step_id") if isinstance(step, Mapping) else None,
                "tool_call_ids": [
                    item["tool_call_id"] for item in _tool_calls(message)
                ] if message is not None else [],
                "represented": message is not None,
            }
        )

    return {
        "schema_version": "traceforge-evidence-reconciliation-v1",
        "generated_at": _utc_now(),
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "mappings": mappings,
        "boundaries": {
            name: {"tool_call_ids": sorted(calls), "tool_result_ids": sorted(results)}
            for name, (calls, results) in boundaries.items()
        },
        "compaction": {
            "detected": compaction_detected,
            "capture_call_count": len(anthropic_calls),
            "hermes_message_count": len(assistant_messages),
            "represented_capture_call_count": len(represented_indices),
        },
        "hashes": {
            "full_trajectory_sha256": canonical_json_sha256(full_trajectory),
            "atif_sha256": canonical_json_sha256(atif),
        },
    }

def write_evidence_bundle(
    output_dir: Path | str,
    exchanges: JsonSource | None = None,
    hermes_session: JsonSource | None = None,
    *,
    task_input: JsonSource | None = None,
    metadata: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """一次生成无损视图、ATIF、投影报告和对账报告。"""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    # 早期 Agent 将 CaptureProxy 证据放在 agent/capture/；标准 Trial 契约
    # 要求它们位于 agent/ 根目录。保留原文件并复制字节，不重序列化。
    for capture_name in ("anthropic-exchanges.jsonl", "anthropic-sse.jsonl"):
        target = output_path / capture_name
        nested = output_path / "capture" / capture_name
        if not target.is_file() and nested.is_file():
            shutil.copyfile(nested, target)
    if exchanges is None and hermes_session is None:
        full = build_full_trajectory(
            output_path,
            task_input=task_input,
            metadata=metadata,
            artifacts=artifacts,
        )
    elif exchanges is not None and hermes_session is not None:
        full = build_full_trajectory(
            exchanges,
            hermes_session,
            task_input=task_input,
            metadata=metadata,
            artifacts=artifacts,
        )
    else:
        raise EvidenceError("exchanges 和 hermes_session 必须同时提供或同时省略")
    atif, projection = project_atif_v17(full, include_report=True)
    reconciliation = reconcile_evidence(
        full, atif,
        exchanges=exchanges if exchanges is not None else output_path / "anthropic-exchanges.jsonl",
    )
    values = {
        "trajectory.full.json": full,
        "trajectory.json": atif,
        "projection_report.json": projection,
        "reconciliation.json": reconciliation,
    }
    paths: dict[str, Path] = {}
    for name, value in values.items():
        path = output_path / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        paths[name] = path
    if not projection.get("ok") or not reconciliation.get("ok"):
        raise EvidenceError("轨迹投影或对账失败")
    return {
        "full": full,
        "atif": atif,
        "projection_report": projection,
        "reconciliation": reconciliation,
        "usage": full.get("usage"),
        "paths": paths,
    }


__all__ = [
    "EvidenceError",
    "USAGE_FIELDS",
    "aggregate_usage",
    "atif_prompt_tokens",
    "build_full_trajectory",
    "canonical_json_sha256",
    "normalize_usage",
    "project_atif_v17",
    "reconcile_evidence",
    "write_evidence_bundle",
]
