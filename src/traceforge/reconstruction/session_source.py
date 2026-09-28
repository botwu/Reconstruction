"""从完整 session 与筛选标签构造不可变的重建源。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.reconstruction.env_replay import normalize_file_ops
from traceforge.screening.observable import build_spans
from traceforge.screening.contracts import TRIAGE_PROMPT_VERSION
from traceforge.screening.task_labels import (
    apply_task_tags,
    build_session_tags,
    is_selected_reconstruction_task,
)
from traceforge.trajectory.privacy import omit_private_reasoning

SOURCE_SCHEMA = "traceforge.reconstruction-source.v3"


class ReconstructionSourceError(ValueError):
    """无法从原始 session 构造重建源。"""


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("value")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    if isinstance(content, dict):
        value = content.get("value") or content.get("text")
        if isinstance(value, str):
            return value.strip()
    return ""


def _result_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """保留原始 content 槽位与文本空白，供并行结果按位置对应。"""
    content = message.get("content")
    if content is None:
        return []
    blocks = content if isinstance(content, list) else [content]
    result: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        text = block if isinstance(block, str) else None
        if isinstance(block, dict) and block.get("type") in (None, "text", "output_text"):
            for field in ("text", "value"):
                if isinstance(block.get(field), str):
                    text = block[field]
                    break
        result.append({"index": index, "text": text})
    return result


def _role(message: Any) -> str:
    return str(message.get("role", "")) if isinstance(message, dict) else ""


def _tool_name(call: dict[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return str(call.get("name") or "")


def _tool_arguments(call: dict[str, Any]) -> Any:
    function = call.get("function")
    raw = function.get("arguments") if isinstance(function, dict) else call.get("arguments")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _call_id(call: dict[str, Any]) -> str:
    value = call.get("id") or call.get("tool_call_id")
    return str(value) if value else ""


def _span_at(spans: list[Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for span in spans:
        for index in range(span.message_start, span.message_end):
            result[index] = span.span_id
    return result


def _tool_timeline(messages: list[dict[str, Any]], spans: list[Any]) -> list[dict[str, Any]]:
    index_to_span = _span_at(spans)
    pending: dict[str, dict[str, Any]] = {}
    timeline: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if _role(message) == "assistant":
            for call in message.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                call_id = _call_id(call)
                item = {
                    "call_id": call_id,
                    "name": _tool_name(call),
                    "arguments": _tool_arguments(call),
                    "result_text": None,
                    "result_blocks": [],
                    "pending": True,
                    "span_id": index_to_span.get(index),
                    "assistant_message_index": index,
                }
                timeline.append(item)
                if call_id:
                    pending[call_id] = item
        if _role(message) == "tool":
            call_id = str(message.get("tool_call_id") or "")
            item = pending.get(call_id)
            if item is None:
                timeline.append({
                    "call_id": call_id,
                    "name": str(message.get("name") or message.get("tool_name") or ""),
                    "arguments": None,
                    "result_text": _message_text(message),
                    "result_blocks": _result_blocks(message),
                    "pending": False,
                    "span_id": index_to_span.get(index),
                    "orphan_tool_result": True,
                    "tool_message_index": index,
                })
            else:
                item["result_text"] = _message_text(message)
                item["result_blocks"] = _result_blocks(message)
                item["pending"] = False
                item["tool_message_index"] = index
    return timeline


def timeline_has_file_ops(timeline: list[dict[str, Any]]) -> bool:
    return any(op.get("kind") in {"read", "write"} for op in normalize_file_ops(timeline))


def _span_records(spans: list[Any], messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for span in spans:
        users = [_message_text(messages[i]) for i in span.user_message_indices if 0 <= i < len(messages)]
        result[span.span_id] = {
            "span_id": span.span_id,
            "message_start": span.message_start,
            "message_end": span.message_end,
            "message_indices": list(range(span.message_start, span.message_end)),
            "user_message_indices": list(span.user_message_indices),
            "user_texts": [x for x in users if x],
        }
    return result


def _evidence_refs(item: dict[str, Any], fallback_spans: list[str], span_map: dict[str, dict[str, Any]]) -> dict[str, list[Any]]:
    refs = item.get("evidence_refs")
    if not isinstance(refs, dict):
        refs = {}
    span_ids = [str(x) for x in (refs.get("span_ids") or item.get("span_ids") or fallback_spans) if str(x) in span_map]
    message_indices = [int(x) for x in (refs.get("message_indices") or item.get("message_indices") or []) if isinstance(x, int) and x >= 0]
    if not message_indices:
        for sid in span_ids:
            message_indices.extend(span_map[sid]["message_indices"])
    return {"span_ids": list(dict.fromkeys(span_ids)), "message_indices": sorted(set(message_indices))}


def _tagged_tasks(record: dict[str, Any], span_map: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    triage = record.get("triage") if isinstance(record.get("triage"), dict) else {}
    raw_tasks = triage.get("tasks")
    raw_relations = triage.get("relations") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        selected = [str(x) for x in (triage.get("selected_span_ids") or []) if str(x) in span_map]
        if not selected:
            raise ReconstructionSourceError("selected_span_ids 对不上原始 messages，且 ELIGIBLE 记录缺少带任务标签的 tasks[]")
        tasks = []
        for sid in selected:
            refs = _evidence_refs({"span_ids": [sid]}, [sid], span_map)
            texts = [t for s in refs["span_ids"] for t in span_map[s]["user_texts"]]
            tasks.append({
                "task_id": f"legacy-{sid}", "span_ids": refs["span_ids"],
                "message_indices": refs["message_indices"], "user_texts": texts,
                "is_actionable": True, "outcome": "UNKNOWN",
                "reconstruction_eligible": False,
                "eligibility": {"decision": "REVIEW", "blocking_reason_codes": ["LEGACY_TASK_LABELS_INCOMPLETE"]},
                "label_status": "LEGACY_INCOMPLETE",
                "evidence_refs": refs,
            })
        for task in tasks:
            apply_task_tags(task)
        return tasks, [], "LEGACY_INCOMPLETE"
    tasks = []
    seen_spans: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise ReconstructionSourceError("triage.tasks 含非对象")
        declared_spans = [str(x) for x in (raw.get("span_ids") or ([raw["span_id"]] if raw.get("span_id") else []))]
        unknown = [sid for sid in declared_spans if sid not in span_map]
        if unknown:
            raise ReconstructionSourceError("task span_ids 对不上原始 messages：" + ",".join(unknown))
        if not declared_spans:
            raise ReconstructionSourceError("task 缺少 span_ids")
        overlap = seen_spans.intersection(declared_spans)
        if overlap:
            raise ReconstructionSourceError("一个 span 被多个任务占用：" + ",".join(sorted(overlap)))
        seen_spans.update(declared_spans)
        # evidence_refs 是证明，不是任务边界；任务范围必须保留所有被分组 span 的用户澄清。
        refs = _evidence_refs(raw, declared_spans, span_map)
        scope_indices = sorted({i for sid in declared_spans for i in span_map[sid]["message_indices"]})
        texts = [t for sid in declared_spans for t in span_map[sid]["user_texts"]]
        task = dict(raw)
        task_id = str(raw.get("task_id") or "")
        if not task_id:
            raise ReconstructionSourceError("task 缺少稳定 task_id")
        task.update({"task_id": task_id, "span_ids": declared_spans, "message_indices": scope_indices, "user_texts": texts, "evidence_refs": refs})
        task.setdefault("is_actionable", True)
        # reconstruction_eligible 是布尔门禁；tags 是同一套派生词汇，供下游选择和提示。
        task.setdefault("reconstruction_eligible", False)
        apply_task_tags(task)
        tasks.append(task)
    relations = [x for x in raw_relations if isinstance(x, dict)]
    ids = {x["task_id"] for x in tasks}
    relations = [x for x in relations if x.get("from_task_id") in ids and x.get("to_task_id") in ids and x.get("from_task_id") != x.get("to_task_id")]
    label_status = "COMPLETE" if triage.get("label_status") == "COMPLETE" else "LEGACY_INCOMPLETE"
    if label_status != "COMPLETE":
        for task in tasks:
            task["reconstruction_eligible"] = False
            task.setdefault("eligibility", {}).setdefault("blocking_reason_codes", []).append("LEGACY_TASK_LABELS_INCOMPLETE")
            apply_task_tags(task)
    return tasks, relations, label_status


def build_reconstruction_source(*, raw_line: str, record: dict[str, Any]) -> dict[str, Any]:
    if record.get("decision") != "ELIGIBLE":
        raise ReconstructionSourceError(f"只从 ELIGIBLE 构造重建源，当前 decision={record.get('decision')}")
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise ReconstructionSourceError("原始行不是合法 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise ReconstructionSourceError("原始行缺少 messages 数组")
    raw_messages = [item if isinstance(item, dict) else {} for item in payload["messages"]]
    # span_id 由筛选在完整原始 messages 上计算；先对齐 span，再剥 reasoning 落盘。
    spans, span_meta = build_spans(raw_messages)
    span_map = _span_records(spans, raw_messages)
    tasks, relations, label_status = _tagged_tasks(record, span_map)
    timeline = _tool_timeline(raw_messages, spans)
    triage = record.get("triage") if isinstance(record.get("triage"), dict) else {}
    session_tags = build_session_tags(
        tasks=tasks,
        relations=relations,
        decision=record.get("decision"),
    )
    recorded_session_tags = [str(x) for x in (triage.get("session_tags") or []) if isinstance(x, str)]
    if recorded_session_tags and set(recorded_session_tags) != set(session_tags):
        raise ReconstructionSourceError("session_tags 与筛选任务/关系不一致")
    selected = [task for task in tasks if is_selected_reconstruction_task(task)]
    selected_span_ids = [sid for task in selected for sid in task["span_ids"]]
    selected_tools = [item for item in timeline if item.get("span_id") is None or item.get("span_id") in set(selected_span_ids)]
    persisted = omit_private_reasoning(payload)
    return {
        "schema_version": SOURCE_SCHEMA,
        "source_ref": record.get("source_ref"),
        "line_number": record.get("line_number"),
        "line_sha256": record.get("line_sha256") or hashlib.sha256(raw_line.encode()).hexdigest(),
        "route": record.get("route"),
        "label_status": label_status,
        "selected_task_ids": [task["task_id"] for task in selected],
        "selected_span_ids": selected_span_ids,
        "session_tags": session_tags,
        "tasks": tasks,
        "relations": relations,
        "tool_timeline": timeline,
        "selected_tool_timeline": selected_tools,
        "selected_span_has_file_ops": timeline_has_file_ops(selected_tools),
        "raw_session": persisted,
        "session": {
            "message_count": len(raw_messages), "span_count": len(spans),
            "tool_call_count": len(timeline), "selected_tool_call_count": len(selected_tools),
            "pending_tool_call_count": sum(1 for x in timeline if x.get("pending")),
        },
        "privacy": {"private_thinking_reasoning": "omitted"},
        "policy": {"compile": False, "evidence_join": False, "unit": "eligible_raw_session", "projection": False},
    }


def load_eligible_record(records_path: str | Path, *, line_number: int) -> dict[str, Any]:
    path = Path(records_path)
    if not path.is_file():
        raise ReconstructionSourceError(f"筛选记录不存在：{path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        if isinstance(row, dict) and row.get("line_number") == line_number:
            if row.get("decision") != "ELIGIBLE":
                raise ReconstructionSourceError(f"line_number={line_number} 的 decision={row.get('decision')}，不是 ELIGIBLE")
            triage = row.get("triage")
            tasks = triage.get("tasks") if isinstance(triage, dict) else None
            prompt_version = triage.get("prompt_version") if isinstance(triage, dict) else None
            # Reconstruction consumes only v10 task labels. Older records have
            # span_ids but no stable task_id/evidence contract; accepting them
            # would silently turn a legacy screening result into a new task.
            if prompt_version != TRIAGE_PROMPT_VERSION or not isinstance(tasks, list) or not tasks:
                raise ReconstructionSourceError(
                    "筛选记录不是 reconstruction-screening-triage-v10，必须重新运行 screening run"
                )
            invalid = [
                item for item in tasks
                if not isinstance(item, dict)
                or not isinstance(item.get("task_id"), str)
                or not item.get("task_id")
                or not isinstance(item.get("evidence_refs"), dict)
            ]
            if invalid:
                raise ReconstructionSourceError(
                    "v10 筛选记录缺少稳定 task_id/evidence_refs，必须重新运行 screening run"
                )
            return row
    raise ReconstructionSourceError(f"筛选记录中没有 line_number={line_number}")


def load_raw_line(input_path: str | Path, *, line_number: int, line_sha256: str | None = None) -> str:
    path = Path(input_path)
    if not path.is_file():
        raise ReconstructionSourceError(f"原始 JSONL 不存在：{path}")
    with path.open(encoding="utf-8") as handle:
        for index, raw in enumerate(handle, start=1):
            if index == line_number:
                if line_sha256 and hashlib.sha256(raw.encode()).hexdigest() != line_sha256:
                    raise ReconstructionSourceError(f"line_number={line_number} 的 sha256 与筛选记录不一致")
                return raw
    raise ReconstructionSourceError(f"原始 JSONL 没有第 {line_number} 行")


def write_reconstruction_source(source: dict[str, Any], output_dir: str | Path) -> Path:
    root = Path(output_dir); root.mkdir(parents=True, exist_ok=True)
    path = root / "reconstruction_source.json"
    persisted = omit_private_reasoning(source)
    if isinstance(persisted, dict):
        privacy = persisted.setdefault("privacy", {})
        if isinstance(privacy, dict):
            privacy["private_thinking_reasoning"] = "omitted"
    path.write_text(json.dumps(persisted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# Public read-only wrappers used by raw-session intake. The underlying helpers stay
# private so the legacy screened source keeps its existing implementation.
def message_text(message: dict[str, Any]) -> str:
    return _message_text(message)


def tool_timeline(messages: list[dict[str, Any]], spans: list[Any]) -> list[dict[str, Any]]:
    return _tool_timeline(messages, spans)


def span_records(spans: list[Any], messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return _span_records(spans, messages)
