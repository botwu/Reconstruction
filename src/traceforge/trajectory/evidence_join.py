"""把编译轨迹投影为可重建 Task 输入，并保留执行阶段事实。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content", {}) if isinstance(payload, dict) else {}
    if isinstance(content, str):
        return content
    value = content.get("value") if isinstance(content, dict) else None
    return value if isinstance(value, str) else None


def _jsonl(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None or not Path(path).exists():
        return []
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def _tool_call_id(row: dict[str, Any]) -> str | None:
    payload = row.get("payload", {})
    if not isinstance(payload, dict):
        return None
    value = payload.get("tool_call_id")
    if isinstance(value, str):
        return value
    function = payload.get("function")
    if isinstance(function, dict):
        value = function.get("tool_call_id")
        if isinstance(value, str):
            return value
    return None


def _pairing_state(
    rows: list[dict[str, Any]], pairings: list[dict[str, Any]]
) -> tuple[set[str], set[str]]:
    """返回已匹配和明确未观测结果的 call_id；不把未观测误报为执行失败。"""
    matched: set[str] = set()
    missing: set[str] = set()
    for pairing in pairings:
        call_id = pairing.get("tool_call_id")
        if not isinstance(call_id, str):
            continue
        statuses = {str(x) for x in pairing.get("statuses", [])}
        if pairing.get("matched_result_event_id") or "MATCHED_ONE_TO_ONE" in statuses:
            matched.add(call_id)
        elif "RESULT_NOT_OBSERVED" in statuses:
            missing.add(call_id)
    if pairings:
        return matched, missing

    def identity(row: dict[str, Any]) -> str:
        return _tool_call_id(row) or "event:" + str(row.get("event_occurrence_id", ""))

    calls = {identity(row) for row in rows if row.get("event_kind") == "TOOL_CALL"}
    results = {identity(row) for row in rows if row.get("event_kind") == "TOOL_RESULT"}
    return results, calls - results


def _phase(kind: str, *, before_target: bool, target: bool) -> str:
    if before_target or kind == "SYSTEM":
        return "PRE_TASK_CONTEXT"
    if target and kind == "USER":
        return "TARGET_REQUEST"
    if kind == "TOOL_CALL":
        return "ATTEMPT_ACTION"
    if kind == "TOOL_RESULT":
        return "ATTEMPT_OBSERVATION"
    if kind == "ASSISTANT_MESSAGE":
        return "ATTEMPT_RESPONSE"
    return "ATTEMPT_RESPONSE"


def _emit(
    *,
    capture_id: str,
    rows: list[dict[str, Any]],
    output_path: str | Path,
    missing: list[str] | None = None,
    query_ordinal: int | None = None,
    target_event_id: str | None = None,
    source_row_count: int | None = None,
    truncated: bool = False,
    pairings: list[dict[str, Any]] | None = None,
    capture_meta: dict[str, Any] | None = None,
    boundary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    target_seen = target_event_id is None
    matched, missing_calls = _pairing_state(rows, pairings or [])
    for row in rows:
        event_id = row.get("event_occurrence_id")
        pointer = row.get("source_json_pointer")
        ref_id = hashlib.sha256(f"event:{event_id}:{pointer}".encode()).hexdigest()
        kind = row.get("event_kind")
        payload = row.get("payload", {})
        is_target = target_event_id is None or event_id == target_event_id
        if is_target and kind == "USER":
            target_seen = True
        phase = _phase(kind, before_target=not target_seen and not is_target, target=is_target)
        if target_event_id is None:
            phase = (
                "TARGET_REQUEST"
                if kind == "USER"
                else (
                    "ATTEMPT_ACTION"
                    if kind == "TOOL_CALL"
                    else "ATTEMPT_OBSERVATION"
                    if kind == "TOOL_RESULT"
                    else "PRE_TASK_CONTEXT"
                    if kind == "SYSTEM"
                    else "ATTEMPT_RESPONSE"
                )
            )
        # role 是旧接口字段；phase 承载细粒度阶段，工具事件不再伪装成 FAILURE。
        role = (
            "TASK"
            if phase == "TARGET_REQUEST"
            else "CONTEXT"
            if phase == "PRE_TASK_CONTEXT"
            else "ATTEMPT"
        )
        selected.append(
            {
                "evidence_id": ref_id,
                "source_id": event_id,
                "event_kind": kind,
                "sequence_number": row.get("sequence_number"),
                "source_pointer": pointer,
                "role": role,
                "phase": phase,
                "content_sha256": row.get("visible_payload_sha256"),
                "text": _text(payload),
                "payload": payload,
                "_source_occurrence_ids": list(
                    row.get("_source_occurrence_ids") or [str(event_id or "")]
                ),
            }
        )
    users = [item for item in selected if item["event_kind"] == "USER" and item.get("text")]
    pending_ids = sorted(
        missing_calls
        | {
            call_id
            for call_id in (
                _tool_call_id(row) for row in rows if row.get("event_kind") == "TOOL_CALL"
            )
            if call_id is not None and call_id not in matched and not pairings
        }
    )
    missing_refs = list(missing or [])
    reasons = ["MISSING_EVIDENCE"] if missing_refs else []
    if not users:
        reasons.append("NO_USER_QUERY")
    if pending_ids:
        reasons.append("PENDING_TOOL_RESULT")
    if truncated:
        reasons.append("INPUT_TRUNCATED")
    result = {
        "schema_version": "traceforge.task-reconstruction-input.v2",
        "capture_id": capture_id,
        "query_ordinal": query_ordinal,
        "task_query_candidates": [item["text"] for item in users if item["role"] == "TASK"],
        "context": [item for item in selected if item["phase"] == "PRE_TASK_CONTEXT"],
        "evidence": selected,
        "missing_evidence_ids": missing_refs,
        "selection": {
            "evidence_requested": len(selected) + len(missing_refs),
            "evidence_resolved": len(selected),
            "missing_count": len(missing_refs),
            "user_query_count": len(users),
            "target_user_count": sum(item["phase"] == "TARGET_REQUEST" for item in selected),
            "context_user_count": sum(
                item["phase"] == "PRE_TASK_CONTEXT" and item["event_kind"] == "USER"
                for item in selected
            ),
            "context_event_count": sum(item["phase"] == "PRE_TASK_CONTEXT" for item in selected),
            "attempt_action_count": sum(item["phase"] == "ATTEMPT_ACTION" for item in selected),
            "attempt_observation_count": sum(
                item["phase"] == "ATTEMPT_OBSERVATION" for item in selected
            ),
            "attempt_response_count": sum(item["phase"] == "ATTEMPT_RESPONSE" for item in selected),
            "pending_tool_call_count": len(pending_ids),
            "pending_tool_call_ids": pending_ids,
            "source_row_count": source_row_count if source_row_count is not None else len(rows),
            "selected_row_count": len(rows),
            "truncated": truncated,
        },
        "provenance": {
            "capture": capture_meta or {},
            "request_boundary": boundary or {},
            "snapshot": bool(capture_meta or boundary),
            "unobserved_result_is_not_failure": True,
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
    return sorted(
        rows, key=lambda row: (row.get("sequence_number", 0), row.get("event_occurrence_id", ""))
    )


def _metadata(
    *,
    capture_id: str,
    captures_path: str | Path | None,
    request_boundaries_path: str | Path | None,
    tool_pairings_path: str | Path | None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    capture = next(
        (r for r in _jsonl(captures_path) if r.get("capture_occurrence_id") == capture_id), {}
    )
    boundary = next(
        (
            r
            for r in _jsonl(request_boundaries_path)
            if r.get("capture_occurrence_id") == capture_id
        ),
        {},
    )
    pairings = [
        r for r in _jsonl(tool_pairings_path) if r.get("capture_occurrence_id") == capture_id
    ]
    return capture, boundary, pairings


def build_capture_input(
    *,
    event_occurrences_path: str | Path,
    capture_id: str,
    output_path: str | Path,
    max_events: int = 200,
    captures_path: str | Path | None = None,
    request_boundaries_path: str | Path | None = None,
    tool_pairings_path: str | Path | None = None,
) -> dict[str, Any]:
    """选择一个 capture 的事实；截断会被记录且不会伪装成完整输入。"""
    all_rows = _read_rows(
        event_occurrences_path,
        capture_id,
        {"SYSTEM", "USER", "TOOL_CALL", "TOOL_RESULT", "ASSISTANT_MESSAGE"},
    )
    rows = all_rows[:max_events]
    capture, boundary, pairings = _metadata(
        capture_id=capture_id,
        captures_path=captures_path,
        request_boundaries_path=request_boundaries_path,
        tool_pairings_path=tool_pairings_path,
    )
    return _emit(
        capture_id=capture_id,
        rows=rows,
        output_path=output_path,
        source_row_count=len(all_rows),
        truncated=len(rows) < len(all_rows),
        pairings=pairings,
        capture_meta=capture,
        boundary=boundary,
    )


def build_query_task_input(
    *,
    event_occurrences_path: str | Path,
    capture_id: str,
    query_ordinal: int = -1,
    output_path: str | Path,
    captures_path: str | Path | None = None,
    request_boundaries_path: str | Path | None = None,
    tool_pairings_path: str | Path | None = None,
    max_events: int = 2000,
) -> dict[str, Any]:
    """按 USER 边界拆出 QueryTurn，保留目标前上下文和目标后的执行事实。"""
    all_rows = _read_rows(
        event_occurrences_path,
        capture_id,
        {"SYSTEM", "USER", "TOOL_CALL", "TOOL_RESULT", "ASSISTANT_MESSAGE"},
    )
    starts = [index for index, row in enumerate(all_rows) if row.get("event_kind") == "USER"]
    if not starts:
        return _emit(
            capture_id=capture_id,
            rows=[],
            output_path=output_path,
            missing=["NO_USER_QUERY"],
            query_ordinal=query_ordinal,
        )
    if not -len(starts) <= query_ordinal < len(starts):
        return _emit(
            capture_id=capture_id,
            rows=[],
            output_path=output_path,
            missing=["QUERY_ORDINAL_OUT_OF_RANGE"],
            query_ordinal=query_ordinal,
        )
    position = query_ordinal if query_ordinal >= 0 else len(starts) + query_ordinal
    start = starts[position]
    end = starts[position + 1] if position + 1 < len(starts) else len(all_rows)
    before = all_rows[:start]
    turn = all_rows[start:end]
    target_event_id = all_rows[start].get("event_occurrence_id")
    selected = before + turn
    truncated = len(selected) > max_events
    if truncated:
        selected = selected[:max_events]
    capture, boundary, pairings = _metadata(
        capture_id=capture_id,
        captures_path=captures_path,
        request_boundaries_path=request_boundaries_path,
        tool_pairings_path=tool_pairings_path,
    )
    return _emit(
        capture_id=capture_id,
        rows=selected,
        output_path=output_path,
        query_ordinal=query_ordinal,
        target_event_id=target_event_id,
        source_row_count=len(all_rows),
        truncated=truncated,
        pairings=pairings,
        capture_meta=capture,
        boundary=boundary,
    )


def build_task_input(
    *,
    evidence_path: str | Path,
    event_occurrences_path: str | Path,
    capture_id: str,
    output_path: str | Path,
    captures_path: str | Path | None = None,
    request_boundaries_path: str | Path | None = None,
    tool_pairings_path: str | Path | None = None,
) -> dict[str, Any]:
    """按已有证据引用 join；缺失引用显式记录。"""
    refs = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    rows = {
        row.get("event_occurrence_id"): row
        for row in _read_rows(
            event_occurrences_path,
            capture_id,
            {"SYSTEM", "USER", "TOOL_CALL", "TOOL_RESULT", "ASSISTANT_MESSAGE"},
        )
    }
    selected, missing = [], []
    for ref in refs:
        source_id = ref.get("source_id") if isinstance(ref, dict) else None
        row = rows.get(source_id)
        if row is None:
            missing.append(source_id)
        else:
            selected.append(row)
    capture, boundary, pairings = _metadata(
        capture_id=capture_id,
        captures_path=captures_path,
        request_boundaries_path=request_boundaries_path,
        tool_pairings_path=tool_pairings_path,
    )
    return _emit(
        capture_id=capture_id,
        rows=selected,
        output_path=output_path,
        missing=missing,
        pairings=pairings,
        capture_meta=capture,
        boundary=boundary,
    )


def build_session_input(
    *,
    event_occurrences_path: str | Path,
    capture_id: str,
    output_path: str | Path,
    captures_path: str | Path | None = None,
    request_boundaries_path: str | Path | None = None,
    tool_pairings_path: str | Path | None = None,
) -> dict[str, Any]:
    """按 candidate_group_id 聚合完整 session；不按 USER 回合或事件预算裁剪。"""
    captures = _jsonl(captures_path)
    selected_capture = next(
        (item for item in captures if item.get("capture_occurrence_id") == capture_id), {}
    )
    group_id = selected_capture.get("candidate_group_id")
    if group_id is None:
        group_id = selected_capture.get("thread_id") or selected_capture.get("session_ref")
    members = [
        item
        for item in captures
        if group_id is not None
        and (
            item.get("candidate_group_id") == group_id
            or (not item.get("candidate_group_id") and item.get("thread_id") == group_id)
        )
    ]
    if not members:
        members = (
            [selected_capture] if selected_capture else [{"capture_occurrence_id": capture_id}]
        )
    member_ids = {str(item.get("capture_occurrence_id")) for item in members}
    raw_rows = [
        row
        for row in _jsonl(event_occurrences_path)
        if str(row.get("capture_occurrence_id")) in member_ids
        and row.get("event_kind")
        in {"SYSTEM", "USER", "TOOL_CALL", "TOOL_RESULT", "ASSISTANT_MESSAGE"}
    ]
    member_order = {
        str(item.get("capture_occurrence_id")): (
            str(item.get("request_time_start") or ""),
            str(item.get("response_time_end") or ""),
            str(item.get("source_capture_id") or ""),
            str(item.get("capture_occurrence_id") or ""),
        )
        for item in members
    }
    raw_rows.sort(
        key=lambda row: (
            member_order.get(str(row.get("capture_occurrence_id")), ("", "", "", "")),
            int(row.get("sequence_number") or 0),
            str(row.get("event_occurrence_id") or ""),
        )
    )
    canonical: list[dict[str, Any]] = []
    by_fingerprint: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in raw_rows:
        try:
            payload_key = json.dumps(
                row.get("payload"), ensure_ascii=False, sort_keys=True, default=str
            )
        except (TypeError, ValueError):
            payload_key = repr(row.get("payload"))
        key = (int(row.get("sequence_number") or 0), str(row.get("event_kind") or ""), payload_key)
        existing = by_fingerprint.get(key)
        if existing is None:
            existing = dict(row)
            existing["_source_occurrence_ids"] = [str(row.get("event_occurrence_id") or "")]
            by_fingerprint[key] = existing
            canonical.append(existing)
        else:
            existing["_source_occurrence_ids"].append(str(row.get("event_occurrence_id") or ""))
    # raw_rows has already been ordered by capture chronology and then each
    # capture's local sequence. Do not sort by local sequence again: sequence
    # numbers restart in every capture and that would reorder a whole session.
    boundaries = [
        row
        for row in _jsonl(request_boundaries_path)
        if str(row.get("capture_occurrence_id")) in member_ids
    ]
    pairings = [
        row
        for row in _jsonl(tool_pairings_path)
        if str(row.get("capture_occurrence_id")) in member_ids
    ]
    capture_meta = {
        "session_identity": {
            "candidate_group_id": group_id,
            "capture_occurrence_ids": sorted(member_ids),
            "target_capture_occurrence_id": capture_id,
        },
        "captures": sorted(
            members,
            key=lambda item: member_order.get(
                str(item.get("capture_occurrence_id")), ("", "", "", "")
            ),
        ),
        "request_boundaries": boundaries,
        "deduplication": {
            "raw_event_count": len(raw_rows),
            "resolved_event_count": len(canonical),
            "shared_prefix_dedup_count": len(raw_rows) - len(canonical),
            "strategy": "sequence,event_kind,payload fingerprint; source ids retained",
        },
    }
    result = _emit(
        capture_id=capture_id,
        rows=canonical,
        output_path=output_path,
        source_row_count=len(raw_rows),
        truncated=False,
        pairings=pairings,
        capture_meta=capture_meta,
        boundary={"capture_occurrence_ids": sorted(member_ids), "boundaries": boundaries},
    )
    result["selection"]["session_capture_count"] = len(members)
    result["selection"]["raw_session_event_count"] = len(raw_rows)
    result["selection"]["deduplicated_session_event_count"] = len(canonical)
    result["selection"]["query_ordinal"] = None
    result["selection"]["truncated"] = False
    result["provenance"]["session_identity"] = capture_meta["session_identity"]
    result["provenance"]["request_boundaries"] = boundaries
    result["provenance"]["captures"] = capture_meta["captures"]
    Path(output_path).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result
