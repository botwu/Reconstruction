"""把编译轨迹安全投影为可重建 Task 输入。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content", {}) if isinstance(payload, dict) else {}
    value = content.get("value") if isinstance(content, dict) else None
    return value if isinstance(value, str) else None


def _emit(
    *,
    capture_id: str,
    rows: list[dict[str, Any]],
    output_path: str | Path,
    missing: list[str] | None = None,
    query_ordinal: int | None = None,
    target_event_id: str | None = None,
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    for row in rows:
        event_id = row.get("event_occurrence_id")
        pointer = row.get("source_json_pointer")
        ref_id = hashlib.sha256(f"event:{event_id}:{pointer}".encode()).hexdigest()
        kind = row.get("event_kind")
        payload = row.get("payload", {})
        role = "TASK" if kind == "USER" and (target_event_id is None or event_id == target_event_id) else "CONTEXT" if kind in {"SYSTEM", "USER", "ASSISTANT_MESSAGE"} else "FAILURE"
        selected.append(
            {
                "evidence_id": ref_id,
                "source_id": event_id,
                "event_kind": kind,
                "sequence_number": row.get("sequence_number"),
                "source_pointer": pointer,
                "role": role,
                "content_sha256": row.get("visible_payload_sha256"),
                "text": _text(payload),
                "payload": payload,
            }
        )
    users = [item for item in selected if item["event_kind"] == "USER" and item.get("text")]
    pending = [item for item in selected if item["event_kind"] == "TOOL_CALL"]
    missing_refs = missing or []
    reasons = ["MISSING_EVIDENCE"] if missing_refs else []
    if not users:
        reasons.append("NO_USER_QUERY")
    if pending:
        reasons.append("PENDING_TOOL_RESULT")
    result = {
        "schema_version": "traceforge.task-reconstruction-input.v1",
        "capture_id": capture_id,
        "query_ordinal": query_ordinal,
        "task_query_candidates": [item["text"] for item in users if item["role"] == "TASK"],
        "context": [item for item in selected if item["role"] == "CONTEXT"],
        "evidence": selected,
        "missing_evidence_ids": missing_refs,
        "selection": {
            "evidence_requested": len(selected) + len(missing_refs),
            "evidence_resolved": len(selected),
            "missing_count": len(missing_refs),
            "user_query_count": len(users),
            "target_user_count": sum(item["role"] == "TASK" for item in selected),
            "context_user_count": sum(item["role"] == "CONTEXT" and item["event_kind"] == "USER" for item in selected),
            "context_event_count": sum(item["role"] == "CONTEXT" for item in selected),
            "pending_tool_call_count": len(pending),
        },
        "quality": {
            "usable": bool(users and selected),
            "requires_review": bool(reasons),
            "reason_codes": reasons,
        },
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _read_rows(path: str | Path, capture_id: str, kinds: set[str]) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            if row.get("capture_occurrence_id") == capture_id and row.get("event_kind") in kinds:
                rows.append(row)
    return sorted(rows, key=lambda row: (row.get("sequence_number", 0), row.get("event_occurrence_id", "")))


def build_capture_input(*, event_occurrences_path: str | Path, capture_id: str, output_path: str | Path, max_events: int = 200) -> dict[str, Any]:
    """选择一个 capture 的 USER/TOOL 事实，生成最小可审计输入。"""
    rows = _read_rows(event_occurrences_path, capture_id, {"USER", "TOOL_CALL", "TOOL_RESULT"})[:max_events]
    return _emit(capture_id=capture_id, rows=rows, output_path=output_path)


def build_query_task_input(*, event_occurrences_path: str | Path, capture_id: str, query_ordinal: int = -1, output_path: str | Path) -> dict[str, Any]:
    """按 USER 边界拆出单个 QueryTurn，并保留该问题之前的可见上下文。"""
    rows = _read_rows(event_occurrences_path, capture_id, {"SYSTEM", "USER", "TOOL_CALL", "TOOL_RESULT", "ASSISTANT_MESSAGE"})
    starts = [index for index, row in enumerate(rows) if row.get("event_kind") == "USER"]
    if not starts:
        return _emit(capture_id=capture_id, rows=[], output_path=output_path, missing=["NO_USER_QUERY"], query_ordinal=query_ordinal)
    if not -len(starts) <= query_ordinal < len(starts):
        return _emit(capture_id=capture_id, rows=[], output_path=output_path, missing=["QUERY_ORDINAL_OUT_OF_RANGE"], query_ordinal=query_ordinal)
    position = query_ordinal if query_ordinal >= 0 else len(starts) + query_ordinal
    start = starts[position]
    end = starts[position + 1] if position + 1 < len(starts) else len(rows)
    before = [row for row in rows[:start] if row.get("event_kind") in {"SYSTEM", "USER", "ASSISTANT_MESSAGE"}]
    turn = rows[start:end]
    target_event_id = rows[start].get("event_occurrence_id")
    return _emit(capture_id=capture_id, rows=before + turn, output_path=output_path, query_ordinal=query_ordinal, target_event_id=target_event_id)


def build_task_input(*, evidence_path: str | Path, event_occurrences_path: str | Path, capture_id: str, output_path: str | Path) -> dict[str, Any]:
    """按已有证据引用 join；缺失引用显式记录。"""
    refs = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    wanted = {ref.get("source_id") for ref in refs if isinstance(ref, dict)}
    rows = {row.get("event_occurrence_id"): row for row in _read_rows(event_occurrences_path, capture_id, {"SYSTEM", "USER", "TOOL_CALL", "TOOL_RESULT", "ASSISTANT_MESSAGE"})}
    selected, missing = [], []
    for ref in refs:
        source_id = ref.get("source_id") if isinstance(ref, dict) else None
        row = rows.get(source_id)
        if row is None:
            missing.append(source_id)
        else:
            selected.append(row)
    return _emit(capture_id=capture_id, rows=selected, output_path=output_path, missing=missing)
