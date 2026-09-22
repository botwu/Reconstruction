#!/usr/bin/env python3
"""Export the frozen R01-50 screening batch into workspace/returndata."""

from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path("/mnt/afs_toolcall/wujian1/Projects/workspace/TraceRconstruction")
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.p1_fixtures import wrap_v9_code_file_record
from traceforge.reconstruction.session_source import (
    _message_text,
    _tool_timeline,
    build_reconstruction_source,
)
from traceforge.screening.observable import build_spans
from traceforge.trajectory.privacy import omit_private_reasoning

SCREENING_DIR = Path(
    "/tmp/traceforge-screening-r01-v9-deepseek/"
    "80d2d1f6b99ad9fd6b23f846743d0632fbd621bcda01c5773ab2e19ad1c92ae6"
)
SOURCE_JSONL = ROOT / "return_data/four_batch/by-rubric/R01.jsonl"
RUBRIC_DOC = ROOT / "docs/reconstruction-screening-rubric.md"
OUT = Path("/mnt/afs_toolcall/wujian1/Projects/workspace/returndata")


def _user_texts(messages: list) -> list[str]:
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_text(message).strip()
        if not text or text.startswith("<environment_context>"):
            continue
        texts.append(text)
    return texts


def _env(messages: list) -> dict[str, str]:
    for message in messages:
        if not (isinstance(message, dict) and message.get("role") == "user"):
            continue
        text = _message_text(message)
        if "<cwd>" not in text:
            continue
        cwd = ""
        shell = ""
        if "<cwd>" in text and "</cwd>" in text:
            cwd = text.split("<cwd>", 1)[1].split("</cwd>", 1)[0].strip()
        if "<shell>" in text and "</shell>" in text:
            shell = text.split("<shell>", 1)[1].split("</shell>", 1)[0].strip()
        return {"cwd": cwd, "shell": shell}
    return {"cwd": "", "shell": ""}


def _load_source_lines() -> dict[int, str]:
    lines: dict[int, str] = {}
    with SOURCE_JSONL.open(encoding="utf-8") as handle:
        for index, raw in enumerate(handle, start=1):
            if index > 50:
                break
            if raw.strip():
                lines[index] = raw
    return lines


def _compact_record(record: dict, parsed: dict) -> dict:
    triage = record.get("triage") or {}
    rubric = triage.get("rubric") or {}
    return {
        "source_ref": record.get("source_ref"),
        "line_number": record.get("line_number"),
        "decision": record.get("decision"),
        "route": record.get("route"),
        "capture_id": record.get("capture_id"),
        "thread_id": record.get("thread_id"),
        "r1_task_identifiability": rubric.get("task_identifiability"),
        "r2_failure_evidence": rubric.get("failure_evidence"),
        "outcome": triage.get("outcome"),
        "domain_route": triage.get("domain_route"),
        "needs_reconstruction": triage.get("needs_reconstruction"),
        "reason": triage.get("reason"),
        "selected_span_ids": triage.get("selected_span_ids") or [],
        "user_preview": (parsed.get("user_texts") or [""])[0][:200],
        "task_count": len(triage.get("tasks") or []),
        "cwd": parsed.get("environment", {}).get("cwd"),
        "shell": parsed.get("environment", {}).get("shell"),
        "tool_calls": parsed.get("tool_call_count"),
        "message_count": parsed.get("message_count"),
    }


def main() -> None:
    if not SCREENING_DIR.is_dir():
        raise SystemExit(f"screening batch missing: {SCREENING_DIR}")
    if not SOURCE_JSONL.is_file():
        raise SystemExit(f"source jsonl missing: {SOURCE_JSONL}")

    out = OUT / "r01-50-v9-deepseek"
    sessions_dir = out / "sessions"
    if out.exists():
        shutil.rmtree(out)
    sessions_dir.mkdir(parents=True)

    shutil.copy2(SCREENING_DIR / "selection_manifest.json", out / "selection_manifest.json")
    shutil.copy2(SCREENING_DIR / "private/records.jsonl", out / "records.jsonl")
    shutil.copy2(RUBRIC_DOC, OUT / "reconstruction-screening-rubric.md")

    source_lines = _load_source_lines()
    records = [
        json.loads(line)
        for line in (SCREENING_DIR / "private/records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    index: list[dict] = []
    eligible_parsed: list[dict] = []
    parse_errors: list[dict] = []

    for record in records:
        line_number = int(record["line_number"])
        raw_line = source_lines.get(line_number)
        if raw_line is None:
            parse_errors.append({"line_number": line_number, "error": "source line missing"})
            continue
        payload = json.loads(raw_line)
        messages = [item if isinstance(item, dict) else {} for item in payload.get("messages") or []]
        spans, _ = build_spans(messages)
        timeline = _tool_timeline(messages, spans)
        domain = payload.get("domain_meta") if isinstance(payload.get("domain_meta"), dict) else {}
        parsed = {
            "source_ref": record.get("source_ref"),
            "line_number": line_number,
            "decision": record.get("decision"),
            "route": record.get("route"),
            "screening": {
                "reason": (record.get("triage") or {}).get("reason"),
                "outcome": (record.get("triage") or {}).get("outcome"),
                "domain_route": (record.get("triage") or {}).get("domain_route"),
                "rubric": (record.get("triage") or {}).get("rubric"),
                "selected_span_ids": (record.get("triage") or {}).get("selected_span_ids") or [],
                "tasks": (record.get("triage") or {}).get("tasks") or [],
            },
            "source_task": ((domain.get("task") or {}) if isinstance(domain.get("task"), dict) else {}).get("normalized"),
            "environment": _env(messages),
            "user_texts": _user_texts(messages),
            "message_count": len(messages),
            "span_count": len(spans),
            "tool_call_count": len(timeline),
            "tool_names": Counter(str(item.get("name") or "") for item in timeline).most_common(),
            "spans": [
                {
                    "span_id": span.span_id,
                    "message_start": span.message_start,
                    "message_end": span.message_end,
                    "user_message_indices": list(span.user_message_indices),
                }
                for span in spans
            ],
        }
        dest = sessions_dir / f"L{line_number:02d}_{record['decision'].lower()}"
        dest.mkdir()
        (dest / "screening.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (dest / "parsed.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (dest / "raw_session.json").write_text(
            json.dumps(omit_private_reasoning(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if record.get("decision") == "ELIGIBLE":
            try:
                wrapped = wrap_v9_code_file_record(record, raw_line)
                source = build_reconstruction_source(raw_line=raw_line, record=wrapped)
                source.pop("raw_session", None)
                (dest / "reconstruction_source.json").write_text(
                    json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                parsed["reconstruction_source_ok"] = True
                parsed["selected_span_has_file_ops"] = source.get("selected_span_has_file_ops")
            except Exception as exc:  # noqa: BLE001
                parsed["reconstruction_source_ok"] = False
                parsed["reconstruction_source_error"] = f"{type(exc).__name__}: {exc}"
                (dest / "parsed.json").write_text(
                    json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            eligible_parsed.append(parsed)
        index.append(_compact_record(record, parsed))

    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "eligible_parsed.json").write_text(
        json.dumps(eligible_parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    counts = Counter(item["decision"] for item in index)
    routes = Counter(item["route"] for item in index if item["decision"] == "ELIGIBLE")
    readme = f"""# 重建筛选结果导出

这份目录是从已冻结的 R01 前 50 条抽样筛选批次解析出来的 session，不是全量 four_batch。

## Rubric 在哪

- 说明：`TraceRconstruction/docs/reconstruction-screening-rubric.md`
- 本目录副本：`reconstruction-screening-rubric.md`
- 实现：`TraceRconstruction/src/traceforge/screening/rubric.py`、`model_triage.py`、`rules.py`
- 冻结合同：`reconstruction-screening-v9` / `reconstruction-screening-triage-v9`（文档后改名为 v10，尺的 R1/R2 没改）

硬门槛：有效任务（R1 ≥ 2）且没做好（FAILURE/INCOMPLETE + R2 ≥ 2）。任一 task 过线整条 ELIGIBLE。

## 抽样筛选结果在哪

正式校准批次原来只在临时目录，现已拷到这里：

- 原始产物：`/tmp/traceforge-screening-r01-v9-deepseek/80d2d1f6b99ad9fd6b23f846743d0632fbd621bcda01c5773ab2e19ad1c92ae6`
- 本目录：`r01-50-v9-deepseek/`

输入是本地截断版 `TraceRconstruction/return_data/four_batch/by-rubric/R01.jsonl` 的前 50 行。
模型：`vol/deepseek-v4-flash-0731`，concurrency=8。

| 决策 | 条数 |
| --- | ---: |
| ELIGIBLE | {counts.get("ELIGIBLE", 0)} |
| REVIEW | {counts.get("REVIEW", 0)} |
| REJECT | {counts.get("REJECT", 0)} |
| DEFER | {counts.get("DEFER", 0)} |

ELIGIBLE 路由：{dict(routes)}

后面还有一次 v10 重跑（`/tmp/traceforge-screening-r01-v10-deepseek`），当时模型 HTTP 503，50 条几乎全是 REVIEW，**不能当筛选结果用**。

## 本目录文件

- `selection_manifest.json`：批次清单和 `eligible_source_refs`
- `records.jsonl`：50 条筛选记录（分数、tasks、理由）
- `index.json`：50 条摘要
- `eligible_parsed.json`：35 条 ELIGIBLE 的解析结果
- `sessions/Lxx_<decision>/`
  - `screening.json`：筛选记录
  - `parsed.json`：用户话、环境、工具、span
  - `raw_session.json`：原始 session（已去掉 thinking/reasoning）
  - `reconstruction_source.json`：仅 ELIGIBLE，且 v9→v10 包装成功时才有
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps({"out": str(out), "counts": dict(counts), "eligible_routes": dict(routes), "errors": parse_errors}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
