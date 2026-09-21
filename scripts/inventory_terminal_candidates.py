#!/usr/bin/env python3
"""Deterministic, read-only inventory for selected terminal candidates.

This stage only reads the selector JSONL and its manifest.  It does not replay
commands, call a model, or mutate source records.  Use a temporary or ignored
output path when running it against the full upstream inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

TERMINAL_NAMES = {
    "apply_patch", "bash", "cmd", "command", "edit", "exec_command", "file_edit",
    "glob", "grep", "powershell", "read", "read_file", "rg", "shell",
    "shell_command", "terminal", "write", "write_file",
}
RETRIEVAL_NAMES = {
    "browser", "chat_history_get", "open_url", "url_fetch", "web_fetch",
    "web_search", "web_search_preview",
}
_ACTION_RE = re.compile(
    r"(?i)(修复|修改|新增|实现|生成|创建|编写|补充|增加|调整|重构|更新|改成|完善|"
    r"fix|implement|add|create|write|modify|change|refactor|generate|patch|test)"
)
_REVIEW_ONLY_RE = re.compile(
    r"(?i)(只做(?:代码)?审查|只读(?:代码)?评审|只审查|不修改|"
    r"review[- ]only|do not modify|without modifying)"
)
_IMPLEMENT_RE = re.compile(
    r"(?i)(实现|修复|修改|新增|生成|创建|编写|补充|增加|调整|重构|更新|"
    r"fix|implement|add|create|write|modify|change|refactor|generate|patch|test)"
)
_SECRET_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|token|password|secret|credential|authorization)"
    r"(\s*[:=])\s*(?!Bearer\b)[^\s,;]+"
)
_AUTH_BEARER_RE = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*Bearer\s+[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SK_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}")
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8}|"
    r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})(?![A-Za-z0-9])"
)
_FRAMEWORK_HEADS = (
    "<environment_context>", "# AGENTS.md", "<INSTRUCTIONS>",
    "Sender (untrusted metadata)", "<system-reminder>", "<team_mode_status>",
    "<system-conventions>", "<permissions instructions>", "<codex_delegation>",
    "You are Codex", "You are OpenCode", "# Personality", "## Skills",
    "You are resuming a prior conversation", "<EXTREMELY_IMPORTANT>",
    "superpowers:", "HISTORY below",
)


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or item.get("value") or "")
            if isinstance(item, dict) else str(item)
            for item in content
        )
    if isinstance(content, dict):
        return str(content.get("text") or content.get("value") or "")
    return ""


def _redact(value: str) -> str:
    def replace_secret(match: re.Match[str]) -> str:
        raw = match.group(0)
        delimiter = ":" if ":" in raw else "="
        prefix = raw[: raw.find(delimiter)].rstrip()
        return f"{prefix}{delimiter} <redacted>"

    text = _AUTH_BEARER_RE.sub("Authorization: Bearer <redacted>", value)
    text = _SECRET_RE.sub(replace_secret, text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    return _SK_RE.sub("<redacted>", text)


def _user_texts(record: dict[str, Any]) -> list[str]:
    output: list[str] = []
    messages = record.get("messages")
    if not isinstance(messages, list):
        return output
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_text(message).strip()
        if not text or any(text.startswith(head) for head in _FRAMEWORK_HEADS):
            continue
        if text.startswith("# Files mentioned by the user") or "AUTOCLAW_OUTPUT_PROTOCOL" in text[:800]:
            continue
        output.append(_redact(text))
    return output


def _function_call_rows(record: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    messages = record.get("messages")
    if not isinstance(messages, list):
        return rows
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function", call)
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "").strip().lower()
            leaf = name.rsplit(".", 1)[-1]
            arguments: Any = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            rows.append((leaf, arguments if isinstance(arguments, dict) else {}))
    return rows


def _tool_sets(rows: list[tuple[str, dict[str, Any]]]) -> tuple[set[str], set[str]]:
    terminal: set[str] = set()
    retrieval: set[str] = set()
    for name, arguments in rows:
        if name in TERMINAL_NAMES:
            terminal.add(name)
        if name in RETRIEVAL_NAMES:
            retrieval.add(name)
        if name != "exec":
            continue
        command = arguments.get("cmd") or arguments.get("command")
        if isinstance(command, str) and command.strip():
            terminal.add("exec_command")
        source = arguments.get("input") or arguments.get("code")
        if not isinstance(source, str):
            continue
        for nested in re.findall(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", source):
            nested = nested.lower()
            if nested in TERMINAL_NAMES:
                terminal.add(nested)
            if nested in RETRIEVAL_NAMES:
                retrieval.add(nested)
    return terminal, retrieval


def _path_tokens(text: str) -> list[str]:
    found: list[str] = []
    for match in _PATH_RE.finditer(text):
        path = match.group(1).replace("\\", "/")
        if path not in found:
            found.append(path)
    return found


def _preflight(
    *,
    user_blob: str,
    user_chars: int,
    message_count: int,
    tool_count: int,
    terminal_count: int,
    read_count: int,
    retrieval_count: int,
    risk: str | None,
    min_user_chars: int,
    max_messages: int,
    max_tools: int,
) -> tuple[dict[str, Any], float]:
    reasons: list[str] = []
    action_text = re.sub(
        r"(?i)(?:不|不要|无需)修改|do not modify|without modifying",
        " ",
        user_blob,
    )
    actionable = bool(_ACTION_RE.search(action_text))
    review_only = bool(_REVIEW_ONLY_RE.search(user_blob))
    if review_only:
        actionable = False
    if not user_blob.strip() or user_chars < min_user_chars:
        reasons.append("USER_TEXT_TOO_SHORT")
    if review_only:
        reasons.append("REVIEW_ONLY_REQUEST")
    if not actionable:
        reasons.append("NO_EXPLICIT_FILE_ACTION")
    if terminal_count == 0:
        reasons.append("NO_TERMINAL_CALL")
    if read_count == 0:
        reasons.append("NO_READ_EVIDENCE")
    if message_count > max_messages:
        reasons.append("TOO_MANY_MESSAGES")
    if tool_count > max_tools:
        reasons.append("TOO_MANY_TOOL_CALLS")
    if retrieval_count:
        reasons.append("MIXED_RETRIEVAL")
    if risk is None:
        reasons.append("RISK_UNCLASSIFIED")
    elif risk not in {"local_reversible_write", "read_only"}:
        reasons.append("NON_LOCAL_RISK")
    score = 100.0
    score += min(read_count, 12) * 3.0
    score -= min(message_count, 240) * 0.08
    score -= min(tool_count, 120) * 0.15
    score -= retrieval_count * 5.0
    score -= len(reasons) * 20.0
    eligible = not reasons
    return {
        "eligible": eligible,
        "reason_codes": reasons,
        "actionable": actionable,
        "read_evidence": read_count > 0,
        "local_risk": risk in {"local_reversible_write", "read_only"},
    }, round(score, 3)


def inventory_record(
    record: dict[str, Any],
    *,
    line_number: int,
    raw_line: bytes,
    provenance: dict[str, Any] | None,
    min_user_chars: int,
    max_messages: int,
    max_tools: int,
) -> dict[str, Any]:
    users = _user_texts(record)
    user_blob = "\n".join(users)
    rows = _function_call_rows(record)
    terminal, retrieval = _tool_sets(rows)
    def is_read_call(name: str, arguments: dict[str, Any]) -> bool:
        if name in {"read", "read_file", "cat", "head", "tail", "type", "rg", "grep", "sed"}:
            return True
        command = arguments.get("cmd") or arguments.get("command")
        return isinstance(command, str) and bool(
            re.search(
                r"(?im)(?:^|[;&|])\s*(?:cat|head|tail|type|rg|grep|sed|git\s+"
                r"(?:status|diff|show|log))\b",
                command,
            )
        )

    read_count = sum(is_read_call(name, arguments) for name, arguments in rows)
    path_text = "\n".join(
        [user_blob]
        + [json.dumps(arguments, ensure_ascii=False) for _, arguments in rows]
    )
    paths = _path_tokens(_redact(path_text))
    risk = provenance.get("risk") if provenance else None
    rubric = provenance.get("rubric") if provenance else None
    preflight, score = _preflight(
        user_blob=user_blob,
        user_chars=len(user_blob),
        message_count=len(record.get("messages") or []) if isinstance(record.get("messages"), list) else 0,
        tool_count=len(rows),
        terminal_count=len(terminal),
        read_count=read_count,
        retrieval_count=len(retrieval),
        risk=str(risk) if risk else None,
        min_user_chars=min_user_chars,
        max_messages=max_messages,
        max_tools=max_tools,
    )
    row = {
        "line_number": line_number,
        "raw_line_sha256": hashlib.sha256(raw_line).hexdigest(),
        "source": {
            "source_file": provenance.get("source_file") if provenance else None,
            "source_line": provenance.get("source_line") if provenance else None,
            "rubric": rubric,
            "risk": risk,
            "terminal_tools": sorted(terminal),
            "retrieval_tools": sorted(retrieval),
            "manifest_hash_match": (
                provenance.get("raw_line_sha256") == hashlib.sha256(raw_line).hexdigest()
                if provenance and provenance.get("raw_line_sha256") else None
            ),
        },
        "size": {
            "raw_bytes": len(raw_line),
            "messages": len(record.get("messages") or []) if isinstance(record.get("messages"), list) else 0,
            "user_messages": len(users),
            "user_chars": len(user_blob),
            "tool_calls": len(rows),
            "terminal_calls": len(terminal),
            "read_calls": read_count,
            "retrieval_calls": len(retrieval),
        },
        "user_summary": _redact(user_blob[:320]),
        "path_tokens": paths[:40],
        "preflight": preflight,
        "score": score,
    }
    return row


def _load_manifest(path: Path | None) -> dict[int, dict[str, Any]]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    rows = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    output: dict[int, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("output_index"), int):
            output[row["output_index"]] = row
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--min-user-chars", type=int, default=20)
    parser.add_argument("--max-messages", type=int, default=240)
    parser.add_argument("--max-tools", type=int, default=120)
    args = parser.parse_args()
    if args.limit < 1 or args.min_user_chars < 0 or args.max_messages < 1 or args.max_tools < 1:
        parser.error("limit and size limits must be positive")
    if not args.input.is_file():
        parser.error(f"input is not a file: {args.input}")
    for destination in (args.output, args.output.with_name(args.output.name + ".manifest.json")):
        if destination.exists() or destination.is_symlink():
            parser.error(f"refuse to overwrite: {destination}")
        if destination.resolve() == args.input.resolve():
            parser.error("output must differ from input")
    provenance = _load_manifest(args.manifest)
    candidates: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    output_index = 0
    with args.input.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if len(candidates) >= args.limit:
                counts["limit_reached"] += 1
                break
            counts["lines_scanned"] += 1
            if not raw_line.strip():
                counts["blank"] += 1
                continue
            # selection_manifest.output_index counts physical non-blank rows
            # emitted by the selector. Keep it independent of valid candidates:
            # one malformed row must not shift every later provenance record.
            output_index += 1
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                counts["invalid_json"] += 1
                continue
            if not isinstance(record, dict):
                counts["invalid_top_level"] += 1
                continue
            row = inventory_record(
                record,
                line_number=line_number,
                raw_line=raw_line,
                provenance=provenance.get(line_number),
                min_user_chars=args.min_user_chars,
                max_messages=args.max_messages,
                max_tools=args.max_tools,
            )
            candidates.append(row)
            counts["records"] += 1
            counts["eligible"] += int(row["preflight"]["eligible"])
    candidates.sort(key=lambda row: (-float(row["score"]), row["line_number"]))
    payload = {
        "schema": "traceforge.terminal-candidate-inventory.v1",
        "status": "PREFLIGHT_ONLY",
        "input": str(args.input),
        "manifest": str(args.manifest) if args.manifest else None,
        "policy": {
            "read_only_source": True,
            "model_calls": False,
            "replay": "NOT_RUN",
            "key_output": False,
            "limit": args.limit,
            "min_user_chars": args.min_user_chars,
            "max_messages": args.max_messages,
            "max_tools": args.max_tools,
        },
        "counts": dict(counts),
        "candidates": candidates,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "records": counts.get("records", 0),
        "eligible": counts.get("eligible", 0),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
