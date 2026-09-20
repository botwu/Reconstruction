#!/usr/bin/env python3
"""从按 rubric 分桶的 JSONL 中索引 terminal 候选；不判断 replay 或 eligibility。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_RUBRICS = ("R04", "R05")
TERMINAL_NAMES = {
    "apply_patch", "bash", "cmd", "command", "edit", "exec_command", "file_edit",
    "glob", "grep", "powershell", "read", "read_file", "rg", "shell",
    "shell_command", "terminal", "write", "write_file",
}
RETRIEVAL_NAMES = {
    "browser", "chat_history_get", "open_url", "url_fetch", "web_fetch",
    "web_search", "web_search_preview",
}
HIGH_RISK_MARKERS = (
    "irreversible",
    "credential",
    "secret",
    "destructive",
    "external_side_effect",
)


def _primary_rubric(record: dict[str, Any]) -> str | None:
    meta = record.get("domain_meta")
    if not isinstance(meta, dict):
        return None
    rubric = meta.get("rubric")
    if not isinstance(rubric, dict):
        return None
    primary = rubric.get("primary")
    if not isinstance(primary, dict):
        return None
    code = primary.get("code")
    return str(code) if isinstance(code, str) else None


def _risk_code(record: dict[str, Any]) -> str | None:
    meta = record.get("domain_meta")
    risk = meta.get("operation_risk") if isinstance(meta, dict) else None
    code = risk.get("code") if isinstance(risk, dict) else None
    return str(code) if isinstance(code, str) else None


def _tool_names(record: dict[str, Any]) -> tuple[list[str], list[str]]:
    """只索引 assistant 的实际调用；通用 exec 名称不证明终端活动。"""
    terminal: list[str] = []
    retrieval: list[str] = []
    messages = record.get("messages")
    if not isinstance(messages, list):
        return terminal, retrieval
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
            if leaf in TERMINAL_NAMES:
                terminal.append(leaf)
            if leaf in RETRIEVAL_NAMES:
                retrieval.append(leaf)
            if leaf != "exec":
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = None
            # 只解析 wrapper 内具名 tools 调用，不执行历史代码。
            if isinstance(arguments, dict):
                source = arguments.get("input") or arguments.get("code")
                command = arguments.get("cmd") or arguments.get("command")
                if isinstance(command, str) and command.strip():
                    terminal.append("exec_command")
            else:
                source = arguments
            if not isinstance(source, str):
                continue
            nested = re.findall(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", source)
            for nested_name in nested:
                nested_name = nested_name.lower()
                if nested_name in TERMINAL_NAMES:
                    terminal.append(nested_name)
                if nested_name in RETRIEVAL_NAMES:
                    retrieval.append(nested_name)
    return terminal, retrieval


def _high_risk(code: str | None) -> bool:
    lowered = (code or "").lower()
    return any(marker in lowered for marker in HIGH_RISK_MARKERS)


def _iter_paths(args: argparse.Namespace) -> list[Path]:
    paths = [Path(item) for item in args.input]
    if args.input_dir:
        paths.extend(sorted(Path(args.input_dir).glob("*.jsonl")))
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen and path.is_file():
            seen.add(resolved)
            out.append(path)
    return out


def _source_expectation(path: Path) -> dict[str, Any]:
    """数据索引描述上游全量文件，不能代表当前已复制完整。"""
    index = path.parent / "distribution.json"
    if not index.is_file():
        return {}
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"index_error": "INVALID_DISTRIBUTION_INDEX"}
    rows = data.get("distribution", []) if isinstance(data, dict) else []
    for row in rows:
        if isinstance(row, dict) and row.get("code") == path.stem:
            return {
                "expected_records": row.get("records"),
                "expected_bytes": row.get("bytes"),
                "expected_sha256": row.get("sha256"),
                "upstream_path": row.get("annotation"),
            }
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", default=[], help="JSONL 文件（可重复）")
    parser.add_argument("--input-dir", help="包含 rubric JSONL 文件的目录")
    parser.add_argument("--output", required=True, help="候选原始 JSONL 输出路径")
    parser.add_argument(
        "--rubric", default=",".join(DEFAULT_RUBRICS),
        help="主 rubric 白名单，逗号分隔；默认 R04,R05",
    )
    parser.add_argument("--limit", type=int, default=0, help="候选数量上限；0 为不限制")
    parser.add_argument(
        "--allow-high-risk", action="store_true",
        help="包含被标为不可逆或破坏性操作的轨迹候选",
    )
    args = parser.parse_args()
    paths = _iter_paths(args)
    if not paths:
        parser.error("没有可读的输入 JSONL 文件")
    allowed = {item.strip() for item in args.rubric.split(",") if item.strip()}
    output = Path(args.output)
    manifest_path = output.with_name(output.name + ".manifest.json")
    if args.limit < 0:
        parser.error("limit 不能小于 0")
    if not allowed:
        parser.error("rubric 白名单不能为空")
    for destination in (output, manifest_path):
        if destination.exists() or destination.is_symlink():
            parser.error(f"禁止覆盖现有文件：{destination}")
        if any(destination.resolve() == path.resolve() for path in paths):
            parser.error(f"输出不能覆盖原始输入：{destination}")
    output.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    selected_by_rubric: Counter[str] = Counter()
    selected_tools: Counter[str] = Counter()
    line_index: list[dict[str, Any]] = []
    selected = 0
    source_audits: list[dict[str, Any]] = []

    with output.open("xb") as out:
        for path in paths:
            source_audit = {"path": str(path), "physical_lines": 0, "bytes": 0,
                            **_source_expectation(path)}
            digest = hashlib.sha256()
            source_audits.append(source_audit)
            try:
                handle = path.open("rb")
            except OSError:
                counts["unreadable_file"] += 1
                source_audit["read_status"] = "UNREADABLE"
                continue
            with handle:
                for line_number, raw_bytes in enumerate(handle, 1):
                    digest.update(raw_bytes)
                    source_audit["physical_lines"] += 1
                    source_audit["bytes"] += len(raw_bytes)
                    counts["physical_lines"] += 1
                    try:
                        raw = raw_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        counts["invalid_utf8"] += 1
                        continue
                    if not raw.strip():
                        counts["blank"] += 1
                        continue
                    counts["records_seen"] += 1
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        counts["invalid_json"] += 1
                        continue
                    if not isinstance(record, dict):
                        counts["invalid_top_level"] += 1
                        continue
                    rubric = _primary_rubric(record)
                    if rubric not in allowed:
                        counts["excluded_rubric"] += 1
                        continue
                    terminal, retrieval = _tool_names(record)
                    if not terminal:
                        counts["excluded_without_terminal_tool"] += 1
                        if retrieval:
                            counts["retrieval_only"] += 1
                        continue
                    if _high_risk(_risk_code(record)) and not args.allow_high_risk:
                        counts["excluded_high_risk"] += 1
                        continue
                    if args.limit and selected >= args.limit:
                        counts["limit_reached"] += 1
                        continue
                    out.write(raw_bytes if raw_bytes.endswith(b"\n") else raw_bytes + b"\n")
                    selected += 1
                    counts["selected"] += 1
                    selected_by_rubric[rubric or "UNKNOWN"] += 1
                    selected_tools.update(terminal)
                    line_index.append(
                        {
                            "output_index": selected,
                            "source_file": str(path),
                            "source_line": line_number,
                            "rubric": rubric,
                            "risk": _risk_code(record),
                            "terminal_tools": sorted(set(terminal)),
                            "retrieval_tools": sorted(set(retrieval)),
                            "mixed_terminal_retrieval": bool(retrieval),
                            "raw_line_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                            "replay_check": "NOT_RUN",
                        }
                    )

            source_audit["sha256"] = digest.hexdigest()
            source_audit["read_status"] = "COMPLETE"
            expected_sha256 = source_audit.get("expected_sha256")
            source_audit["matches_index"] = (
                digest.hexdigest() == expected_sha256 if expected_sha256 else None
            )
            expected_records = source_audit.get("expected_records")
            expected_bytes = source_audit.get("expected_bytes")
            source_audit["coverage_complete"] = bool(
                source_audit["matches_index"] is True
                and (expected_records is None or expected_records == source_audit["physical_lines"])
                and (expected_bytes is None or expected_bytes == source_audit["bytes"])
            )

    manifest = {
        "schema": "traceforge.terminal-domain-selection.v2",
        "status": "CANDIDATE_ONLY",
        "policy": {
            "rubrics": sorted(allowed),
            "terminal_tools": sorted(TERMINAL_NAMES),
            "retrieval_tools": sorted(RETRIEVAL_NAMES),
            "high_risk_excluded_by_default": not args.allow_high_risk,
            "raw_records_preserved": True,
            "replay_check": "NOT_RUN",
            "eligibility_decision": "NOT_RUN",
        },
        "sources": source_audits,
        "output": str(output),
        "counts": dict(counts),
        "selected_by_rubric": dict(selected_by_rubric),
        "selected_terminal_tools": dict(selected_tools),
        "records": line_index,
    }
    with manifest_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "output": str(output),
        "manifest": str(manifest_path),
        "selected": selected,
        "counts": dict(counts),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
