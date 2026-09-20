#!/usr/bin/env python3
"""从按 domain 划分的 JSONL 中筛选可重放的 terminal/工作区轨迹。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from traceforge.reconstruction.session_source import _tool_timeline
from traceforge.screening.observable import build_spans

DEFAULT_RUBRICS = ("R04", "R05")
TERMINAL_NAMES = {
    "apply_patch", "bash", "cmd", "command", "edit", "exec", "file_edit",
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
    messages = record.get("messages")
    if not isinstance(messages, list):
        return [], []
    typed = [item if isinstance(item, dict) else {} for item in messages]
    spans, _ = build_spans(typed)
    try:
        timeline = _tool_timeline(typed, spans)
    except Exception:
        return [], []
    names = [str(item.get("name") or "").strip().lower() for item in timeline]
    terminal: list[str] = []
    retrieval: list[str] = []
    for name in names:
        leaf = name.rsplit(".", 1)[-1]
        if leaf in TERMINAL_NAMES or name in TERMINAL_NAMES:
            terminal.append(leaf)
        if leaf in RETRIEVAL_NAMES or name in RETRIEVAL_NAMES:
            retrieval.append(leaf)
        elif any(token in leaf for token in ("search", "fetch", "browser")):
            retrieval.append(leaf)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", default=[], help="JSONL file (repeatable)")
    parser.add_argument("--input-dir", help="directory containing rubric JSONL files")
    parser.add_argument("--output", required=True, help="selected raw JSONL path")
    parser.add_argument(
        "--rubric", default=",".join(DEFAULT_RUBRICS),
        help="comma-separated primary rubric codes (default: R04,R05)",
    )
    parser.add_argument("--limit", type=int, default=0, help="maximum selected records; 0 means all")
    parser.add_argument(
        "--allow-high-risk", action="store_true",
        help="retain terminal records whose operation risk is marked irreversible/destructive",
    )
    args = parser.parse_args()
    paths = _iter_paths(args)
    if not paths:
        parser.error("no input JSONL files")
    allowed = {item.strip() for item in args.rubric.split(",") if item.strip()}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    selected_by_rubric: Counter[str] = Counter()
    selected_tools: Counter[str] = Counter()
    line_index: list[dict[str, Any]] = []
    selected = 0

    with output.open("w", encoding="utf-8") as out:
        for path in paths:
            try:
                handle = path.open("r", encoding="utf-8")
            except OSError:
                counts["unreadable_file"] += 1
                continue
            with handle:
                for line_number, raw in enumerate(handle, 1):
                    raw = raw.rstrip("\n")
                    if not raw.strip():
                        counts["blank"] += 1
                        continue
                    counts["records_seen"] += 1
                    try:
                        record = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
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
                    out.write(raw + "\n")
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
                        }
                    )

    manifest = {
        "schema": "traceforge.terminal-domain-selection.v1",
        "policy": {
            "rubrics": sorted(allowed),
            "terminal_tools": sorted(TERMINAL_NAMES),
            "retrieval_tools": sorted(RETRIEVAL_NAMES),
            "high_risk_excluded_by_default": not args.allow_high_risk,
            "raw_records_preserved": True,
        },
        "sources": [str(path) for path in paths],
        "output": str(output),
        "counts": dict(counts),
        "selected_by_rubric": dict(selected_by_rubric),
        "selected_terminal_tools": dict(selected_tools),
        "records": line_index,
    }
    manifest_path = output.with_name("terminal_selection_manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "output": str(output),
        "manifest": str(manifest_path),
        "selected": selected,
        "counts": dict(counts),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
