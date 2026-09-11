"""把编译轨迹安全投影为可重建 Task 输入。"""
from __future__ import annotations
import hashlib, json
from pathlib import Path
from typing import Any

def _text(payload: dict[str, Any]) -> str | None:
    c = payload.get("content", {}) if isinstance(payload, dict) else {}
    v = c.get("value") if isinstance(c, dict) else None
    return v if isinstance(v, str) else None

def _emit(*, capture_id: str, rows: list[dict[str, Any]], output_path: str | Path, missing: list[str] | None = None) -> dict[str, Any]:
    selected=[]
    for row in rows:
        eid=row.get("event_occurrence_id"); pointer=row.get("source_json_pointer")
        ref_id=hashlib.sha256(f"event:{eid}:{pointer}".encode()).hexdigest()
        payload=row.get("payload", {})
        selected.append({"evidence_id":ref_id,"source_id":eid,"event_kind":row.get("event_kind"),
          "sequence_number":row.get("sequence_number"),"source_pointer":pointer,
          "role":"TASK" if row.get("event_kind")=="USER" else "FAILURE",
          "content_sha256":row.get("visible_payload_sha256"),"text":_text(payload) if row.get("event_kind")=="USER" else None,
          "payload":payload})
    users=[x for x in selected if x["event_kind"]=="USER" and x.get("text")]
    pending=[x for x in selected if x["event_kind"]=="TOOL_CALL"]
    miss=missing or []
    pending_count=len(pending)
    reasons=(['MISSING_EVIDENCE'] if miss else [])+(['NO_USER_QUERY'] if not users else [])
    if pending_count:
        reasons.append('PENDING_TOOL_RESULT')
    result={"schema_version":"traceforge.task-reconstruction-input.v1","capture_id":capture_id,
      "task_query_candidates":[x["text"] for x in users],"evidence":selected,"missing_evidence_ids":miss,
      "selection":{"evidence_requested":len(selected)+len(miss),"evidence_resolved":len(selected),"missing_count":len(miss),"user_query_count":len(users),"pending_tool_call_count":len(pending)},
      "quality":{"usable":bool(users and selected),"requires_review":bool(reasons),
        "reason_codes":reasons}}
    out=Path(output_path); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n"); return result

def build_capture_input(*, event_occurrences_path: str|Path, capture_id: str, output_path: str|Path, max_events: int=200) -> dict[str,Any]:
    """选择一个 capture 的 USER/TOOL 事实，生成最小可审计输入。"""
    rows=[]
    for line in Path(event_occurrences_path).open():
        row=json.loads(line)
        if row.get("capture_occurrence_id")==capture_id and row.get("event_kind") in {"USER","TOOL_CALL","TOOL_RESULT"}:
            rows.append(row)
    rows=sorted(rows,key=lambda r:(r.get("sequence_number",0),r.get("event_occurrence_id","")))[:max_events]
    return _emit(capture_id=capture_id,rows=rows,output_path=output_path)

def build_query_task_input(*, event_occurrences_path: str|Path, capture_id: str, query_ordinal: int = -1, output_path: str|Path) -> dict[str,Any]:
    """按 USER 边界拆出单个 QueryTurn，避免把一条 session 当作一个任务。"""
    all_rows=[]
    for line in Path(event_occurrences_path).open():
        row=json.loads(line)
        if row.get("capture_occurrence_id")==capture_id and row.get("event_kind") in {"USER","TOOL_CALL","TOOL_RESULT","ASSISTANT_MESSAGE"}:
            all_rows.append(row)
    all_rows.sort(key=lambda r:(r.get("sequence_number",0),r.get("event_occurrence_id","")))
    starts=[i for i,r in enumerate(all_rows) if r.get("event_kind")=="USER"]
    if not starts: return _emit(capture_id=capture_id,rows=[],output_path=output_path,missing=["NO_USER_QUERY"])
    if not -len(starts) <= query_ordinal < len(starts):
        return _emit(capture_id=capture_id,rows=[],output_path=output_path,missing=["QUERY_ORDINAL_OUT_OF_RANGE"])
    idx=starts[query_ordinal]
    end=starts[query_ordinal+1] if query_ordinal >= 0 and query_ordinal+1 < len(starts) else (starts[query_ordinal+1] if query_ordinal < -1 and abs(query_ordinal+1)<=len(starts) else len(all_rows))
    return _emit(capture_id=capture_id,rows=all_rows[idx:end],output_path=output_path)

def build_task_input(*, evidence_path: str|Path, event_occurrences_path: str|Path, capture_id: str, output_path: str|Path) -> dict[str,Any]:
    """按已有证据引用 join；缺失引用显式记录。"""
    refs=json.loads(Path(evidence_path).read_text()); wanted={r.get("source_id") for r in refs if isinstance(r,dict)}
    rows={}
    for line in Path(event_occurrences_path).open():
        row=json.loads(line)
        if row.get("capture_occurrence_id")==capture_id and row.get("event_occurrence_id") in wanted: rows[row["event_occurrence_id"]]=row
    selected=[]; missing=[]
    for ref in refs:
        sid=ref.get("source_id") if isinstance(ref,dict) else None; row=rows.get(sid)
        if row is None: missing.append(sid); continue
        selected.append(row)
    return _emit(capture_id=capture_id,rows=selected,output_path=output_path,missing=missing)
