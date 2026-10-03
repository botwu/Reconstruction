"""Harbor Trial 证据验证和状态归一化。"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as _dt
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from .capture import assemble_anthropic_sse
from .evidence import (
    EvidenceError,
    _superseded_transport_exchange_ids,
    _validate_raw_body,
    aggregate_usage,
    canonical_json_sha256,
    compaction_summary_text,
    compaction_windows,
    is_assistant_response,
    normalize_usage,
    project_atif_v17,
    reconcile_evidence,
    request_assistant_contents,
    response_history_matches,
)
from .input_contract import (
    RENDERED_INPUT_SCHEMA,
    RUNTIME_APPENDIX_HEADER,
    TASK_BUNDLE_INPUT_PROFILE,
    render_runtime_appendix,
)
from .task_bundle import TaskBundleError, validate_task_bundle


class CertificationStatus(StrEnum):
    TASK_PASS = "TASK_PASS"
    TASK_FAIL = "TASK_FAIL"
    TASK_BUDGET_EXHAUSTED = "TASK_BUDGET_EXHAUSTED"
    INFRA_AGENT = "INFRA_AGENT"
    INFRA_ENVIRONMENT = "INFRA_ENVIRONMENT"
    INFRA_CAPTURE = "INFRA_CAPTURE"
    INFRA_VERIFIER = "INFRA_VERIFIER"
    INFRA_TIMEOUT = "INFRA_TIMEOUT"
    CANCELLED = "CANCELLED"
    CLEANUP_UNVERIFIABLE = "CLEANUP_UNVERIFIABLE"

    @property
    def is_task_outcome(self) -> bool:
        return self in {
            self.TASK_PASS,
            self.TASK_FAIL,
            self.TASK_BUDGET_EXHAUSTED,
        }


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int | None
    detector: str
    match_sha256: str


@dataclass
class CertificationResult:
    status: CertificationStatus
    certified: bool
    task_passed: bool | None
    generated_at: str
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    evidence_hashes: dict[str, str] = field(default_factory=dict)
    secret_findings: list[SecretFinding] = field(default_factory=list)
    schema_version: str = "traceforge-harbor-certification-v1"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


class TrialValidationError(RuntimeError):
    """Trial 无法形成合法认证结果。"""


def _utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialValidationError(f"无法读取 JSON {path}: {exc}") from exc


def _safe_relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _content_present(value: Any) -> bool:
    """判断 prompt/message 是否包含可保留的真实内容。"""

    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return any(
            _content_present(value.get(key))
            for key in ("text", "content", "output", "thinking")
            if key in value
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_content_present(item) for item in value)
    return False


_CANARY_LINE_RE = re.compile(r"^(<!--.*canary.*-->|#.*canary.*)$", re.IGNORECASE)


def _strip_leading_canary(text: str) -> str:
    """与 Harbor 0.22.0 的 Task instruction 读取语义保持一致。"""

    lines = text.split("\n")
    index = 0
    while index < len(lines) and _CANARY_LINE_RE.match(lines[index].strip()):
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return "\n".join(lines[index:])


def _captured_response(exchange: Any) -> dict[str, Any]:
    if not isinstance(exchange, Mapping):
        return {}
    response = exchange.get("response")
    if not isinstance(response, Mapping):
        return {}
    message = response.get("message")
    if isinstance(message, Mapping):
        return dict(message)
    parsed = response.get("json")
    if isinstance(parsed, Mapping):
        return dict(parsed)
    body = response.get("body")
    if isinstance(body, Mapping) and isinstance(body.get("json"), Mapping):
        return dict(body["json"])
    if "content" in response or "usage" in response:
        return dict(response)
    return {}


def _response_assistant_projection(
    response: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    content = response.get("content")
    if not isinstance(content, list):
        return "", tool_calls
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(str(block["text"]))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id") or ""),
                    "name": str(block.get("name") or ""),
                    "arguments": block.get("input")
                    if isinstance(block.get("input"), Mapping)
                    else {},
                }
            )
    return "".join(text_parts), tool_calls


def _hermes_assistant_tool_calls(message: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        return []
    if not isinstance(raw_calls, list):
        return None
    result: list[dict[str, Any]] = []
    for call in raw_calls:
        if not isinstance(call, Mapping):
            return None
        function = call.get("function")
        if not isinstance(function, Mapping):
            return None
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return None
        if not isinstance(arguments, Mapping):
            return None
        result.append(
            {
                "id": str(call.get("id") or call.get("tool_call_id") or ""),
                "name": str(function.get("name") or call.get("name") or ""),
                "arguments": dict(arguments),
            }
        )
    return result


def _tool_result_map_from_requests(calls: Any) -> tuple[dict[str, Any], bool]:
    results: dict[str, Any] = {}
    conflict = False
    if not isinstance(calls, list):
        return results, conflict
    for call in calls:
        request = call.get("request") if isinstance(call, Mapping) else None
        messages = request.get("messages") if isinstance(request, Mapping) else None
        if not isinstance(messages, list):
            continue
        for message in messages:
            content = message.get("content") if isinstance(message, Mapping) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                    continue
                call_id = str(block.get("tool_use_id") or block.get("tool_call_id") or "")
                if not call_id:
                    conflict = True
                    continue
                value = block.get("content")
                if call_id in results and results[call_id] != value:
                    conflict = True
                results[call_id] = value
    return results, conflict


def _tool_result_map_from_messages(messages: Any) -> tuple[dict[str, Any], bool]:
    results: dict[str, Any] = {}
    conflict = False
    if not isinstance(messages, list):
        return results, conflict
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or message.get("id") or "")
        if not call_id or call_id in results:
            conflict = True
            continue
        results[call_id] = message.get("content")
    return results, conflict


def _request_user_and_tool_result_sequence(
    calls: Any,
) -> tuple[list[str], list[tuple[str, Any]], bool]:
    """从最后一笔真实请求恢复送入模型的完整非 system transcript。"""

    if not isinstance(calls, list) or not calls:
        return [], [], True
    final_call = calls[-1]
    request = final_call.get("request") if isinstance(final_call, Mapping) else None
    messages = request.get("messages") if isinstance(request, Mapping) else None
    if not isinstance(messages, list):
        return [], [], True
    users: list[str] = []
    results: list[tuple[str, Any]] = []
    invalid = False
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            users.append(content)
            continue
        if not isinstance(content, list):
            invalid = True
            continue
        text_parts: list[str] = []
        has_non_tool = False
        for block in content:
            if not isinstance(block, Mapping):
                invalid = True
                continue
            if block.get("type") == "tool_result":
                call_id = str(block.get("tool_use_id") or block.get("tool_call_id") or "")
                if not call_id:
                    invalid = True
                else:
                    results.append((call_id, block.get("content")))
                continue
            has_non_tool = True
            if block.get("type") != "text" or not isinstance(block.get("text"), str):
                invalid = True
            else:
                text_parts.append(str(block["text"]))
        if has_non_tool:
            users.append("".join(text_parts))
    return users, results, invalid


def _hermes_user_and_tool_result_sequence(
    messages: Any,
) -> tuple[list[str], list[tuple[str, Any]], bool]:
    if not isinstance(messages, list):
        return [], [], True
    users: list[str] = []
    results: list[tuple[str, Any]] = []
    invalid = False
    for message in messages:
        if not isinstance(message, Mapping):
            invalid = True
            continue
        if message.get("role") == "system":
            invalid = True
        elif message.get("role") == "user":
            text = _first_user_text([message])
            if text is None:
                invalid = True
            else:
                users.append(text)
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or message.get("id") or "")
            if not call_id:
                invalid = True
            else:
                results.append((call_id, message.get("content")))
    return users, results, invalid


def _anthropic_transcript(calls: Any) -> tuple[list[dict[str, Any]], bool]:
    """从最后一笔真实 Anthropic request 和它的 response 恢复完整对话。"""

    if not isinstance(calls, list) or not calls:
        return [], True
    final_call = calls[-1]
    request = final_call.get("request") if isinstance(final_call, Mapping) else None
    messages = request.get("messages") if isinstance(request, Mapping) else None
    response = final_call.get("response") if isinstance(final_call, Mapping) else None
    if not isinstance(messages, list) or not isinstance(response, Mapping):
        return [], True

    transcript: list[dict[str, Any]] = []
    invalid = False
    for message in messages:
        if not isinstance(message, Mapping):
            invalid = True
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            invalid = True
            continue
        if role == "assistant":
            text, tool_calls = _response_assistant_projection(message)
            if not isinstance(content, list):
                if isinstance(content, str):
                    text = content
                else:
                    invalid = True
            transcript.append(
                {"role": "assistant", "content": text, "tool_calls": tool_calls}
            )
            continue
        if role != "user":
            invalid = True
            continue
        if isinstance(content, str):
            transcript.append({"role": "user", "content": content})
            continue
        if not isinstance(content, list):
            invalid = True
            continue
        text_parts: list[str] = []
        tool_results: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, Mapping):
                invalid = True
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(str(block["text"]))
            elif block_type == "tool_result":
                call_id = str(block.get("tool_use_id") or block.get("tool_call_id") or "")
                if not call_id:
                    invalid = True
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": block.get("content"),
                    }
                )
            else:
                invalid = True
        if text_parts and tool_results:
            if not all(compaction_summary_text(part) for part in text_parts):
                invalid = True
            # Hermes 把摘要和前置工具返回合成一个 user turn，保留 block 原序。
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "text":
                    transcript.append({"role": "user", "content": block.get("text")})
                elif block.get("type") == "tool_result":
                    transcript.append({
                        "role": "tool",
                        "tool_call_id": str(
                            block.get("tool_use_id") or block.get("tool_call_id") or ""
                        ),
                        "content": block.get("content"),
                    })
        else:
            if text_parts:
                transcript.append({"role": "user", "content": "".join(text_parts)})
            transcript.extend(tool_results)

    if is_assistant_response(final_call):
        text, tool_calls = _response_assistant_projection(response)
        transcript.append({"role": "assistant", "content": text, "tool_calls": tool_calls})
    for event in transcript:
        if event.get("role") == "user":
            # Hermes/SDK 在后续 request 中会去掉初始 prompt 末尾换行。
            # 初始原文已由第一笔 request 单独逐字节绑定；这里只
            # 比较完整角色交错和后续内容，不重复依赖运输投影。
            event["content"] = "<FIRST_USER_BOUND_BY_INITIAL_REQUEST>"
            break
    return transcript, invalid


def _terminal_text_only_call(call: Any, index: int, total: int) -> bool:
    """Return true for the normal final Hermes text-only completion call.

    Hermes may issue one final completion request after the tool loop. That
    request intentionally omits the tool schema; it remains bound to the
    captured response and is validated through the response/evidence hashes.
    """
    if index != total - 1 or not isinstance(call, Mapping):
        return False
    request = call.get("request")
    response = call.get("response")
    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        return False
    content = response.get("content")
    has_tool_use = isinstance(content, list) and any(
        isinstance(block, Mapping)
        and block.get("type") in {"tool_use", "tool_call"}
        for block in content
    )
    return (
        is_assistant_response(call)
        and not request.get("tools")
        and not request.get("tool_choice")
        and not has_tool_use
        and bool(call.get("complete"))
    )


def _hermes_transcript(messages: Any) -> tuple[list[dict[str, Any]], bool]:
    """将 Hermes session 投影成与 Anthropic 边界可逐项比较的对话。"""

    if not isinstance(messages, list):
        return [], True
    transcript: list[dict[str, Any]] = []
    invalid = False
    for message in messages:
        if not isinstance(message, Mapping):
            invalid = True
            continue
        role = message.get("role")
        if role == "system":
            invalid = True
            continue
        if role == "user":
            text = _first_user_text([message])
            if text is None:
                invalid = True
            else:
                transcript.append({"role": "user", "content": text})
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or message.get("id") or "")
            if not call_id:
                invalid = True
            transcript.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": message.get("content"),
                }
            )
            continue
        if role == "assistant":
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text, _ = _response_assistant_projection(message)
            elif content is None:
                text = ""
            else:
                invalid = True
                text = ""
            tool_calls = _hermes_assistant_tool_calls(message)
            if tool_calls is None:
                invalid = True
                tool_calls = []
            transcript.append(
                {"role": "assistant", "content": text, "tool_calls": tool_calls}
            )
            continue
        invalid = True
    for event in transcript:
        if event.get("role") == "user":
            event["content"] = "<FIRST_USER_BOUND_BY_INITIAL_REQUEST>"
            break
    return transcript, invalid


def _context_compaction_detected(messages: Any, calls: Any) -> bool:
    return bool(compaction_windows(calls))


def _call_transcript_prefix_issues(calls: Any, messages: Any) -> list[dict[str, Any]]:
    """逐笔验证历史；只有已证明的压缩边界可以替换原窗口。"""
    if not isinstance(calls, list):
        return [{"code": "CALL_TRANSCRIPT_CALLS_SHAPE"}]
    hermes, hermes_invalid = _hermes_transcript(messages)
    if hermes_invalid:
        return [{"code": "CALL_TRANSCRIPT_HERMES_INVALID"}]
    windows = compaction_windows(calls)
    compacted = {index for window in windows.values() for index in window["compacted"]}
    final_indices = [
        index for index, call in enumerate(calls)
        if is_assistant_response(call) and index not in compacted
    ]
    assistant_positions = [
        index for index, event in enumerate(hermes)
        if event.get("role") == "assistant" and not compaction_summary_text(event.get("content"))
    ]
    issues: list[dict[str, Any]] = []
    if len(assistant_positions) != len(final_indices):
        issues.append({
            "code": "CALL_TRANSCRIPT_ASSISTANT_COUNT",
            "calls": len(final_indices), "assistants": len(assistant_positions),
        })
    visible: list[int] = []
    previous_prefix: list[dict[str, Any]] | None = None
    last_boundary = max(windows, default=-1)
    for index, call in enumerate(calls):
        if not isinstance(call, Mapping):
            issues.append({"code": "CALL_TRANSCRIPT_CALL_SHAPE", "call_index": index})
            continue
        if index in windows:
            visible = list(windows[index]["retained"])
        request = call.get("request")
        request_messages = request.get("messages") if isinstance(request, Mapping) else None
        actual = request_assistant_contents(request_messages)
        expected = [calls[item]["response"]["content"] for item in visible]
        if len(actual) != len(expected) or not all(
            response_history_matches(a, e) for a, e in zip(actual, expected, strict=True)
        ):
            issues.append({"code": "CALL_RESPONSE_HISTORY_MISMATCH", "call_index": index})
        raw_prefix, raw_invalid = _anthropic_transcript([call])
        request_prefix, request_invalid = _anthropic_transcript(
            [dict(call, response={}, status=500)]
        )
        # 每次失败请求也必须保留此前真实历史；只有当前压缩边界可更换它。
        if (
            previous_prefix is not None and index not in windows
            and not _terminal_text_only_call(call, index, len(calls))
            and (request_invalid or request_prefix[:len(previous_prefix)] != previous_prefix)
        ):
            issues.append({"code": "CALL_REQUEST_HISTORY_MISMATCH", "call_index": index})
        if index >= last_boundary and not _terminal_text_only_call(call, index, len(calls)):
            ordinal = sum(item < index for item in final_indices)
            if is_assistant_response(call):
                end = assistant_positions[ordinal] + 1 if ordinal < len(assistant_positions) else -1
            else:
                end = (
                    assistant_positions[ordinal]
                    if ordinal < len(assistant_positions) else len(hermes)
                )
            if raw_invalid or end < 0 or raw_prefix != hermes[:end]:
                issues.append({"code": "CALL_TRANSCRIPT_PREFIX_MISMATCH", "call_index": index})
        previous_prefix = raw_prefix
        if is_assistant_response(call):
            visible.append(index)
    return issues


def _first_user_text(messages: Any) -> str | None:
    """取得首个纯文本 user turn；初始任务输入不允许悄悄丢弃附件。"""

    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        parts: list[str] = []
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "text":
                return None
            text = block.get("text")
            if not isinstance(text, str):
                return None
            parts.append(text)
        return "".join(parts)
    return None


def _mapping_contains(actual: Any, expected: Any) -> bool:
    """验证规范化对象完整保留原对象，允许附加派生字段。"""

    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _mapping_contains(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            _mapping_contains(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    return actual == expected


def _validate_workspace_snapshot(value: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(value, Mapping):
        return [{"code": "WORKSPACE_INITIAL_NOT_OBJECT"}]
    root = value.get("root")
    if (
        not isinstance(root, str)
        or not root
        or not PurePosixPath(root).is_absolute()
        or ".." in PurePosixPath(root).parts
    ):
        issues.append({"code": "WORKSPACE_INITIAL_ROOT"})
    files = value.get("files")
    if not isinstance(files, list):
        return [*issues, {"code": "WORKSPACE_INITIAL_FILES"}]
    seen_paths: set[str] = set()
    total_bytes = 0
    for index, item in enumerate(files):
        if not isinstance(item, Mapping):
            issues.append({"code": "WORKSPACE_INITIAL_ENTRY", "index": index})
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            issues.append({"code": "WORKSPACE_INITIAL_PATH", "index": index})
        else:
            parsed = PurePosixPath(path)
            if parsed.is_absolute() or ".." in parsed.parts or parsed.as_posix() != path:
                issues.append({"code": "WORKSPACE_INITIAL_PATH", "index": index})
            if path in seen_paths:
                issues.append({"code": "WORKSPACE_INITIAL_DUPLICATE", "index": index})
            seen_paths.add(path)
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            issues.append({"code": "WORKSPACE_INITIAL_SIZE", "index": index})
        else:
            total_bytes += size
        digest = item.get("sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            issues.append({"code": "WORKSPACE_INITIAL_DIGEST", "index": index})
        mode = item.get("mode")
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
            issues.append({"code": "WORKSPACE_INITIAL_MODE", "index": index})
    if value.get("file_count") != len(files):
        issues.append({"code": "WORKSPACE_INITIAL_FILE_COUNT"})
    if value.get("total_bytes") != total_bytes:
        issues.append({"code": "WORKSPACE_INITIAL_TOTAL_BYTES"})
    if value.get("tree_sha256") != canonical_json_sha256(files):
        issues.append({"code": "WORKSPACE_INITIAL_TREE_DIGEST"})
    return issues


def _workspace_snapshot_from_directory(source: Path, *, logical_root: str) -> dict[str, Any]:
    """从 Harbor Task 的公开输入面重算预期初始 workspace。"""

    files: list[dict[str, Any]] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise TrialValidationError(f"公开 workspace 不允许符号链接: {path}")
        if not path.is_file():
            continue
        raw = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(source).as_posix(),
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
        )
    return {
        "root": logical_root,
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(int(item["size"]) for item in files),
        "tree_sha256": canonical_json_sha256(files),
    }


def _validate_text_component(value: Any, *, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return [{"code": "TASK_INPUT_COMPONENT", "field": field_name}]
    content = value.get("content")
    if not isinstance(content, str) or not content:
        return [{"code": "TASK_INPUT_COMPONENT_CONTENT", "field": field_name}]
    encoded = content.encode("utf-8")
    issues: list[dict[str, Any]] = []
    if value.get("sha256") != hashlib.sha256(encoded).hexdigest():
        issues.append({"code": "TASK_INPUT_COMPONENT_DIGEST", "field": field_name})
    if value.get("size_bytes") != len(encoded):
        issues.append({"code": "TASK_INPUT_COMPONENT_SIZE", "field": field_name})
    if value.get("characters") != len(content):
        issues.append({"code": "TASK_INPUT_COMPONENT_CHARACTERS", "field": field_name})
    return issues


def _validate_task_input_snapshot(
    task_input: Any,
    workspace_initial: Any,
) -> list[dict[str, Any]]:
    """验证 Task Bundle 的 task、runtime 和初始 workspace 组合输入。"""

    if not isinstance(task_input, Mapping):
        return [{"code": "TASK_INPUT_NOT_OBJECT"}]
    issues: list[dict[str, Any]] = []
    if task_input.get("schema_version") != "traceforge-agent-visible-input/v1":
        issues.append({"code": "TASK_INPUT_SCHEMA"})
    unsigned = dict(task_input)
    observed_self_hash = unsigned.pop("sha256", None)
    if observed_self_hash != canonical_json_sha256(unsigned):
        issues.append({"code": "TASK_INPUT_SELF_DIGEST"})

    instruction = task_input.get("instruction")
    if not isinstance(instruction, Mapping):
        issues.append({"code": "TASK_INPUT_INSTRUCTION"})
    else:
        if instruction.get("schema_version") != RENDERED_INPUT_SCHEMA:
            issues.append({"code": "TASK_INPUT_INSTRUCTION_SCHEMA"})
        task_component = instruction.get("task_instruction")
        appendix_component = instruction.get("runtime_appendix")
        rendered_component = instruction.get("rendered_user_prompt")
        issues.extend(
            _validate_text_component(task_component, field_name="task_instruction")
        )
        issues.extend(
            _validate_text_component(appendix_component, field_name="runtime_appendix")
        )
        issues.extend(
            _validate_text_component(rendered_component, field_name="rendered_user_prompt")
        )
        task_text = task_component.get("content") if isinstance(task_component, Mapping) else None
        appendix_text = (
            appendix_component.get("content")
            if isinstance(appendix_component, Mapping)
            else None
        )
        rendered_text = (
            rendered_component.get("content")
            if isinstance(rendered_component, Mapping)
            else None
        )
        if not isinstance(appendix_text, str) or not appendix_text.startswith(
            RUNTIME_APPENDIX_HEADER
        ):
            issues.append({"code": "TASK_INPUT_RUNTIME_APPENDIX"})
        if (
            not isinstance(task_text, str)
            or not isinstance(appendix_text, str)
            or rendered_text != f"{task_text}\n\n{appendix_text}"
        ):
            issues.append({"code": "TASK_INPUT_COMPOSITION"})
        if instruction.get("composition") != (
            "task_instruction + two_newlines + runtime_appendix"
        ):
            issues.append({"code": "TASK_INPUT_COMPOSITION_LABEL"})
        hashes = instruction.get("hashes")
        expected_hashes = {
            "task_instruction_sha256": (
                hashlib.sha256(task_text.encode("utf-8")).hexdigest()
                if isinstance(task_text, str)
                else None
            ),
            "runtime_appendix_sha256": (
                hashlib.sha256(appendix_text.encode("utf-8")).hexdigest()
                if isinstance(appendix_text, str)
                else None
            ),
            "rendered_user_prompt_sha256": (
                hashlib.sha256(rendered_text.encode("utf-8")).hexdigest()
                if isinstance(rendered_text, str)
                else None
            ),
        }
        if not isinstance(hashes, Mapping) or any(
            hashes.get(name) != digest for name, digest in expected_hashes.items()
        ):
            issues.append({"code": "TASK_INPUT_HASH_INDEX"})

    workspace = task_input.get("workspace")
    issues.extend(_validate_workspace_snapshot(workspace))
    if workspace != workspace_initial:
        issues.append({"code": "TASK_INPUT_WORKSPACE_MISMATCH"})
    runtime = task_input.get("runtime")
    if not isinstance(runtime, Mapping):
        issues.append({"code": "TASK_INPUT_RUNTIME"})
    else:
        workspace_root = workspace.get("root") if isinstance(workspace, Mapping) else None
        if runtime.get("workspace_root") != workspace_root:
            issues.append({"code": "TASK_INPUT_RUNTIME_WORKSPACE"})
        artifact_root = runtime.get("artifact_root")
        if (
            not isinstance(artifact_root, str)
            or not PurePosixPath(artifact_root).is_absolute()
            or ".." in PurePosixPath(artifact_root).parts
        ):
            issues.append({"code": "TASK_INPUT_RUNTIME_ARTIFACT"})
        if runtime.get("provider") != "anthropic":
            issues.append({"code": "TASK_INPUT_RUNTIME_PROVIDER"})
        for field_name in ("model", "task_id"):
            if not isinstance(runtime.get(field_name), str) or not runtime.get(field_name):
                issues.append({"code": "TASK_INPUT_RUNTIME_FIELD", "field": field_name})
        toolsets = runtime.get("toolsets")
        if not isinstance(toolsets, list) or not toolsets or any(
            not isinstance(item, str) or not item for item in toolsets
        ):
            issues.append({"code": "TASK_INPUT_RUNTIME_TOOLSETS"})
        elif (
            isinstance(workspace_root, str)
            and isinstance(artifact_root, str)
            and isinstance(appendix_text, str)
        ):
            try:
                expected_appendix = render_runtime_appendix(
                    workspace_root=workspace_root,
                    artifact_root=artifact_root,
                    toolsets=",".join(toolsets),
                )
            except ValueError:
                issues.append({"code": "TASK_INPUT_RUNTIME_APPENDIX_PARAMETERS"})
            else:
                if appendix_text != expected_appendix:
                    issues.append({"code": "TASK_INPUT_RUNTIME_APPENDIX_MISMATCH"})
    return issues


_GENERIC_SECRET_PATTERNS = (
    (
        "token_prefix",
        re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{12,}"),
        0,
    ),
    (
        "auth_header_value",
        re.compile(
            r"(?i)(?:x-api-key|authorization|api-key)[\"']?\s*[:=]\s*[\"']?"
            r"(?!<redacted>|redacted|local-noauth|null|none)(?:Bearer\s+)?([A-Za-z0-9_./+=-]{8,})"
        ),
        1,
    ),
    (
        "credential_env_value",
        re.compile(
            r"(?i)(?:ANTHROPIC|OPENAI|TOKENHUB|E2B|AGS)_[A-Z0-9_]*KEY\s*="
            r"\s*[\"']?(?!<redacted>|redacted|local-noauth|null|none)([^\s\"']{8,})"
        ),
        1,
    ),
)


def scan_secrets(
    root: Path | str,
    *,
    secrets: Iterable[str] = (),
    max_file_bytes: int = 32 * 1024 * 1024,
) -> list[SecretFinding]:
    """扫描 Trial 目录，只返回匹配摘要，永不回显 secret。"""

    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise TrialValidationError(f"Trial 目录不存在: {root_path}")
    explicit = [
        value.encode("utf-8")
        for value in secrets
        if isinstance(value, str)
        and len(value) >= 6
        and value.lower() not in {"local-noauth", "<redacted>", "redacted"}
    ]
    findings: list[SecretFinding] = []
    for path in sorted(root_path.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name == "certification.json":
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            raw = path.read_bytes()
            relative = _safe_relative(path, root_path)
        except (OSError, ValueError):
            continue
        seen: set[tuple[str, int | None, str]] = set()
        for secret in explicit:
            if secret not in raw:
                continue
            digest = hashlib.sha256(secret).hexdigest()
            key = ("explicit_secret", None, digest)
            if key not in seen:
                findings.append(SecretFinding(relative, None, "explicit_secret", digest))
                seen.add(key)
        text = raw.decode("utf-8", "replace")
        for detector, pattern, group_index in _GENERIC_SECRET_PATTERNS:
            for match in pattern.finditer(text):
                value = match.group(group_index)
                if value.lower() in {"local-noauth", "<redacted>", "redacted", "null", "none"}:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
                key = (detector, line, digest)
                if key in seen:
                    continue
                findings.append(SecretFinding(relative, line, detector, digest))
                seen.add(key)
    return findings


def _generic_secret_value(root: Path, finding: SecretFinding) -> str | None:
    for detector, pattern, group_index in _GENERIC_SECRET_PATTERNS:
        if detector != finding.detector:
            continue
        try:
            text = (root / finding.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for match in pattern.finditer(text):
            value = match.group(group_index)
            if hashlib.sha256(value.encode("utf-8")).hexdigest() == finding.match_sha256:
                return value
    return None


def _source_literal_fingerprints(
    root: Path, source: Path, findings: list[SecretFinding],
) -> set[tuple[str, str]]:
    """仅认可绑定原文及其可证明的显示缩略，不把省略号当作通行证。"""
    source_findings = scan_secrets(source)
    allowed = {(item.detector, item.match_sha256) for item in source_findings}
    originals: dict[str, set[str]] = {}
    for item in source_findings:
        value = _generic_secret_value(source, item)
        if value is not None:
            originals.setdefault(item.detector, set()).add(value)
            if item.detector == "auth_header_value":
                originals[item.detector].add(f"Bearer {value}")
    for item in findings:
        key = (item.detector, item.match_sha256)
        if key in allowed or item.detector not in originals:
            continue
        value = _generic_secret_value(root, item) or ""
        prefix, separator, suffix = value.partition("...")
        if not separator or not prefix or not suffix or "..." in suffix or len(prefix + suffix) < 8:
            continue
        if any(
            original.startswith(prefix) and original.endswith(suffix)
            and len(original) > len(prefix + suffix)
            for original in originals[item.detector]
        ):
            allowed.add(key)
    return allowed


def validate_atif_v17(value: Any) -> list[dict[str, Any]]:
    """在不依赖 Harbor/Pydantic 的控制面执行核心 ATIF v1.7 不变量校验。"""

    errors: list[dict[str, Any]] = []
    if not isinstance(value, Mapping):
        return [{"code": "ATIF_NOT_OBJECT"}]
    if value.get("schema_version") != "ATIF-v1.7":
        errors.append({"code": "ATIF_SCHEMA_VERSION", "actual": value.get("schema_version")})
    agent = value.get("agent")
    if not isinstance(agent, Mapping):
        errors.append({"code": "ATIF_AGENT_MISSING"})
    else:
        for key in ("name", "version"):
            if not isinstance(agent.get(key), str) or not agent.get(key):
                errors.append({"code": "ATIF_AGENT_FIELD", "field": key})
        tool_definitions = agent.get("tool_definitions")
        if not isinstance(tool_definitions, list) or not tool_definitions:
            errors.append({"code": "ATIF_TOOL_DEFINITIONS_MISSING"})
    steps = value.get("steps")
    if not isinstance(steps, list):
        return errors + [{"code": "ATIF_STEPS_MISSING"}]
    declared_calls: set[str] = set()
    observed_calls: set[str] = set()
    observed_sources: set[str] = set()
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, Mapping):
            errors.append({"code": "ATIF_STEP_NOT_OBJECT", "step": index})
            continue
        if step.get("step_id") != index:
            errors.append(
                {"code": "ATIF_STEP_SEQUENCE", "step": index, "actual": step.get("step_id")}
            )
        source = step.get("source")
        if source not in {"system", "user", "agent"}:
            errors.append({"code": "ATIF_STEP_SOURCE", "step": index})
        else:
            observed_sources.add(str(source))
        if not isinstance(step.get("message"), (str, list)):
            errors.append({"code": "ATIF_STEP_MESSAGE", "step": index})
        elif source in {"system", "user"} and not _content_present(step.get("message")):
            errors.append(
                {
                    "code": "ATIF_REQUIRED_MESSAGE_EMPTY",
                    "step": index,
                    "source": source,
                }
            )
        raw_calls = step.get("tool_calls")
        if raw_calls is not None:
            if step.get("source") != "agent" or not isinstance(raw_calls, list):
                errors.append({"code": "ATIF_TOOL_CALLS_LOCATION", "step": index})
            else:
                for call in raw_calls:
                    if not isinstance(call, Mapping):
                        errors.append({"code": "ATIF_TOOL_CALL", "step": index})
                        continue
                    call_id = call.get("tool_call_id")
                    if not isinstance(call_id, str) or not call_id:
                        errors.append({"code": "ATIF_TOOL_CALL_ID", "step": index})
                    elif call_id in declared_calls:
                        errors.append({"code": "ATIF_DUPLICATE_TOOL_CALL_ID", "id": call_id})
                    else:
                        declared_calls.add(call_id)
                    if not isinstance(call.get("function_name"), str) or not isinstance(
                        call.get("arguments"), Mapping
                    ):
                        errors.append({"code": "ATIF_TOOL_CALL_SHAPE", "step": index})
        observation = step.get("observation")
        if observation is not None:
            if not isinstance(observation, Mapping) or not isinstance(
                observation.get("results"), list
            ):
                errors.append({"code": "ATIF_OBSERVATION", "step": index})
            else:
                for result in observation["results"]:
                    if not isinstance(result, Mapping):
                        errors.append({"code": "ATIF_OBSERVATION_RESULT", "step": index})
                        continue
                    source_call_id = result.get("source_call_id")
                    if source_call_id is not None:
                        if not isinstance(source_call_id, str) or not source_call_id:
                            errors.append({"code": "ATIF_OBSERVATION_CALL_ID", "step": index})
                        else:
                            observed_calls.add(source_call_id)
        metrics = step.get("metrics")
        if metrics is not None:
            if step.get("source") != "agent" or not isinstance(metrics, Mapping):
                errors.append({"code": "ATIF_METRICS_LOCATION", "step": index})
            else:
                for field_name in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                    if field_name in metrics and (
                        isinstance(metrics[field_name], bool)
                        or not isinstance(metrics[field_name], int)
                        or metrics[field_name] < 0
                    ):
                        errors.append(
                            {"code": "ATIF_METRIC_VALUE", "step": index, "field": field_name}
                        )
    for orphan in sorted(observed_calls - declared_calls):
        errors.append({"code": "ATIF_ORPHAN_OBSERVATION", "id": orphan})
    for missing in sorted(declared_calls - observed_calls):
        errors.append({"code": "ATIF_MISSING_OBSERVATION", "id": missing})
    for source in ("system", "user", "agent"):
        if source not in observed_sources:
            errors.append({"code": "ATIF_REQUIRED_SOURCE_MISSING", "source": source})
    return errors


def _parse_sse_frame(raw: bytes) -> dict[str, Any] | None:
    """按 CaptureGateway 的规则从原始 SSE frame 恢复语义字段。"""

    if not raw:
        return None
    event_name: str | None = None
    event_id: str | None = None
    data_lines: list[bytes] = []
    for line in raw.splitlines():
        if not line or line.startswith(b":"):
            continue
        field_name, separator, value = line.partition(b":")
        if separator and value.startswith(b" "):
            value = value[1:]
        if field_name == b"event":
            event_name = value.decode("utf-8", "replace")
        elif field_name == b"id":
            event_id = value.decode("utf-8", "replace")
        elif field_name == b"data":
            data_lines.append(value)
    data_bytes = b"\n".join(data_lines)
    data: Any = None
    if data_bytes:
        try:
            data = json.loads(data_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = data_bytes.decode("utf-8", "replace")
    return {"event": event_name, "id": event_id, "data": data}


def _parse_sse_body(raw: bytes) -> list[tuple[bytes, dict[str, Any]]]:
    """从整个 HTTP body 恢复 CaptureGateway 实际看到的 frame 顺序。"""

    remaining = raw
    result: list[tuple[bytes, dict[str, Any]]] = []
    while True:
        match = re.search(rb"\r?\n\r?\n", remaining)
        if match is None:
            break
        frame = remaining[: match.start()]
        remaining = remaining[match.end() :]
        parsed = _parse_sse_frame(frame)
        if parsed is not None:
            result.append((frame, parsed))
    if remaining:
        parsed = _parse_sse_frame(remaining)
        if parsed is not None:
            parsed["unterminated"] = True
            result.append((remaining, parsed))
    return result


def _validate_anthropic_message(
    value: Any,
    *,
    exchange_id: str,
) -> list[dict[str, Any]]:
    """验证 HTTP 200 的 Anthropic Messages 返回确实是完整模型消息。"""

    location = {"exchange_id": exchange_id}
    if not isinstance(value, Mapping) or not value:
        return [{"code": "ANTHROPIC_MESSAGE_SHAPE", **location}]
    errors: list[dict[str, Any]] = []
    for field_name in ("id", "model", "stop_reason"):
        if not isinstance(value.get(field_name), str) or not value.get(field_name):
            errors.append(
                {"code": "ANTHROPIC_MESSAGE_FIELD", "field": field_name, **location}
            )
    if value.get("type") != "message":
        errors.append({"code": "ANTHROPIC_MESSAGE_TYPE", **location})
    if value.get("role") != "assistant":
        errors.append({"code": "ANTHROPIC_MESSAGE_ROLE", **location})
    if "complete" in value and not isinstance(value.get("complete"), bool):
        errors.append({"code": "ANTHROPIC_MESSAGE_COMPLETE", **location})

    content = value.get("content")
    if not isinstance(content, list) or not content:
        errors.append({"code": "ANTHROPIC_MESSAGE_CONTENT", **location})
    else:
        for index, block in enumerate(content):
            block_location = {**location, "index": index}
            if not isinstance(block, Mapping):
                errors.append({"code": "ANTHROPIC_CONTENT_BLOCK", **block_location})
                continue
            block_type = block.get("type")
            if not isinstance(block_type, str) or not block_type:
                errors.append({"code": "ANTHROPIC_CONTENT_BLOCK_TYPE", **block_location})
            elif block_type not in {
                "text",
                "thinking",
                "redacted_thinking",
                "tool_use",
            }:
                errors.append(
                    {
                        "code": "ANTHROPIC_CONTENT_BLOCK_UNSUPPORTED",
                        "block_type": block_type,
                        **block_location,
                    }
                )
            elif block_type == "text" and not isinstance(block.get("text"), str):
                errors.append({"code": "ANTHROPIC_TEXT_BLOCK", **block_location})
            elif block_type == "thinking" and not isinstance(block.get("thinking"), str):
                errors.append({"code": "ANTHROPIC_THINKING_BLOCK", **block_location})
            elif block_type == "redacted_thinking" and not isinstance(
                block.get("data"), str
            ):
                errors.append({"code": "ANTHROPIC_REDACTED_THINKING_BLOCK", **block_location})
            elif block_type == "tool_use":
                if any(
                    not isinstance(block.get(field_name), str) or not block.get(field_name)
                    for field_name in ("id", "name")
                ) or not isinstance(block.get("input"), Mapping):
                    errors.append({"code": "ANTHROPIC_TOOL_USE_BLOCK", **block_location})

    usage = value.get("usage")
    if not isinstance(usage, Mapping):
        errors.append({"code": "ANTHROPIC_USAGE_SHAPE", **location})
    else:
        for field_name in ("input_tokens", "output_tokens"):
            observed = usage.get(field_name)
            if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
                errors.append(
                    {"code": "ANTHROPIC_USAGE_FIELD", "field": field_name, **location}
                )
        for field_name in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if field_name not in usage:
                continue
            observed = usage.get(field_name)
            if observed is not None and (
                isinstance(observed, bool) or not isinstance(observed, int) or observed < 0
            ):
                errors.append(
                    {"code": "ANTHROPIC_USAGE_FIELD", "field": field_name, **location}
                )
    return errors


def _validate_capture_records(exchanges: list[Any], sse_records: list[Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    superseded_transport = _superseded_transport_exchange_ids(exchanges)
    sse_by_exchange: dict[str, list[tuple[Mapping[str, Any], bytes, dict[str, Any]]]] = {}
    sensitive_headers = {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
    }
    for line_number, item in enumerate(sse_records, start=1):
        if not isinstance(item, Mapping):
            errors.append({"code": "SSE_RECORD_SHAPE", "line": line_number})
            continue
        exchange_id = item.get("exchange_id")
        if not isinstance(exchange_id, str) or not exchange_id:
            errors.append({"code": "SSE_EXCHANGE_ID", "line": line_number})
            continue
        encoded = item.get("raw_base64")
        raw: bytes | None = None
        if not isinstance(encoded, str):
            errors.append({"code": "SSE_RAW_FRAME_MISSING", "line": line_number})
        else:
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                errors.append({"code": "SSE_RAW_FRAME_BASE64", "line": line_number})
            else:
                if item.get("raw_size_bytes") != len(raw):
                    errors.append({"code": "SSE_FRAME_SIZE_MISMATCH", "line": line_number})
                if item.get("raw_sha256") != hashlib.sha256(raw).hexdigest():
                    errors.append({"code": "SSE_FRAME_HASH_MISMATCH", "line": line_number})
        parsed = _parse_sse_frame(raw) if raw is not None else None
        if parsed is None:
            errors.append({"code": "SSE_FRAME_PARSE", "line": line_number})
            continue
        for field_name in ("event", "id", "data"):
            if item.get(field_name) != parsed.get(field_name):
                errors.append(
                    {
                        "code": "SSE_FRAME_SEMANTIC_MISMATCH",
                        "line": line_number,
                        "field": field_name,
                    }
                )
        if item.get("unterminated") is True:
            parsed["unterminated"] = True
        sse_by_exchange.setdefault(exchange_id, []).append((item, raw, parsed))

    known_exchange_ids: set[str] = set()
    for line_number, item in enumerate(exchanges, start=1):
        if not isinstance(item, Mapping):
            errors.append({"code": "EXCHANGE_RECORD_SHAPE", "line": line_number})
            continue
        exchange_id = item.get("exchange_id")
        if not isinstance(exchange_id, str) or not exchange_id:
            errors.append({"code": "EXCHANGE_ID", "line": line_number})
            continue
        if exchange_id in known_exchange_ids:
            errors.append({"code": "DUPLICATE_EXCHANGE_ID", "exchange_id": exchange_id})
        known_exchange_ids.add(exchange_id)
        recovered = exchange_id in superseded_transport
        required_values = {
            "method": "POST",
            "path": "/v1/messages",
        }
        for field_name, expected in required_values.items():
            if item.get(field_name) != expected:
                errors.append(
                    {
                        "code": "CAPTURE_EXCHANGE_FIELD",
                        "exchange_id": exchange_id,
                        "field": field_name,
                    }
                )
        for field_name in ("started_at", "finished_at"):
            if not isinstance(item.get(field_name), str) or not item.get(field_name):
                errors.append(
                    {
                        "code": "CAPTURE_EXCHANGE_FIELD",
                        "exchange_id": exchange_id,
                        "field": field_name,
                    }
                )
        duration_ms = item.get("duration_ms")
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
        ):
            errors.append({"code": "CAPTURE_DURATION", "exchange_id": exchange_id})
        response_status = item.get("response_status")
        if (
            isinstance(response_status, bool)
            or not isinstance(response_status, int)
            or not 100 <= response_status <= 599
        ):
            errors.append({"code": "CAPTURE_HTTP_STATUS", "exchange_id": exchange_id})
        elif response_status != 200:
            errors.append(
                {
                    "code": "MODEL_EXCHANGE_HTTP_FAILURE",
                    "exchange_id": exchange_id,
                    "status": response_status,
                }
            )
        for field_name in ("streaming", "complete"):
            if not isinstance(item.get(field_name), bool):
                errors.append(
                    {
                        "code": "CAPTURE_EXCHANGE_FIELD",
                        "exchange_id": exchange_id,
                        "field": field_name,
                    }
                )
        if item.get("complete") is not True and not recovered:
            errors.append({"code": "MODEL_EXCHANGE_INCOMPLETE", "exchange_id": exchange_id})
        if item.get("error_code") is not None and not recovered:
            errors.append(
                {
                    "code": "MODEL_EXCHANGE_ERROR",
                    "exchange_id": exchange_id,
                    "error_code": item.get("error_code"),
                }
            )
        event_count = item.get("sse_event_count")
        if isinstance(event_count, bool) or not isinstance(event_count, int) or event_count < 0:
            errors.append({"code": "SSE_EVENT_COUNT_SHAPE", "exchange_id": exchange_id})
        for header_field in ("request_headers", "response_headers"):
            headers = item.get(header_field)
            if not isinstance(headers, Mapping):
                errors.append(
                    {
                        "code": "CAPTURE_HEADERS_SHAPE",
                        "exchange_id": exchange_id,
                        "field": header_field,
                    }
                )
                continue
            leaked_names = sorted(
                str(name) for name in headers if str(name).lower() in sensitive_headers
            )
            if leaked_names:
                errors.append(
                    {
                        "code": "CAPTURE_AUTH_HEADER_PERSISTED",
                        "exchange_id": exchange_id,
                        "field": header_field,
                        "header_names": leaked_names,
                    }
                )
        body_errors, _ = _validate_raw_body(
            item.get("request"), location=f"exchange:{exchange_id}:request"
        )
        errors.extend(body_errors)
        response = item.get("response")
        response_body = (
            response.get("body")
            if item.get("streaming") and isinstance(response, Mapping)
            else response
        )
        body_errors, response_raw = _validate_raw_body(
            response_body, location=f"exchange:{exchange_id}:response"
        )
        errors.extend(body_errors)
        events = sse_by_exchange.get(exchange_id, [])
        if item.get("streaming"):
            sequences = [event[0].get("sequence") for event in events]
            if sequences != list(range(len(events))):
                errors.append({"code": "SSE_SEQUENCE", "exchange_id": exchange_id})
            if item.get("sse_event_count") != len(events):
                errors.append({"code": "SSE_EVENT_COUNT", "exchange_id": exchange_id})
            body_events = _parse_sse_body(response_raw) if response_raw is not None else []
            if len(body_events) != len(events):
                errors.append({"code": "SSE_BODY_EVENT_COUNT", "exchange_id": exchange_id})
            else:
                for index, ((body_raw, body_event), (_, event_raw, event)) in enumerate(
                    zip(body_events, events, strict=True)
                ):
                    if body_raw != event_raw or body_event != event:
                        errors.append(
                            {
                                "code": "SSE_BODY_FRAME_MISMATCH",
                                "exchange_id": exchange_id,
                                "index": index,
                            }
                        )
            authoritative_events = [event for _, event in body_events]
            for index, event in enumerate(authoritative_events):
                data = event.get("data")
                data_type = data.get("type") if isinstance(data, Mapping) else None
                event_type = event.get("event")
                if (
                    isinstance(data_type, str)
                    and isinstance(event_type, str)
                    and data_type != event_type
                ):
                    errors.append(
                        {
                            "code": "SSE_EVENT_TYPE_MISMATCH",
                            "exchange_id": exchange_id,
                            "index": index,
                        }
                    )
            stop_seen = any(
                isinstance(event.get("data"), Mapping)
                and event["data"].get("type") == "message_stop"
                and event.get("event") in {None, "message_stop"}
                for event in authoritative_events
            )
            if item.get("response_status") == 200 and item.get("complete") and not stop_seen:
                errors.append({"code": "SSE_MESSAGE_STOP_MISSING", "exchange_id": exchange_id})
            normalized_response = assemble_anthropic_sse(authoritative_events)
            captured_message = (
                response.get("message") if isinstance(response, Mapping) else None
            )
            if captured_message != normalized_response:
                errors.append(
                    {
                        "code": "SSE_MESSAGE_REASSEMBLY_MISMATCH",
                        "exchange_id": exchange_id,
                    }
                )
            if (
                item.get("response_status") == 200
                and normalized_response.get("complete") is not True
                and not recovered
            ):
                errors.append(
                    {
                        "code": "SSE_ASSEMBLED_MESSAGE_INCOMPLETE",
                        "exchange_id": exchange_id,
                    }
                )
            if item.get("response_status") == 200 and item.get("complete") != stop_seen:
                errors.append({"code": "SSE_COMPLETENESS_MISMATCH", "exchange_id": exchange_id})
        elif events:
            errors.append({"code": "SSE_FOR_NON_STREAMING_EXCHANGE", "exchange_id": exchange_id})
        if item.get("response_status") == 200 and item.get("complete"):
            errors.extend(
                _validate_anthropic_message(
                    _captured_response(item),
                    exchange_id=exchange_id,
                )
            )
    for orphan in sorted(set(sse_by_exchange) - known_exchange_ids):
        errors.append({"code": "SSE_ORPHAN_EXCHANGE", "exchange_id": orphan})
    return errors


def _validate_reward(value: Any) -> tuple[bool, float | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    if not isinstance(value, Mapping) or not value:
        return False, None, [{"code": "REWARD_NOT_FLAT_OBJECT"}]
    if set(value) != {"task"}:
        errors.append(
            {
                "code": "REWARD_CRITERIA_MISMATCH",
                "expected": ["task"],
                "actual": sorted(str(key) for key in value),
            }
        )
    for key, item in value.items():
        if not isinstance(key, str) or isinstance(item, bool) or not isinstance(item, (int, float)):
            errors.append({"code": "REWARD_NON_NUMERIC", "field": str(key)})
            continue
        if not math.isfinite(float(item)):
            errors.append({"code": "REWARD_NON_FINITE", "field": key})
            continue
        if not 0.0 <= float(item) <= 1.0:
            errors.append({"code": "REWARD_OUT_OF_RANGE", "field": key})
            continue
    task_reward = value.get("task")
    if isinstance(task_reward, bool) or not isinstance(task_reward, (int, float)):
        errors.append({"code": "REWARD_TASK_MISSING"})
        task_value = None
    else:
        task_value = float(task_reward)
    return not errors, task_value, errors


def _validate_evaluation_contract(
    verdict: Any,
    task_bundle: Any,
) -> list[dict[str, Any]]:
    if not isinstance(verdict, Mapping):
        return [{"code": "EVALUATION_CONTRACT_VERDICT_SHAPE"}]
    observed = verdict.get("evaluation_contract")
    if not isinstance(observed, Mapping):
        return [{"code": "EVALUATION_CONTRACT_MISSING"}]
    if not isinstance(task_bundle, Mapping):
        return [{"code": "EVALUATION_CONTRACT_TASK_BUNDLE_MISSING"}]
    rubric = task_bundle.get("rubric")
    rubric = rubric if isinstance(rubric, Mapping) else {}
    verifier_hashes = task_bundle.get("verifier_sha256")
    verifier_hashes = verifier_hashes if isinstance(verifier_hashes, Mapping) else {}
    control = task_bundle.get("control")
    control_files = control.get("files") if isinstance(control, Mapping) else None
    control_manifest_hash: str | None = None
    if isinstance(control_files, list):
        for entry in control_files:
            if isinstance(entry, Mapping) and entry.get("path") == "input-manifest.json":
                digest = entry.get("sha256")
                control_manifest_hash = digest if isinstance(digest, str) else None
                break
    expected = {
        "schema_version": "traceforge-evaluation-contract/v1",
        "rubric_sha256": rubric.get("sha256"),
        "verifier_sha256": verifier_hashes.get("grader.py"),
        "control_manifest_sha256": control_manifest_hash,
        "aggregation": rubric.get("aggregation"),
        "criteria": rubric.get("criteria"),
    }
    if any(value is None for value in expected.values()):
        return [{"code": "EVALUATION_CONTRACT_SOURCE_INCOMPLETE"}]
    if observed != expected:
        return [{"code": "EVALUATION_CONTRACT_MISMATCH"}]
    return []


def _validate_verdict(
    verdict: Any,
    *,
    reward_value: float | None,
) -> list[dict[str, Any]]:
    """校验独立 verifier 的正式结果信封及其与 reward 的一致性。"""

    if not isinstance(verdict, Mapping):
        return [{"code": "VERDICT_NOT_OBJECT"}]

    errors: list[dict[str, Any]] = []
    schema_version = verdict.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        errors.append({"code": "VERDICT_SCHEMA_INVALID"})

    status = verdict.get("status")
    if status not in {"TASK_PASS", "TASK_FAIL"}:
        errors.append({"code": "VERDICT_STATUS_INVALID", "status": status})
    elif reward_value is not None:
        expected = "TASK_PASS" if reward_value >= 1.0 else "TASK_FAIL"
        if status != expected:
            errors.append(
                {
                    "code": "REWARD_VERDICT_MISMATCH",
                    "reward_outcome": expected,
                    "verdict_status": status,
                }
            )

    reason_code = verdict.get("reason_code")
    if not isinstance(reason_code, str) or not reason_code.strip():
        errors.append({"code": "VERDICT_REASON_CODE_INVALID"})
    if not isinstance(verdict.get("details"), Mapping):
        errors.append({"code": "VERDICT_DETAILS_INVALID"})
    return errors


def _validate_artifact_manifest(
    trial_root: Path, manifest: Any
) -> tuple[bool, list[dict[str, Any]], dict[str, str]]:
    errors: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    # Harbor's native artifact collector emits a list of source/destination
    # records.  Accept that contract while retaining the stricter explicit
    # file/hash manifest below for TraceForge-produced manifests.
    if isinstance(manifest, list):
        if not manifest:
            return False, [{"code": "ARTIFACT_MANIFEST_EMPTY"}], hashes
        trial_root = trial_root.resolve()
        for index, entry in enumerate(manifest):
            if not isinstance(entry, Mapping):
                errors.append({"code": "ARTIFACT_ENTRY_SHAPE", "index": index})
                continue
            destination = entry.get("destination")
            kind = entry.get("type")
            if entry.get("status") not in {None, "ok"}:
                errors.append({"code": "ARTIFACT_STATUS_INVALID", "index": index})
                continue
            if not isinstance(destination, str) or not destination.strip():
                errors.append({"code": "ARTIFACT_DESTINATION_INVALID", "index": index})
                continue
            raw_path = Path(destination)
            if raw_path.is_absolute() or raw_path.parts[:1] != ("artifacts",) or ".." in raw_path.parts:
                errors.append({"code": "ARTIFACT_PATH_ABSOLUTE", "index": index})
                continue
            unresolved = trial_root / raw_path
            candidate = unresolved.resolve()
            try:
                candidate.relative_to(trial_root)
            except ValueError:
                errors.append({"code": "ARTIFACT_PATH_TRAVERSAL", "index": index})
                continue
            current = unresolved
            symlink_found = False
            while current != trial_root:
                if current.is_symlink():
                    symlink_found = True
                    break
                current = current.parent
            if symlink_found:
                errors.append({"code": "ARTIFACT_SYMLINK", "index": index})
                continue
            if kind == "directory":
                if not candidate.is_dir():
                    errors.append({"code": "ARTIFACT_DIRECTORY_MISSING", "index": index})
                    continue
                for child in candidate.rglob("*"):
                    if child.is_symlink():
                        errors.append({"code": "ARTIFACT_SYMLINK", "index": index})
                        break
                    if child.is_file():
                        if child.stat().st_nlink > 1:
                            errors.append({"code": "ARTIFACT_HARDLINK", "index": index})
                            break
                        relative = child.relative_to(trial_root).as_posix()
                        hashes[f"artifact:{relative}"] = hashlib.sha256(child.read_bytes()).hexdigest()
            elif kind == "file":
                if not candidate.is_file():
                    errors.append({"code": "ARTIFACT_FILE_MISSING", "index": index})
                    continue
                if candidate.stat().st_nlink > 1:
                    errors.append({"code": "ARTIFACT_HARDLINK", "index": index})
                    continue
                hashes[f"artifact:{raw_path.as_posix()}"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
            else:
                errors.append({"code": "ARTIFACT_TYPE_INVALID", "index": index})
        return not errors, errors, hashes
    if not isinstance(manifest, Mapping):
        return False, [{"code": "ARTIFACT_MANIFEST_NOT_OBJECT"}], hashes
    entries = manifest.get("files")
    if not isinstance(entries, list):
        entries = manifest.get("artifacts")
    if not isinstance(entries, list) or not entries:
        return False, [{"code": "ARTIFACT_MANIFEST_EMPTY"}], hashes
    artifact_root = (trial_root / "artifacts").resolve()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            errors.append({"code": "ARTIFACT_ENTRY_SHAPE", "index": index})
            continue
        raw_path = Path(entry["path"])
        if ".." in raw_path.parts:
            errors.append({"code": "ARTIFACT_PATH_TRAVERSAL", "index": index})
            continue
        if raw_path.is_absolute():
            errors.append({"code": "ARTIFACT_PATH_ABSOLUTE", "index": index})
            continue
        unresolved = artifact_root / raw_path
        current = unresolved
        symlink_found = False
        while current != artifact_root:
            if current.is_symlink():
                symlink_found = True
                break
            current = current.parent
        if symlink_found:
            errors.append({"code": "ARTIFACT_SYMLINK", "index": index})
            continue
        candidate = unresolved.resolve()
        try:
            candidate.relative_to(artifact_root)
        except ValueError:
            errors.append({"code": "ARTIFACT_PATH_TRAVERSAL", "index": index})
            continue
        if not candidate.is_file():
            errors.append({"code": "ARTIFACT_FILE_MISSING", "index": index})
            continue
        if candidate.stat().st_nlink > 1:
            errors.append({"code": "ARTIFACT_HARDLINK", "index": index})
            continue
        raw = candidate.read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        relative = candidate.relative_to(artifact_root).as_posix()
        hashes[f"artifact:{relative}"] = actual_hash
        expected_hash = entry.get("sha256")
        if expected_hash is not None and expected_hash != actual_hash:
            errors.append({"code": "ARTIFACT_HASH_MISMATCH", "index": index})
        expected_size = entry.get("size_bytes", entry.get("size"))
        if expected_size is not None and expected_size != len(raw):
            errors.append({"code": "ARTIFACT_SIZE_MISMATCH", "index": index})
    return not errors, errors, hashes


def _json_hash(path: Path) -> str:
    return canonical_json_sha256(_read_json(path))


def _explicit_trial_state(result: Any, verdict: Any) -> CertificationStatus | None:
    combined: list[str] = []
    for value in (result, verdict):
        if isinstance(value, Mapping):
            for key in ("status", "state", "error_type", "error_code", "outcome"):
                if value.get(key) is not None:
                    combined.append(str(value[key]).upper())
    state = " ".join(combined)
    if "CANCEL" in state:
        return CertificationStatus.CANCELLED
    if "TIMEOUT" in state:
        return CertificationStatus.INFRA_TIMEOUT
    if "BUDGET" in state or "MAX_TOKEN" in state:
        return CertificationStatus.TASK_BUDGET_EXHAUSTED
    if "CLEANUP_UNVERIFIABLE" in state:
        return CertificationStatus.CLEANUP_UNVERIFIABLE
    if "INFRA_ENVIRONMENT" in state or "ENVIRONMENT_ERROR" in state:
        return CertificationStatus.INFRA_ENVIRONMENT
    if "INFRA_AGENT" in state or "AGENT_ERROR" in state:
        return CertificationStatus.INFRA_AGENT
    if "INFRA_VERIFIER" in state or "VERIFIER_ERROR" in state:
        return CertificationStatus.INFRA_VERIFIER
    for value in (result, verdict):
        if not isinstance(value, Mapping):
            continue
        cleanup = value.get("cleanup")
        if isinstance(cleanup, Mapping) and cleanup.get("verified") is False:
            return CertificationStatus.CLEANUP_UNVERIFIABLE
        if value.get("cleanup_verified") is False:
            return CertificationStatus.CLEANUP_UNVERIFIABLE
    return None


def validate_harbor_trial(
    trial_dir: Path | str,
    *,
    secrets: Iterable[str] = (),
    preserve_source_literals: bool = False,
    write: bool = True,
) -> CertificationResult:
    """验证完整 Trial，且只写入 ``certification.json``。

    ``result.json`` 和 verifier 产物都是不可变输入。``TASK_FAIL`` 是可认证的
    任务结果；基础设施状态不进入任务成功率分母。原文保留模式仅允许哈希绑定输入中
    已有的常量；显式运行凭据仍然拒收。
    """

    root = Path(trial_dir).resolve()
    if not root.is_dir():
        raise TrialValidationError(f"Trial 目录不存在: {root}")
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checks: dict[str, bool] = {}
    hashes: dict[str, str] = {}
    public_source: Path | None = None

    root_required = ["config.json", "lock.json", "result.json"]
    missing_root = [name for name in root_required if not (root / name).is_file()]
    checks["harbor_metadata"] = not missing_root
    if missing_root:
        errors.append({"code": "HARBOR_METADATA_MISSING", "files": missing_root})

    config_json: Any = {}
    lock_json: Any = {}
    result_json: Any = {}
    for name in root_required:
        path = root / name
        if path.is_file():
            try:
                parsed = _read_json(path)
                hashes[name] = canonical_json_sha256(parsed)
                if name == "config.json":
                    config_json = parsed
                elif name == "lock.json":
                    lock_json = parsed
                elif name == "result.json":
                    result_json = parsed
            except TrialValidationError as exc:
                checks["harbor_metadata"] = False
                errors.append({"code": "INVALID_JSON", "file": name, "detail": str(exc)})

    capture_required = [
        "anthropic-exchanges.jsonl",
        "anthropic-sse.jsonl",
        "trajectory.full.json",
        "trajectory.json",
        "reconciliation.json",
        "projection_report.json",
    ]
    agent_required = [
        "hermes-session.jsonl",
        "hermes.stdout.log",
        "hermes.stderr.log",
        "task-input.json",
        "workspace-initial-manifest.json",
        "workspace-manifest.json",
    ]
    capture_missing = [name for name in capture_required if not (root / "agent" / name).is_file()]
    agent_missing = [name for name in agent_required if not (root / "agent" / name).is_file()]
    checks["capture_files"] = not capture_missing
    checks["agent_files"] = not agent_missing
    if capture_missing:
        errors.append({"code": "CAPTURE_FILES_MISSING", "files": capture_missing})
    if agent_missing:
        errors.append({"code": "AGENT_FILES_MISSING", "files": agent_missing})

    atif: Any = None
    full_trajectory: Any = None
    reconciliation: Any = None
    projection: Any = None
    task_input: Any = None
    workspace_initial: Any = None
    exchanges: list[Any] = []
    for name in (
        "trajectory.full.json",
        "trajectory.json",
        "reconciliation.json",
        "projection_report.json",
        "task-input.json",
        "workspace-initial-manifest.json",
        "workspace-manifest.json",
    ):
        path = root / "agent" / name
        if not path.is_file():
            continue
        try:
            parsed = _read_json(path)
            hashes[f"agent/{name}"] = canonical_json_sha256(parsed)
            if name == "trajectory.full.json":
                full_trajectory = parsed
            elif name == "trajectory.json":
                atif = parsed
            elif name == "reconciliation.json":
                reconciliation = parsed
            elif name == "projection_report.json":
                projection = parsed
            elif name == "task-input.json":
                task_input = parsed
            elif name == "workspace-initial-manifest.json":
                workspace_initial = parsed
        except TrialValidationError as exc:
            errors.append({"code": "INVALID_JSON", "file": f"agent/{name}", "detail": str(exc)})
            if name in {
                "task-input.json",
                "workspace-initial-manifest.json",
                "workspace-manifest.json",
            }:
                checks["agent_files"] = False
            else:
                checks["capture_files"] = False

    session_messages: list[Any] | None = None
    session_metadata: dict[str, Any] | None = None
    session_path = root / "agent" / "hermes-session.jsonl"
    if session_path.is_file():
        try:
            session_records = [
                json.loads(line)
                for line in session_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            hashes["agent/hermes-session.jsonl"] = hashlib.sha256(
                session_path.read_bytes()
            ).hexdigest()
            if (
                len(session_records) != 1
                or not isinstance(session_records[0], Mapping)
                or not isinstance(session_records[0].get("messages"), list)
            ):
                raise TrialValidationError(
                    "agent/hermes-session.jsonl 必须只含一个带 messages 的对象"
                )
            session_messages = session_records[0]["messages"]
            session_metadata = {
                key: value
                for key, value in session_records[0].items()
                if key not in {"messages", "conversation"}
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TrialValidationError) as exc:
            checks["agent_files"] = False
            errors.append(
                {
                    "code": "HERMES_SESSION_INVALID",
                    "detail": str(exc),
                }
            )

    exchange_path = root / "agent" / "anthropic-exchanges.jsonl"
    if exchange_path.is_file():
        try:
            for line_number, line in enumerate(
                exchange_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    exchanges.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise TrialValidationError(
                        f"agent/anthropic-exchanges.jsonl:{line_number} 不是有效 JSON"
                    ) from exc
            hashes["agent/anthropic-exchanges.jsonl"] = hashlib.sha256(
                exchange_path.read_bytes()
            ).hexdigest()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(
                {
                    "code": "INVALID_JSONL",
                    "file": "agent/anthropic-exchanges.jsonl",
                    "detail": str(exc),
                }
            )
        except TrialValidationError as exc:
            errors.append(
                {
                    "code": "INVALID_JSONL",
                    "file": "agent/anthropic-exchanges.jsonl",
                    "detail": str(exc),
                }
            )
    sse_records: list[Any] = []
    sse_path = root / "agent" / "anthropic-sse.jsonl"
    if sse_path.is_file():
        try:
            for line_number, line in enumerate(
                sse_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    sse_records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise TrialValidationError(
                        f"agent/anthropic-sse.jsonl:{line_number} 不是有效 JSON"
                    ) from exc
            hashes["agent/anthropic-sse.jsonl"] = hashlib.sha256(sse_path.read_bytes()).hexdigest()
        except (OSError, UnicodeDecodeError, TrialValidationError) as exc:
            errors.append(
                {
                    "code": "INVALID_JSONL",
                    "file": "agent/anthropic-sse.jsonl",
                    "detail": str(exc),
                }
            )
    capture_record_errors = _validate_capture_records(exchanges, sse_records)
    checks["capture_records"] = not capture_record_errors
    if capture_record_errors:
        errors.append({"code": "CAPTURE_RECORDS_INVALID", "issues": capture_record_errors})
    checks["model_exchange_present"] = bool(exchanges)
    if not exchanges:
        errors.append({"code": "MODEL_EXCHANGE_MISSING"})
    superseded_transport = _superseded_transport_exchange_ids(exchanges)
    if superseded_transport:
        warnings.append(
            {
                "code": "RETRY_SUPERSEDED_TRANSPORT_ERROR",
                "exchange_ids": sorted(superseded_transport),
            }
        )
    incomplete = [
        item.get("exchange_id")
        for item in exchanges
        if isinstance(item, Mapping)
        and item.get("exchange_id") not in superseded_transport
        and (item.get("response_status") == 200 and not item.get("complete"))
    ]
    checks["model_exchanges_complete"] = not incomplete
    if incomplete:
        errors.append({"code": "INCOMPLETE_MODEL_EXCHANGE", "exchange_ids": incomplete})

    task_input_errors = _validate_task_input_snapshot(task_input, workspace_initial)
    checks["task_input"] = not task_input_errors
    if task_input_errors:
        errors.append({"code": "TASK_INPUT_INVALID", "issues": task_input_errors})

    workspace_binding_errors: list[dict[str, Any]] = []
    task_bundle_contract: dict[str, Any] | None = None
    task_path: Path | None = None
    try:
        task_config = config_json.get("task") if isinstance(config_json, Mapping) else None
        task_path_value = task_config.get("path") if isinstance(task_config, Mapping) else None
        workspace_record = task_input.get("workspace") if isinstance(task_input, Mapping) else None
        logical_root = (
            workspace_record.get("root") if isinstance(workspace_record, Mapping) else None
        )
        if not isinstance(task_path_value, str) or not task_path_value:
            raise TrialValidationError("config.json 缺少 task.path")
        if not isinstance(logical_root, str) or not logical_root:
            raise TrialValidationError("task-input.json 缺少 workspace.root")
        task_path = Path(task_path_value).resolve()
        task_bundle_contract = validate_task_bundle(task_path)
        hashes["task/bundle"] = str(task_bundle_contract["sha256"])
        hashes["task/task.toml"] = str(task_bundle_contract["task_toml_sha256"])
        hashes["task/instruction.md"] = str(
            task_bundle_contract["instruction_sha256"]
        )
        hashes["task/tests"] = str(task_bundle_contract["tests"]["tree_sha256"])
        hashes["task/workspace"] = str(
            task_bundle_contract["workspace"]["tree_sha256"]
        )
        lock_task = lock_json.get("task") if isinstance(lock_json, Mapping) else None
        lock_task_path = lock_task.get("path") if isinstance(lock_task, Mapping) else None
        result_task_id = result_json.get("task_id") if isinstance(result_json, Mapping) else None
        result_task_path = (
            result_task_id.get("path") if isinstance(result_task_id, Mapping) else None
        )
        result_config = result_json.get("config") if isinstance(result_json, Mapping) else None
        result_config_task = (
            result_config.get("task") if isinstance(result_config, Mapping) else None
        )
        result_config_path = (
            result_config_task.get("path")
            if isinstance(result_config_task, Mapping)
            else None
        )
        observed_paths = (lock_task_path, result_task_path, result_config_path)
        if any(
            not isinstance(path_value, str)
            or Path(path_value).resolve() != task_path
            for path_value in observed_paths
        ):
            workspace_binding_errors.append({"code": "HARBOR_TASK_PATH_MISMATCH"})
        lock_digest = lock_task.get("digest") if isinstance(lock_task, Mapping) else None
        if not isinstance(lock_digest, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", lock_digest
        ) is None:
            workspace_binding_errors.append({"code": "HARBOR_TASK_DIGEST_MISSING"})
        result_checksum = (
            result_json.get("task_checksum") if isinstance(result_json, Mapping) else None
        )
        if not isinstance(result_checksum, str) or re.fullmatch(
            r"[0-9a-f]{64}", result_checksum
        ) is None:
            workspace_binding_errors.append({"code": "HARBOR_TASK_CHECKSUM_MISSING"})
        elif result_checksum != task_bundle_contract.get("harbor_task_checksum"):
            workspace_binding_errors.append({"code": "HARBOR_TASK_CHECKSUM_MISMATCH"})

        compile_manifest_path = task_path.parent / "compile-manifest.json"
        if compile_manifest_path.is_file():
            compile_manifest = _read_json(compile_manifest_path)
            hashes["task/compile-manifest.json"] = canonical_json_sha256(
                compile_manifest
            )
            manifest_tasks = (
                compile_manifest.get("tasks")
                if isinstance(compile_manifest, Mapping)
                else None
            )
            if (
                not isinstance(compile_manifest, Mapping)
                or compile_manifest.get("schema_version")
                != "traceforge-harbor-compiled-dataset/v1"
                or not isinstance(manifest_tasks, list)
                or compile_manifest.get("n_trials") != len(manifest_tasks)
            ):
                workspace_binding_errors.append(
                    {"code": "TASK_BUNDLE_COMPILE_MANIFEST_SCHEMA"}
                )
            manifest_entries = (
                [
                    entry
                    for entry in manifest_tasks
                    if isinstance(entry, Mapping)
                    and entry.get("task_dir") == task_path.name
                ]
                if isinstance(manifest_tasks, list)
                else []
            )
            manifest_entry = manifest_entries[0] if len(manifest_entries) == 1 else None
            manifest_task_dirs = (
                [
                    entry.get("task_dir")
                    for entry in manifest_tasks
                    if isinstance(entry, Mapping)
                ]
                if isinstance(manifest_tasks, list)
                else []
            )
            manifest_task_names = (
                [
                    entry.get("task_name")
                    for entry in manifest_tasks
                    if isinstance(entry, Mapping)
                ]
                if isinstance(manifest_tasks, list)
                else []
            )
            # A replicated dataset may intentionally reuse the same logical task
            # name for identical trials.  Directory identities must remain unique;
            # a duplicate name is valid only when its bundle contract is identical.
            duplicate_name_contracts: dict[str, set[str]] = {}
            for entry in manifest_tasks if isinstance(manifest_tasks, list) else []:
                if not isinstance(entry, Mapping):
                    continue
                name = entry.get("task_name")
                bundle = entry.get("task_bundle")
                if not isinstance(name, str) or not isinstance(bundle, Mapping):
                    continue
                normalized_bundle = dict(bundle)
                normalized_bundle.pop("task_bundle_root", None)
                duplicate_name_contracts.setdefault(name, set()).add(
                    canonical_json_sha256(normalized_bundle)
                )
            manifest_identities_valid = (
                all(isinstance(value, str) and value for value in manifest_task_dirs)
                and all(
                    isinstance(value, str) and value
                    for value in manifest_task_names
                )
                and len(manifest_task_dirs) == len(set(manifest_task_dirs))
                and all(len(values) == 1 for values in duplicate_name_contracts.values())
            )
            if (
                not isinstance(manifest_entry, Mapping)
                or not manifest_identities_valid
                or manifest_entry.get("task_name")
                != task_bundle_contract.get("task_name")
                or manifest_entry.get("task_bundle") != task_bundle_contract
            ):
                workspace_binding_errors.append(
                    {"code": "TASK_BUNDLE_COMPILE_MANIFEST_MISMATCH"}
                )
        else:
            workspace_binding_errors.append(
                {"code": "TASK_BUNDLE_COMPILE_MANIFEST_MISSING"}
            )

        instruction_path = task_path / "instruction.md"
        if not instruction_path.is_file():
            raise TrialValidationError("Harbor Task 缺少 instruction.md")
        source_instruction = _strip_leading_canary(
            instruction_path.read_text(encoding="utf-8")
        )
        input_instruction = (
            task_input.get("instruction") if isinstance(task_input, Mapping) else None
        )
        task_component = (
            input_instruction.get("task_instruction")
            if isinstance(input_instruction, Mapping)
            else None
        )
        captured_instruction = (
            task_component.get("content") if isinstance(task_component, Mapping) else None
        )
        if captured_instruction != source_instruction:
            workspace_binding_errors.append({"code": "TASK_INSTRUCTION_SOURCE_MISMATCH"})

        public_source = task_path / "workspace"
        if not public_source.is_dir():
            raise TrialValidationError("正式 Harbor Task 缺少 workspace/")
        expected_workspace = _workspace_snapshot_from_directory(
            public_source,
            logical_root=logical_root,
        )
        hashes["expected/workspace-initial"] = canonical_json_sha256(expected_workspace)
        if expected_workspace != workspace_initial:
            workspace_binding_errors.append({"code": "WORKSPACE_INITIAL_SOURCE_MISMATCH"})
    except (OSError, TaskBundleError, TrialValidationError) as exc:
        workspace_binding_errors.append(
            {"code": "WORKSPACE_INITIAL_SOURCE_UNAVAILABLE", "detail": str(exc)}
        )
    checks["workspace_binding"] = not workspace_binding_errors
    checks["task_bundle"] = (
        task_bundle_contract is not None and not workspace_binding_errors
    )
    if workspace_binding_errors:
        errors.append({"code": "WORKSPACE_BINDING_INVALID", "issues": workspace_binding_errors})

    full_structure_errors: list[dict[str, Any]] = []
    if not isinstance(full_trajectory, Mapping):
        full_structure_errors.append({"code": "FULL_TRAJECTORY_NOT_OBJECT"})
    else:
        if full_trajectory.get("schema_version") != "traceforge-lossless-trajectory-v1":
            full_structure_errors.append({"code": "FULL_TRAJECTORY_SCHEMA"})
        system_prompt = full_trajectory.get("system_prompt")
        if not _content_present(system_prompt):
            full_structure_errors.append({"code": "FULL_SYSTEM_PROMPT_MISSING"})
        tools = full_trajectory.get("tools")
        if not isinstance(tools, list) or not tools:
            full_structure_errors.append({"code": "FULL_TRAJECTORY_FIELD", "field": "tools"})
        for field_name in ("messages", "anthropic_calls"):
            if not isinstance(full_trajectory.get(field_name), list):
                full_structure_errors.append({"code": "FULL_TRAJECTORY_FIELD", "field": field_name})
        normalized_messages = full_trajectory.get("messages")
        if isinstance(normalized_messages, list):
            roles = {
                message.get("role")
                for message in normalized_messages
                if isinstance(message, Mapping)
            }
            if "user" not in roles:
                full_structure_errors.append({"code": "FULL_USER_MESSAGE_MISSING"})
            if "assistant" not in roles:
                full_structure_errors.append({"code": "FULL_ASSISTANT_MESSAGE_MISSING"})
            for index, message in enumerate(normalized_messages):
                if not isinstance(message, Mapping):
                    full_structure_errors.append(
                        {"code": "FULL_MESSAGE_NOT_OBJECT", "index": index}
                    )
                    continue
                if message.get("role") not in {"system", "user", "assistant", "tool"}:
                    full_structure_errors.append(
                        {
                            "code": "FULL_MESSAGE_ROLE",
                            "index": index,
                            "role": message.get("role"),
                        }
                    )
        if not isinstance(full_trajectory.get("usage"), Mapping):
            full_structure_errors.append({"code": "FULL_TRAJECTORY_FIELD", "field": "usage"})
        full_hashes = full_trajectory.get("hashes")
        if not isinstance(full_hashes, Mapping):
            full_structure_errors.append({"code": "FULL_HASHES_MISSING"})
        else:
            expected_hashes = {
                "system_prompt_sha256": canonical_json_sha256(system_prompt),
                "tool_definitions_sha256": canonical_json_sha256(tools),
                "messages_sha256": canonical_json_sha256(normalized_messages),
                "task_input_sha256": canonical_json_sha256(task_input),
            }
            for field_name, expected_hash in expected_hashes.items():
                if full_hashes.get(field_name) != expected_hash:
                    full_structure_errors.append(
                        {"code": "FULL_FIELD_HASH_MISMATCH", "field": field_name}
                    )
        drift = full_trajectory.get("harness_drift")
        if not isinstance(drift, list):
            full_structure_errors.append({"code": "HARNESS_DRIFT_FIELD"})
        normalized_calls = full_trajectory.get("anthropic_calls")
        first_request: Any = None
        if isinstance(normalized_calls, list):
            expected_call_usages: list[dict[str, int | None]] = []
            compaction_detected = _context_compaction_detected(
                normalized_messages, normalized_calls
            )
            if len(normalized_calls) != len(exchanges):
                full_structure_errors.append({"code": "FULL_EXCHANGE_COUNT_MISMATCH"})
            for index, exchange in enumerate(exchanges):
                if index >= len(normalized_calls) or not isinstance(
                    normalized_calls[index], Mapping
                ):
                    continue
                if normalized_calls[index].get("raw_exchange_sha256") != canonical_json_sha256(
                    exchange
                ):
                    full_structure_errors.append(
                        {"code": "FULL_EXCHANGE_HASH_MISMATCH", "index": index}
                    )
                captured_request = exchange.get("request")
                captured_request = (
                    captured_request.get("json") if isinstance(captured_request, Mapping) else None
                )
                normalized_request = normalized_calls[index].get("request")
                if captured_request != normalized_request:
                    full_structure_errors.append(
                        {"code": "FULL_EXCHANGE_REQUEST_MISMATCH", "index": index}
                    )
                if not isinstance(normalized_request, Mapping):
                    full_structure_errors.append(
                        {"code": "FULL_EXCHANGE_REQUEST_SHAPE", "index": index}
                    )
                else:
                    if (
                        not isinstance(normalized_request.get("model"), str)
                        or not normalized_request.get("model")
                    ):
                        full_structure_errors.append(
                            {
                                "code": "FULL_EXCHANGE_REQUEST_FIELD",
                                "index": index,
                                "field": "model",
                            }
                        )
                    if not _content_present(normalized_request.get("system")):
                        full_structure_errors.append(
                            {
                                "code": "FULL_EXCHANGE_REQUEST_FIELD",
                                "index": index,
                                "field": "system",
                            }
                        )
                    if (
                        not _terminal_text_only_call(
                            normalized_calls[index], index, len(normalized_calls)
                        )
                        and (
                            not isinstance(normalized_request.get("tools"), list)
                            or not normalized_request.get("tools")
                        )
                    ):
                        full_structure_errors.append(
                            {
                                "code": "FULL_EXCHANGE_REQUEST_FIELD",
                                "index": index,
                                "field": "tools",
                            }
                        )
                    if not isinstance(normalized_request.get("messages"), list) or not (
                        normalized_request.get("messages")
                    ):
                        full_structure_errors.append(
                            {
                                "code": "FULL_EXCHANGE_REQUEST_FIELD",
                                "index": index,
                                "field": "messages",
                            }
                        )
                captured_response = _captured_response(exchange)
                if normalized_calls[index].get("response") != captured_response:
                    full_structure_errors.append(
                        {"code": "FULL_EXCHANGE_RESPONSE_MISMATCH", "index": index}
                    )
                expected_derived = {
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
                }
                for field_name, expected_value in expected_derived.items():
                    if normalized_calls[index].get(field_name) != expected_value:
                        full_structure_errors.append(
                            {
                                "code": "FULL_EXCHANGE_DERIVED_FIELD_MISMATCH",
                                "index": index,
                                "field": field_name,
                            }
                        )
                try:
                    expected_usage = normalize_usage(
                        captured_response.get("usage")
                        if isinstance(captured_response.get("usage"), Mapping)
                        else None
                    )
                except EvidenceError:
                    expected_usage = {
                        "input_tokens": None,
                        "output_tokens": None,
                        "cache_creation_input_tokens": None,
                        "cache_read_input_tokens": None,
                    }
                    full_structure_errors.append(
                        {"code": "FULL_EXCHANGE_USAGE_SOURCE_INVALID", "index": index}
                    )
                expected_call_usages.append(expected_usage)
                if normalized_calls[index].get("usage") != expected_usage:
                    full_structure_errors.append(
                        {"code": "FULL_EXCHANGE_USAGE_MISMATCH", "index": index}
                    )
            expected_full_usage = {
                "calls": expected_call_usages,
                "total": aggregate_usage(expected_call_usages),
            }
            if full_trajectory.get("usage") != expected_full_usage:
                full_structure_errors.append({"code": "FULL_USAGE_MISMATCH"})
            if normalized_calls:
                first_call = normalized_calls[0]
                first_request = (
                    first_call.get("request") if isinstance(first_call, Mapping) else None
                )
                if not isinstance(first_request, Mapping):
                    full_structure_errors.append({"code": "FULL_FIRST_REQUEST_MISSING"})
                else:
                    if first_request.get("system") != system_prompt:
                        full_structure_errors.append(
                            {"code": "FULL_SYSTEM_PROMPT_REQUEST_MISMATCH"}
                        )
                    if first_request.get("tools") != tools:
                        full_structure_errors.append(
                            {"code": "FULL_TOOL_DEFINITIONS_REQUEST_MISMATCH"}
                        )
                first_system_hash = canonical_json_sha256(system_prompt)
                first_tools_hash = canonical_json_sha256(tools)
                first_model = (
                    first_request.get("model")
                    if isinstance(first_request, Mapping)
                    else None
                )
                computed_drift: list[dict[str, Any]] = []
                for index, normalized_call in enumerate(normalized_calls):
                    if _terminal_text_only_call(
                        normalized_call, index, len(normalized_calls)
                    ):
                        continue
                    request = (
                        normalized_call.get("request")
                        if isinstance(normalized_call, Mapping)
                        else None
                    )
                    if not isinstance(request, Mapping):
                        continue
                    system_hash = canonical_json_sha256(request.get("system"))
                    tools_hash = canonical_json_sha256(request.get("tools") or [])
                    if request.get("model") != first_model:
                        full_structure_errors.append(
                            {"code": "FULL_MODEL_DRIFT", "call_index": index}
                        )
                    if system_hash != first_system_hash or tools_hash != first_tools_hash:
                        computed_drift.append(
                            {
                                "call_index": index,
                                "code": "HARNESS_DRIFT",
                                "system_prompt_sha256": system_hash,
                                "tool_definitions_sha256": tools_hash,
                            }
                        )
                if drift != computed_drift:
                    full_structure_errors.append({"code": "HARNESS_DRIFT_RECOMPUTE_MISMATCH"})
                if computed_drift:
                    full_structure_errors.append(
                        {"code": "HARNESS_DRIFT", "calls": computed_drift}
                    )
            assistant_messages = [
                message
                for message in normalized_messages
                if isinstance(message, Mapping) and message.get("role") == "assistant"
            ] if isinstance(normalized_messages, list) else []
            successful_count = sum(is_assistant_response(call) for call in normalized_calls)
            if len(assistant_messages) != successful_count and not compaction_detected:
                full_structure_errors.append({"code": "FULL_ASSISTANT_RESPONSE_COUNT_MISMATCH"})
            assistant_by_call = {
                message.get("_anthropic_call_index"): message
                for message in assistant_messages
                if isinstance(message.get("_anthropic_call_index"), int)
            }
            for index, normalized_call in enumerate(normalized_calls):
                assistant = assistant_by_call.get(index)
                if assistant is None or not isinstance(normalized_call, Mapping):
                    if compaction_detected:
                        continue
                    continue
                response = normalized_call.get("response")
                response = response if isinstance(response, Mapping) else {}
                if assistant.get("anthropic_response") != response:
                    full_structure_errors.append(
                        {"code": "FULL_ASSISTANT_RESPONSE_MISMATCH", "index": index}
                    )
                if assistant.get("anthropic_usage") != normalized_call.get("usage"):
                    full_structure_errors.append(
                        {"code": "FULL_ASSISTANT_USAGE_MISMATCH", "index": index}
                    )
                thinking = "".join(
                    str(block.get("thinking"))
                    for block in (response.get("content") or [])
                    if isinstance(block, Mapping)
                    and block.get("type") == "thinking"
                    and isinstance(block.get("thinking"), str)
                )
                captured_reasoning = assistant.get("anthropic_reasoning_content")
                if captured_reasoning != (thinking or None):
                    full_structure_errors.append(
                        {"code": "FULL_ASSISTANT_THINKING_MISMATCH", "index": index}
                    )
                for field_name in ("reasoning", "reasoning_content"):
                    native_reasoning = assistant.get(field_name)
                    if native_reasoning not in (None, "") and native_reasoning != thinking:
                        full_structure_errors.append(
                            {
                                "code": "FULL_ASSISTANT_NATIVE_REASONING_MISMATCH",
                                "index": index,
                                "field": field_name,
                            }
                        )
                expected_text, expected_tool_calls = _response_assistant_projection(response)
                if assistant.get("content") != expected_text:
                    full_structure_errors.append(
                        {"code": "FULL_ASSISTANT_TEXT_MISMATCH", "index": index}
                    )
                if _hermes_assistant_tool_calls(assistant) != expected_tool_calls:
                    full_structure_errors.append(
                        {"code": "FULL_ASSISTANT_TOOL_CALL_MISMATCH", "index": index}
                    )
                expected_finish_reason = {
                    "tool_use": "tool_calls",
                    "end_turn": "stop",
                    "max_tokens": "length",
                }.get(str(response.get("stop_reason") or ""))
                if (
                    assistant.get("finish_reason") is not None
                    and expected_finish_reason is not None
                    and assistant.get("finish_reason") != expected_finish_reason
                ):
                    full_structure_errors.append(
                        {"code": "FULL_ASSISTANT_FINISH_REASON_MISMATCH", "index": index}
                    )
            result_calls = normalized_calls
            if compaction_detected:
                result_calls = [
                    call
                    for index, call in enumerate(normalized_calls)
                    if index in {
                        message.get("_anthropic_call_index")
                        for message in assistant_messages
                        if isinstance(message.get("_anthropic_call_index"), int)
                    }
                ]
            request_tool_results, request_tool_result_conflict = (
                _tool_result_map_from_requests(result_calls)
            )
            hermes_tool_results, hermes_tool_result_conflict = (
                _tool_result_map_from_messages(normalized_messages)
            )
            if compaction_detected:
                # Each represented request repeats the visible history. Keep
                # only results that survive in Hermes after compaction; the
                # repeated capture IDs are expected and are checked by the
                # evidence reconciler.
                request_tool_results = {
                    key: value
                    for key, value in request_tool_results.items()
                    if key in hermes_tool_results
                }
                request_tool_result_conflict = False
            if (
                request_tool_result_conflict
                or hermes_tool_result_conflict
                or request_tool_results != hermes_tool_results
            ):
                full_structure_errors.append({"code": "FULL_TOOL_RESULT_CONTENT_MISMATCH"})
            request_transcript, request_transcript_invalid = _anthropic_transcript(
                normalized_calls
            )
            hermes_transcript, hermes_transcript_invalid = _hermes_transcript(
                normalized_messages
            )
            terminal_call = (
                _terminal_text_only_call(
                    normalized_calls[-1], len(normalized_calls) - 1, len(normalized_calls)
                )
                if normalized_calls
                else False
            )
            if (
                not terminal_call
                and (
                    request_transcript_invalid
                    or hermes_transcript_invalid
                    or request_transcript != hermes_transcript
                )
            ):
                full_structure_errors.append({"code": "FULL_TRANSCRIPT_BOUNDARY_MISMATCH"})
            call_prefix_issues = _call_transcript_prefix_issues(
                normalized_calls,
                normalized_messages,
            )
            if call_prefix_issues:
                full_structure_errors.append(
                    {
                        "code": "FULL_CALL_TRANSCRIPT_PREFIX_INVALID",
                        "issues": call_prefix_issues,
                    }
                )
        if full_trajectory.get("task_input") != task_input:
            full_structure_errors.append({"code": "FULL_TASK_INPUT_MISMATCH"})
        full_metadata = full_trajectory.get("metadata")
        full_metadata = full_metadata if isinstance(full_metadata, Mapping) else {}
        if full_metadata.get("input_profile") != TASK_BUNDLE_INPUT_PROFILE:
            full_structure_errors.append({"code": "FULL_INPUT_PROFILE"})

        instruction = task_input.get("instruction") if isinstance(task_input, Mapping) else None
        instruction = instruction if isinstance(instruction, Mapping) else {}
        rendered = instruction.get("rendered_user_prompt")
        rendered = rendered if isinstance(rendered, Mapping) else {}
        rendered_prompt = rendered.get("content")
        rendered_digest = rendered.get("sha256")
        if full_metadata.get("rendered_user_prompt_sha256") != rendered_digest:
            full_structure_errors.append({"code": "FULL_INPUT_METADATA_DIGEST"})
        task_runtime = task_input.get("runtime") if isinstance(task_input, Mapping) else None
        task_runtime = task_runtime if isinstance(task_runtime, Mapping) else {}
        request_model = first_request.get("model") if isinstance(first_request, Mapping) else None
        if not (
            task_runtime.get("model")
            == full_trajectory.get("model")
            == full_metadata.get("model")
            == request_model
        ):
            full_structure_errors.append({"code": "FULL_INPUT_MODEL_MISMATCH"})
        if full_metadata.get("provider") != task_runtime.get("provider"):
            full_structure_errors.append({"code": "FULL_INPUT_PROVIDER_MISMATCH"})
        hermes_session_metadata = full_trajectory.get("hermes_session_metadata")
        hermes_session_metadata = (
            hermes_session_metadata
            if isinstance(hermes_session_metadata, Mapping)
            else {}
        )
        if hermes_session_metadata != session_metadata:
            full_structure_errors.append({"code": "FULL_HERMES_SESSION_METADATA_MISMATCH"})
        hermes_native_meta = hermes_session_metadata.get("meta")
        hermes_native_meta = (
            hermes_native_meta if isinstance(hermes_native_meta, Mapping) else {}
        )
        if not (
            task_runtime.get("task_id")
            == full_metadata.get("session_id")
            == hermes_native_meta.get("task_id")
        ):
            full_structure_errors.append({"code": "FULL_INPUT_TASK_ID_MISMATCH"})
        if task_runtime.get("toolsets") != hermes_native_meta.get("toolsets"):
            full_structure_errors.append({"code": "FULL_INPUT_TOOLSETS_MISMATCH"})
        session_first_user = _first_user_text(session_messages)
        full_first_user = _first_user_text(normalized_messages)
        first_request_messages = (
            first_request.get("messages") if isinstance(first_request, Mapping) else None
        )
        request_first_user = _first_user_text(first_request_messages)
        if not isinstance(rendered_prompt, str) or any(
            value != rendered_prompt
            for value in (session_first_user, full_first_user, request_first_user)
        ):
            full_structure_errors.append(
                {
                    "code": "FULL_RENDERED_USER_PROMPT_MISMATCH",
                    "boundaries": ["task_input", "hermes", "full", "anthropic"],
                }
            )
        input_alignment = full_trajectory.get("input_alignment")
        expected_alignment = {
            "task_input_present": isinstance(task_input, Mapping),
            "rendered_user_prompt_present": isinstance(rendered_prompt, str),
            "hermes_first_user_matches": session_first_user == rendered_prompt,
            "anthropic_first_user_matches": request_first_user == rendered_prompt,
            "system_prompt_captured": _content_present(system_prompt),
            "tool_definitions_captured": isinstance(tools, list) and bool(tools),
        }
        if not isinstance(input_alignment, Mapping) or any(
            input_alignment.get(name) is not expected
            for name, expected in expected_alignment.items()
        ):
            full_structure_errors.append({"code": "FULL_INPUT_ALIGNMENT"})

        if isinstance(session_messages, list):
            if not isinstance(normalized_messages, list) or len(session_messages) != len(
                normalized_messages
            ):
                full_structure_errors.append({"code": "FULL_HERMES_MESSAGE_COUNT_MISMATCH"})
            elif any(
                not _mapping_contains(normalized, native)
                for normalized, native in zip(
                    normalized_messages,
                    session_messages,
                    strict=True,
                )
            ):
                full_structure_errors.append({"code": "FULL_HERMES_MESSAGES_MISMATCH"})
    checks["full_trajectory"] = not full_structure_errors
    if full_structure_errors:
        errors.append({"code": "FULL_TRAJECTORY_INVALID", "issues": full_structure_errors})

    atif_errors = validate_atif_v17(atif) if atif is not None else [{"code": "ATIF_MISSING"}]
    checks["atif_v17"] = not atif_errors
    if atif_errors:
        errors.append({"code": "ATIF_INVALID", "issues": atif_errors})
    checks["projection"] = bool(isinstance(projection, Mapping) and projection.get("ok") is True)
    if projection is not None and not checks["projection"]:
        errors.append({"code": "PROJECTION_FAILED"})
    checks["reconciliation"] = bool(
        isinstance(reconciliation, Mapping) and reconciliation.get("ok") is True
    )
    if isinstance(full_trajectory, Mapping) and isinstance(atif, Mapping):
        full_hash = canonical_json_sha256(full_trajectory)
        atif_hash = canonical_json_sha256(atif)
        if isinstance(reconciliation, Mapping):
            reconciliation_hashes = reconciliation.get("hashes")
            if not isinstance(reconciliation_hashes, Mapping) or (
                reconciliation_hashes.get("full_trajectory_sha256") != full_hash
                or reconciliation_hashes.get("atif_sha256") != atif_hash
            ):
                checks["reconciliation"] = False
                errors.append({"code": "RECONCILIATION_HASH_MISMATCH"})
        if isinstance(projection, Mapping) and (
            projection.get("source_sha256") != full_hash
            or projection.get("projection_sha256") != atif_hash
        ):
            checks["projection"] = False
            errors.append({"code": "PROJECTION_HASH_MISMATCH"})
        full_hashes = full_trajectory.get("hashes")
        atif_agent = atif.get("agent")
        atif_agent_extra = atif_agent.get("extra") if isinstance(atif_agent, Mapping) else None
        if (
            not isinstance(full_hashes, Mapping)
            or not isinstance(atif_agent_extra, Mapping)
            or atif_agent_extra.get("system_prompt_sha256")
            != full_hashes.get("system_prompt_sha256")
        ):
            checks["projection"] = False
            errors.append({"code": "ATIF_SYSTEM_PROMPT_HASH_MISMATCH"})
        atif_extra = atif.get("extra")
        task_instruction = (
            task_input.get("instruction") if isinstance(task_input, Mapping) else None
        )
        task_instruction = task_instruction if isinstance(task_instruction, Mapping) else {}
        rendered_record = task_instruction.get("rendered_user_prompt")
        rendered_record = rendered_record if isinstance(rendered_record, Mapping) else {}
        workspace_record = task_input.get("workspace") if isinstance(task_input, Mapping) else None
        workspace_record = workspace_record if isinstance(workspace_record, Mapping) else {}
        expected_atif_extra = {
            "tool_definitions_sha256": canonical_json_sha256(
                full_trajectory.get("tools") or []
            ),
            "task_input_sha256": canonical_json_sha256(task_input),
            "rendered_user_prompt_sha256": rendered_record.get("sha256"),
            "workspace_initial_tree_sha256": workspace_record.get("tree_sha256"),
        }
        if not isinstance(atif_extra, Mapping) or any(
            atif_extra.get(name) != digest for name, digest in expected_atif_extra.items()
        ):
            checks["projection"] = False
            errors.append({"code": "ATIF_INPUT_PROJECTION_MISMATCH"})
        atif_steps = atif.get("steps")
        atif_steps = atif_steps if isinstance(atif_steps, list) else []
        atif_first_user = next(
            (
                step.get("message")
                for step in atif_steps
                if isinstance(step, Mapping) and step.get("source") == "user"
            ),
            None,
        )
        if atif_first_user != rendered_record.get("content"):
            checks["projection"] = False
            errors.append({"code": "ATIF_RENDERED_USER_PROMPT_MISMATCH"})
        try:
            fresh_atif, fresh_projection = project_atif_v17(
                full_trajectory,
                include_report=True,
            )
            fresh_reconciliation = reconcile_evidence(
                full_trajectory, fresh_atif, exchanges=exchanges
            )
        except Exception as exc:
            checks["projection"] = False
            checks["reconciliation"] = False
            errors.append(
                {
                    "code": "EVIDENCE_FRESH_RECOMPUTE_FAILED",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            if fresh_atif != atif:
                checks["projection"] = False
                errors.append({"code": "ATIF_FRESH_PROJECTION_MISMATCH"})
            stored_projection = dict(projection) if isinstance(projection, Mapping) else {}
            comparable_projection = dict(fresh_projection)
            stored_projection.pop("generated_at", None)
            comparable_projection.pop("generated_at", None)
            if stored_projection != comparable_projection:
                checks["projection"] = False
                errors.append({"code": "PROJECTION_FRESH_RECOMPUTE_MISMATCH"})
            stored_reconciliation = (
                dict(reconciliation) if isinstance(reconciliation, Mapping) else {}
            )
            comparable_reconciliation = dict(fresh_reconciliation)
            stored_reconciliation.pop("generated_at", None)
            comparable_reconciliation.pop("generated_at", None)
            if stored_reconciliation != comparable_reconciliation:
                checks["reconciliation"] = False
                errors.append({"code": "RECONCILIATION_FRESH_RECOMPUTE_MISMATCH"})
    if reconciliation is not None and not checks["reconciliation"]:
        errors.append(
            {
                "code": "RECONCILIATION_FAILED",
                "issues": reconciliation.get("issues")
                if isinstance(reconciliation, Mapping)
                else None,
            }
        )

    verifier_required = ["reward.json", "verdict.json"]
    verifier_missing = [
        name for name in verifier_required if not (root / "verifier" / name).is_file()
    ]
    checks["verifier_files"] = not verifier_missing
    reward_value: float | None = None
    verdict: Any = {}
    if verifier_missing:
        errors.append({"code": "VERIFIER_FILES_MISSING", "files": verifier_missing})
    else:
        try:
            reward = _read_json(root / "verifier" / "reward.json")
            verdict = _read_json(root / "verifier" / "verdict.json")
            hashes["verifier/reward.json"] = canonical_json_sha256(reward)
            hashes["verifier/verdict.json"] = canonical_json_sha256(verdict)
            reward_ok, reward_value, reward_errors = _validate_reward(reward)
            checks["reward"] = reward_ok
            errors.extend(reward_errors)
            verdict_errors = _validate_verdict(
                verdict,
                reward_value=reward_value if reward_ok else None,
            )
            checks["verdict"] = not verdict_errors
            errors.extend(verdict_errors)
            evaluation_contract_errors = _validate_evaluation_contract(
                verdict,
                task_bundle_contract,
            )
            checks["evaluation_contract"] = not evaluation_contract_errors
            errors.extend(evaluation_contract_errors)
        except TrialValidationError as exc:
            checks["reward"] = False
            errors.append({"code": "VERIFIER_JSON_INVALID", "detail": str(exc)})

    manifest_path = root / "artifacts" / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = _read_json(manifest_path)
            manifest_ok, manifest_errors, artifact_hashes = _validate_artifact_manifest(
                root, manifest
            )
            checks["artifacts"] = manifest_ok
            errors.extend(manifest_errors)
            hashes["artifacts/manifest.json"] = canonical_json_sha256(manifest)
            hashes.update(artifact_hashes)
        except TrialValidationError as exc:
            checks["artifacts"] = False
            errors.append({"code": "ARTIFACT_MANIFEST_INVALID", "detail": str(exc)})
    else:
        checks["artifacts"] = False
        errors.append({"code": "ARTIFACT_MANIFEST_MISSING"})

    findings = scan_secrets(root, secrets=secrets)
    blocked_findings = findings
    if preserve_source_literals and checks["workspace_binding"] and public_source is not None:
        source_fingerprints = _source_literal_fingerprints(root, public_source, findings)
        blocked_findings = [
            item for item in findings
            if (item.detector, item.match_sha256) not in source_fingerprints
        ]
        if len(blocked_findings) != len(findings):
            warnings.append({
                "code": "SOURCE_LITERALS_PRESERVED",
                "count": len(findings) - len(blocked_findings),
                "source_workspace_sha256": hashes["expected/workspace-initial"],
            })
    checks["secret_scan"] = not blocked_findings
    if blocked_findings:
        errors.append({"code": "SECRET_LEAK", "count": len(blocked_findings)})

    explicit_status = _explicit_trial_state(result_json, verdict)
    error_codes = {str(item.get("code")) for item in errors}
    if explicit_status is not None:
        status = explicit_status
    elif not checks.get("harbor_metadata", False):
        status = CertificationStatus.INFRA_ENVIRONMENT
    elif "WORKSPACE_BINDING_INVALID" in error_codes:
        status = CertificationStatus.INFRA_ENVIRONMENT
    elif "SECRET_LEAK" in error_codes or any(
        code in error_codes
        for code in {
            "CAPTURE_FILES_MISSING",
            "CAPTURE_RECORDS_INVALID",
            "HERMES_SESSION_INVALID",
            "MODEL_EXCHANGE_MISSING",
            "INCOMPLETE_MODEL_EXCHANGE",
            "TASK_INPUT_INVALID",
            "FULL_TRAJECTORY_INVALID",
            "ATIF_INVALID",
            "ATIF_INPUT_PROJECTION_MISMATCH",
            "ATIF_RENDERED_USER_PROMPT_MISMATCH",
            "ATIF_FRESH_PROJECTION_MISMATCH",
            "PROJECTION_FRESH_RECOMPUTE_MISMATCH",
            "RECONCILIATION_FRESH_RECOMPUTE_MISMATCH",
            "EVIDENCE_FRESH_RECOMPUTE_FAILED",
            "PROJECTION_FAILED",
            "PROJECTION_HASH_MISMATCH",
            "RECONCILIATION_FAILED",
            "RECONCILIATION_HASH_MISMATCH",
        }
    ):
        status = CertificationStatus.INFRA_CAPTURE
    elif "AGENT_FILES_MISSING" in error_codes or any(
        code in error_codes
        for code in {
            "ARTIFACT_MANIFEST_MISSING",
            "ARTIFACT_MANIFEST_INVALID",
            "ARTIFACT_MANIFEST_NOT_OBJECT",
            "ARTIFACT_MANIFEST_EMPTY",
            "ARTIFACT_ENTRY_SHAPE",
            "ARTIFACT_PATH_ABSOLUTE",
            "ARTIFACT_PATH_TRAVERSAL",
            "ARTIFACT_SYMLINK",
            "ARTIFACT_HARDLINK",
            "ARTIFACT_FILE_MISSING",
            "ARTIFACT_HASH_MISMATCH",
            "ARTIFACT_SIZE_MISMATCH",
        }
    ):
        status = CertificationStatus.INFRA_AGENT
    elif any(
        code in error_codes
        for code in {
            "VERIFIER_FILES_MISSING",
            "VERIFIER_JSON_INVALID",
            "REWARD_NOT_FLAT_OBJECT",
            "REWARD_NON_NUMERIC",
            "REWARD_NON_FINITE",
            "REWARD_OUT_OF_RANGE",
            "REWARD_TASK_MISSING",
            "REWARD_CRITERIA_MISMATCH",
            "REWARD_VERDICT_MISMATCH",
            "VERDICT_NOT_OBJECT",
            "VERDICT_SCHEMA_INVALID",
            "VERDICT_STATUS_INVALID",
            "VERDICT_REASON_CODE_INVALID",
            "VERDICT_DETAILS_INVALID",
            "EVALUATION_CONTRACT_VERDICT_SHAPE",
            "EVALUATION_CONTRACT_MISSING",
            "EVALUATION_CONTRACT_TASK_BUNDLE_MISSING",
            "EVALUATION_CONTRACT_SOURCE_INCOMPLETE",
            "EVALUATION_CONTRACT_MISMATCH",
        }
    ):
        status = CertificationStatus.INFRA_VERIFIER
    elif reward_value is None:
        status = CertificationStatus.INFRA_VERIFIER
    elif errors:
        status = CertificationStatus.INFRA_CAPTURE
    elif reward_value >= 1.0:
        status = CertificationStatus.TASK_PASS
    else:
        status = CertificationStatus.TASK_FAIL

    certified = status.is_task_outcome and not errors and all(checks.values())
    task_passed = status == CertificationStatus.TASK_PASS if status.is_task_outcome else None
    certification = CertificationResult(
        status=status,
        certified=certified,
        task_passed=task_passed,
        generated_at=_utc_now(),
        checks=checks,
        errors=errors,
        warnings=warnings,
        evidence_hashes=hashes,
        secret_findings=findings,
    )
    if write:
        target = root / "certification.json"
        temporary = root / ".certification.json.tmp"
        temporary.write_text(
            json.dumps(certification.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    return certification


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 Harbor AGS Trial 证据")
    parser.add_argument("trial_dir", type=Path)
    parser.add_argument("--secret", action="append", default=[])
    parser.add_argument("--no-write", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = validate_harbor_trial(args.trial_dir, secrets=args.secret, write=not args.no_write)
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if result.certified else 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())


__all__ = [
    "CertificationResult",
    "CertificationStatus",
    "SecretFinding",
    "TrialValidationError",
    "scan_secrets",
    "validate_atif_v17",
    "validate_harbor_trial",
]
