#!/usr/bin/env python3
"""Stream-sample four_batch GPT-5.6 Sol trajectories and measure rebuildability."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path("/mnt/afs_toolcall/wujian1/Projects/workspace/TraceRconstruction")
sys.path.insert(0, str(ROOT / "src"))

from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.session_source import _tool_timeline
from traceforge.reconstruction.session_spans import build_spans

CANDIDATE_ROOTS = [
    Path("/mnt/afs_toolcall/juxiaolong1/Projects/DataFilter_v2/gpt56sol/domain/by-rubric"),
    ROOT / "return_data/four_batch/by-rubric",
]

RUBRICS = [
    ("R01", 180),
    ("R02", 80),
    ("R03", 80),
    ("R04", 280),
    ("R05", 200),
    ("R06", 120),
    ("R07", 80),
    ("R08", 120),
    ("R09", 180),
    ("R10", 60),
    ("R11", 80),
    ("UNKNOWN", 80),
]

WIN_RE = re.compile(r"(?i)(powershell|Get-Content|Set-Content|C:\\\\|C:/Users|E:\\\\|\.bat\b|\.ps1\b|cmd\.exe)")
POSIX_RE = re.compile(r"(?i)(\b(?:cat|ls|grep|rg)\b|/home/|/app/|\.sh\b)")
WEB_TOOLS = {"web_search", "web_search_preview", "url_fetch", "browser", "open_url"}
FILE_NATIVE = {"read", "write", "edit", "read_file", "write_file", "glob", "grep"}
SHELL_TOOLS = {"exec", "bash", "shell", "terminal", "command", "powershell"}


def locate(name: str) -> Path | None:
    for root in CANDIDATE_ROOTS:
        path = root / name
        if path.is_file():
            return path
    return None


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("value")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(content, dict):
        return str(content.get("value") or content.get("text") or "")
    return ""


ENV_RE = re.compile(
    r"<cwd>(?P<cwd>.*?)</cwd>.*?<shell>(?P<shell>.*?)</shell>",
    re.S | re.I,
)


def first_user_text(messages: list) -> str:
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            text = message_text(message).strip()
            if text and not text.startswith("<environment_context>") and "AGENTS.md instructions" not in text[:80]:
                return text
    return ""


def env_context(messages: list) -> dict:
    for message in messages:
        if not (isinstance(message, dict) and message.get("role") == "user"):
            continue
        text = message_text(message)
        if "<environment_context>" not in text:
            continue
        match = ENV_RE.search(text)
        if match:
            return {"cwd": match.group("cwd").strip(), "shell": match.group("shell").strip().lower()}
    return {"cwd": "", "shell": ""}


def user_count(messages: list) -> int:
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")


def tool_defs(record: dict) -> list[str]:
    tools = record.get("tools")
    names = []
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(name, str):
                names.append(name)
    return names


def iter_sample(path: Path, want: int, total_hint: int | None) -> list[tuple[int, dict]]:
    stride = 1
    if total_hint and total_hint > want:
        stride = max(1, total_hint // want)
    out: list[tuple[int, dict]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if (line_no - 1) % stride != 0:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append((line_no, rec))
            if len(out) >= want:
                break
    return out


def analyze_record(rec: dict) -> dict:
    messages = rec.get("messages") if isinstance(rec.get("messages"), list) else []
    domain = rec.get("domain_meta") if isinstance(rec.get("domain_meta"), dict) else {}
    meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
    spans, _ = build_spans([m if isinstance(m, dict) else {} for m in messages])
    timeline = _tool_timeline([m if isinstance(m, dict) else {} for m in messages], spans)
    names = [str(item.get("name") or "").lower() for item in timeline]
    name_counts = Counter(names)
    blob = json.dumps(
        {
            "tools": rec.get("tools"),
            "timeline_args": [item.get("arguments") for item in timeline[:40]],
            "user": first_user_text(messages)[:800],
        },
        ensure_ascii=False,
    )
    windows = bool(WIN_RE.search(blob))
    posix = bool(POSIX_RE.search(blob))
    replay_files = 0
    replay_complete = 0
    replay_partial = 0
    replay_lines = 0
    withheld = 0
    replay_error = None
    try:
        replay = replay_from_timeline(timeline)
        replay_files = len(replay.files)
        withheld = len(replay.withheld_changes)
        for item in replay.files:
            text = item.content or ""
            replay_lines += text.count("\n") + (1 if text else 0)
            if item.completeness == "COMPLETE":
                replay_complete += 1
            else:
                replay_partial += 1
    except Exception as exc:  # noqa: BLE001
        replay_error = type(exc).__name__

    user = first_user_text(messages)
    env = env_context(messages)
    if env.get("shell") == "powershell" or re.search(r"(?i)^[A-Za-z]:\\", env.get("cwd") or ""):
        windows = True
    return {
        "message_count": len(messages),
        "user_count": user_count(messages),
        "span_count": len(spans),
        "tool_calls": len(timeline),
        "pending_tools": sum(1 for item in timeline if item.get("pending")),
        "tool_names": name_counts,
        "has_exec": any(n in SHELL_TOOLS for n in names),
        "has_native_file": any(n in FILE_NATIVE for n in names),
        "has_web": any(n in WEB_TOOLS or "search" in n or "fetch" in n or "browser" in n for n in names),
        "has_wait": "wait" in name_counts,
        "declared_tools": tool_defs(rec),
        "windows": windows,
        "posix": posix,
        "replay_files": replay_files,
        "replay_complete": replay_complete,
        "replay_partial": replay_partial,
        "replay_lines": replay_lines,
        "withheld": withheld,
        "replay_error": replay_error,
        "paper_seed_like": replay_files >= 5 and replay_lines >= 100,
        "has_any_file": replay_files > 0,
        "leaf": (meta.get("leaf_response_status") if isinstance(meta, dict) else None),
        "model": str(meta.get("model") or ""),
        "task_desc": str(((domain.get("task") or {}) if isinstance(domain.get("task"), dict) else {}).get("normalized") or "")[:200],
        "user_preview": user.replace("\n", " ")[:160],
        "risk": ((domain.get("operation_risk") or {}) if isinstance(domain.get("operation_risk"), dict) else {}).get("code"),
        "domain_code": ((domain.get("domain") or {}) if isinstance(domain.get("domain"), dict) else {}).get("code"),
        "cwd": env.get("cwd", "")[:120],
        "shell": env.get("shell", ""),
    }


def summarize(rows: list[dict]) -> dict:
    n = len(rows) or 1
    tools = Counter()
    declared = Counter()
    for row in rows:
        tools.update(row["tool_names"])
        declared.update(row["declared_tools"])
    return {
        "n": len(rows),
        "mean_messages": round(sum(r["message_count"] for r in rows) / n, 1),
        "mean_users": round(sum(r["user_count"] for r in rows) / n, 1),
        "mean_tools": round(sum(r["tool_calls"] for r in rows) / n, 1),
        "pct_exec": round(100 * sum(r["has_exec"] for r in rows) / n, 1),
        "pct_native_file": round(100 * sum(r["has_native_file"] for r in rows) / n, 1),
        "pct_web": round(100 * sum(r["has_web"] for r in rows) / n, 1),
        "pct_wait": round(100 * sum(r["has_wait"] for r in rows) / n, 1),
        "pct_windows": round(100 * sum(r["windows"] for r in rows) / n, 1),
        "pct_posix": round(100 * sum(r["posix"] for r in rows) / n, 1),
        "pct_any_file": round(100 * sum(r["has_any_file"] for r in rows) / n, 1),
        "pct_paper_seed": round(100 * sum(r["paper_seed_like"] for r in rows) / n, 1),
        "mean_replay_files": round(sum(r["replay_files"] for r in rows) / n, 2),
        "mean_replay_complete": round(sum(r["replay_complete"] for r in rows) / n, 2),
        "mean_replay_partial": round(sum(r["replay_partial"] for r in rows) / n, 2),
        "mean_replay_lines": round(sum(r["replay_lines"] for r in rows) / n, 1),
        "mean_withheld": round(sum(r["withheld"] for r in rows) / n, 2),
        "pct_multiuser": round(100 * sum(r["user_count"] >= 2 for r in rows) / n, 1),
        "top_tools": tools.most_common(8),
        "declared_tools": declared.most_common(8),
        "replay_errors": sum(1 for r in rows if r["replay_error"]),
        "shells": Counter(r["shell"] or "unknown" for r in rows).most_common(6),
        "risks": Counter(r["risk"] or "unknown" for r in rows).most_common(6),
        "domains": Counter(r["domain_code"] or "unknown" for r in rows).most_common(6),
        "leaf": Counter(str(r["leaf"] or "unknown") for r in rows).most_common(5),
        "examples": [
            {
                "files": r["replay_files"],
                "complete": r["replay_complete"],
                "partial": r["replay_partial"],
                "lines": r["replay_lines"],
                "users": r["user_count"],
                "tools": r["tool_calls"],
                "win": r["windows"],
                "shell": r["shell"],
                "cwd": r["cwd"],
                "user": r["user_preview"],
                "task": r["task_desc"],
            }
            for r in sorted(rows, key=lambda x: (-x["replay_files"], -x["replay_lines"]))[:5]
        ],
    }


def main() -> None:
    totals = {
        "R01": 1683, "R02": 273, "R03": 398, "R04": 6535, "R05": 1694,
        "R06": 554, "R07": 336, "R08": 1587, "R09": 5859, "R10": 166,
        "R11": 297, "UNKNOWN": 1011,
    }
    report: dict = {"files": {}, "rubrics": {}}
    all_rows: list[dict] = []
    for code, want in RUBRICS:
        path = locate(f"{code}.jsonl")
        report["files"][code] = str(path) if path else None
        if path is None:
            continue
        samples = iter_sample(path, want, totals.get(code))
        rows = [analyze_record(rec) for _, rec in samples]
        all_rows.extend(rows)
        report["rubrics"][code] = summarize(rows)
        print(f"=== {code} n={len(rows)} path={path} ===", flush=True)
        print(json.dumps(report["rubrics"][code], ensure_ascii=False, indent=2), flush=True)
    if all_rows:
        report["overall"] = summarize(all_rows)
        print("=== OVERALL ===", flush=True)
        print(json.dumps(report["overall"], ensure_ascii=False, indent=2), flush=True)
    out = ROOT / "artifacts" / "four_batch_traj_profile.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
