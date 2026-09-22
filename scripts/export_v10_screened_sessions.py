#!/usr/bin/env python3
"""Export the v10 R01-50 rerun into workspace/returndata."""

from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path("/mnt/afs_toolcall/wujian1/Projects/workspace/TraceRconstruction")
sys.path.insert(0, str(ROOT / "src"))

from traceforge.reconstruction.session_source import (
    _message_text,
    _tool_timeline,
    build_reconstruction_source,
)
from traceforge.screening.observable import build_spans
from traceforge.trajectory.privacy import omit_private_reasoning

SCREENING_DIR = Path(
    "/tmp/traceforge-screening-r01-v10-rerun/"
    "32174df8f1cbdd4fb2b8a10bbefc6b21abce54ca2d43977023e0519dd3f8cadf"
)
SOURCE_JSONL = ROOT / "return_data/four_batch/by-rubric/R01.jsonl"
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


def main() -> None:
    out = OUT / "r01-50-v10-deepseek"
    sessions_dir = out / "sessions"
    if out.exists():
        shutil.rmtree(out)
    sessions_dir.mkdir(parents=True)
    shutil.copy2(SCREENING_DIR / "selection_manifest.json", out / "selection_manifest.json")
    shutil.copy2(SCREENING_DIR / "private/records.jsonl", out / "records.jsonl")

    source_lines: dict[int, str] = {}
    with SOURCE_JSONL.open(encoding="utf-8") as handle:
        for index, raw in enumerate(handle, start=1):
            if index > 50:
                break
            if raw.strip():
                source_lines[index] = raw

    records = [
        json.loads(line)
        for line in (SCREENING_DIR / "private/records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    index: list[dict] = []
    eligible_parsed: list[dict] = []
    for record in records:
        line_number = int(record["line_number"])
        raw_line = source_lines[line_number]
        payload = json.loads(raw_line)
        messages = [item if isinstance(item, dict) else {} for item in payload.get("messages") or []]
        spans, _ = build_spans(messages)
        timeline = _tool_timeline(messages, spans)
        triage = record.get("triage") or {}
        parsed = {
            "source_ref": record.get("source_ref"),
            "line_number": line_number,
            "decision": record.get("decision"),
            "route": record.get("route"),
            "label_status": triage.get("label_status"),
            "blocking_reason_codes": record.get("blocking_reason_codes") or [],
            "screening": {
                "reason": triage.get("reason"),
                "outcome": triage.get("outcome"),
                "domain_route": triage.get("domain_route"),
                "rubric": triage.get("rubric"),
                "selected_span_ids": triage.get("selected_span_ids") or [],
                "selected_task_ids": triage.get("selected_task_ids") or [],
                "session_tags": triage.get("session_tags") or [],
                "tasks": triage.get("tasks") or [],
                "relations": triage.get("relations") or [],
                "errors": triage.get("errors") or [],
            },
            "environment": _env(messages),
            "user_texts": _user_texts(messages),
            "message_count": len(messages),
            "span_count": len(spans),
            "tool_call_count": len(timeline),
            "tool_names": Counter(str(item.get("name") or "") for item in timeline).most_common(),
        }
        dest = sessions_dir / f"L{line_number:02d}_{record['decision'].lower()}"
        dest.mkdir()
        (dest / "screening.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        (dest / "parsed.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        (dest / "raw_session.json").write_text(
            json.dumps(omit_private_reasoning(payload), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if record.get("decision") == "ELIGIBLE":
            try:
                source = build_reconstruction_source(raw_line=raw_line, record=record)
                source.pop("raw_session", None)
                (dest / "reconstruction_source.json").write_text(
                    json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                parsed["reconstruction_source_ok"] = True
                parsed["selected_span_has_file_ops"] = source.get("selected_span_has_file_ops")
            except Exception as exc:  # noqa: BLE001
                parsed["reconstruction_source_ok"] = False
                parsed["reconstruction_source_error"] = f"{type(exc).__name__}: {exc}"
                (dest / "parsed.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            eligible_parsed.append(parsed)
        index.append(
            {
                "source_ref": record.get("source_ref"),
                "line_number": line_number,
                "decision": record.get("decision"),
                "route": record.get("route"),
                "label_status": triage.get("label_status"),
                "r1": (triage.get("rubric") or {}).get("task_identifiability"),
                "r2": (triage.get("rubric") or {}).get("failure_evidence"),
                "outcome": triage.get("outcome"),
                "domain_route": triage.get("domain_route"),
                "reason": triage.get("reason"),
                "user_preview": (parsed.get("user_texts") or [""])[0][:200],
                "cwd": parsed["environment"].get("cwd"),
                "tool_calls": parsed["tool_call_count"],
            }
        )
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "eligible_parsed.json").write_text(
        json.dumps(eligible_parsed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    counts = Counter(item["decision"] for item in index)
    print(json.dumps({"out": str(out), "counts": dict(counts), "eligible": counts.get("ELIGIBLE", 0)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
