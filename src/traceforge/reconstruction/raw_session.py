"""从原始 JSONL session 构造不依赖筛选标签的重建 source。

Raw-session intake 保留整条 session 的事实与 provenance。任务边界由独立只读
Session Task Agent 依据 user spans 分组；本模块不会把每个 user turn 静态当成
任务，也不会把失败、成功或难度标签作为丢弃条件。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents import AgentRuntime, AgentSession
from traceforge.reconstruction.agents.roles import SESSION_TASK_ROLE
from traceforge.reconstruction.env_replay import normalize_file_ops
from traceforge.reconstruction.session_source import (
    message_text,
    span_records,
    tool_timeline,
)
from traceforge.reconstruction.session_spans import build_spans

RAW_SOURCE_SCHEMA = "traceforge.reconstruction-source.raw-session.v1"
SEGMENTATION_SCHEMA = "traceforge.session-task-segmentation.v1"


class RawSessionSourceError(ValueError):
    """原始 session 无法形成可审计 source。"""


def _persist(root: Path, name: str, payload: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _task_id(source_hash: str, span_ids: list[str]) -> str:
    value = source_hash + "|" + "|".join(span_ids)
    return "rawtask_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _user_records(raw_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, message in enumerate(raw_messages):
        if message.get("role") != "user":
            continue
        text = message_text(message)
        if text:
            result.append({"id": f"user:{index}", "message_index": index, "text": text})
    return result


def _segmentation_prompt(
    *, spans: list[Any], user_records: list[dict[str, Any]],
    raw_messages: list[dict[str, Any]],
) -> str:
    entries = []
    for span in spans:
        entries.append(
            {
                "span_id": span.span_id,
                "message_start": span.message_start,
                "message_end": span.message_end,
                "user_message_indices": list(span.user_message_indices),
            }
        )
    previous_responses = [
        {"before_span_id": span.span_id, "message_index": index, "text": text}
        for position, span in enumerate(spans)
        for index in range(spans[position - 1].message_start if position else 0, span.message_start)
        if raw_messages[index].get("role") == "assistant"
        if (text := message_text(raw_messages[index]))
    ]
    return "\n".join(
        [
            "Segment this complete raw session into distinct user tasks for reconstruction.",
            (
                'Return JSON only: {"tasks":[{"span_ids":[...],'
                '"evidence_refs":{"message_indices":[...]},"task_kind":"..."}],'
                '"context_span_ids":[...],"relations":[{"from_span_id":"...",'
                '"to_span_id":"...","kind":"continuation|correction|context"}],'
                '"label_status":"COMPLETE"}.'
            ),
            (
                "Group spans by one coherent user goal. A continuation or correction "
                "stays with its parent task when it clearly refers to it. "
                "Do not merge unrelated goals. "
                "任务边界还取决于请求开始时已有的工作。先核对 PREVIOUS_RESPONSES 中的原始助手回复；"
                "若新请求以此前已交付产物为起点，提出新的行为或修复目标，应建立独立任务，"
                "用 context 关系保留前置依赖；不能仅因项目或文件相同就合并。"
                "对尚未完成目标的格式纠正、补充约束及继续执行仍合并，不按成功或报错词汇机械切分。"
                "助手自述只作状态线索；有歧义时用 read_session_message 核对邻近工具返回及修改。"
            ),
            (
                "Every span must occur exactly once in either tasks[].span_ids or "
                "context_span_ids. Never omit a span. Do not create a task solely "
                "because a user turn exists; do not drop a task because it succeeded, "
                "failed, is difficult, or looks unreconstructable."
            ),
            (
                "evidence_refs.message_indices must reference only user messages in "
                "that task's spans. Do not copy assistant/tool actions into user "
                "evidence. Do not invent ids or paths."
            ),
            "SPAN_CATALOG=" + json.dumps(entries, ensure_ascii=False),
            "USER_MESSAGES=" + json.dumps(user_records, ensure_ascii=False),
            "PREVIOUS_RESPONSES=" + json.dumps(previous_responses, ensure_ascii=False),
        ]
    )


def _parse_segmentation(
    payload: Any, *, spans: list[Any], span_map: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return [], [], [], ["INVALID_SEGMENTATION_PAYLOAD"]
    if payload.get("label_status") != "COMPLETE":
        errors.append("LABEL_STATUS_NOT_COMPLETE")
    raw_tasks = payload.get("tasks")
    raw_context = payload.get("context_span_ids")
    if not isinstance(raw_tasks, list) or not isinstance(raw_context, list):
        return [], [], [], ["SEGMENTATION_FIELDS_MISSING"]
    known = set(span_map)
    assigned: dict[str, str] = {}
    tasks: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_tasks):
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("span_ids"), list)
            or not raw["span_ids"]
        ):
            errors.append(f"TASK_{index}_MISSING_SPAN_IDS")
            continue
        ids = [str(value) for value in raw["span_ids"]]
        for span_id in ids:
            if span_id not in known:
                errors.append(f"UNKNOWN_SPAN:{span_id}")
            if span_id in assigned:
                errors.append(f"SPAN_OVERLAP:{span_id}")
            assigned[span_id] = f"task:{index}"
        refs = raw.get("evidence_refs")
        refs = refs if isinstance(refs, dict) else {}
        evidence_indices = refs.get("message_indices")
        if not isinstance(evidence_indices, list):
            errors.append(f"TASK_{index}_MISSING_EVIDENCE_REFS")
            evidence_indices = []
        # bool 虽然是 int 的子类，但不能作为消息索引；拒绝坏证据，避免静默丢弃后误建任务。
        evidence_invalid = any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in evidence_indices
        )
        if evidence_invalid:
            errors.append(f"TASK_{index}_EVIDENCE_INDEX_INVALID")
        evidence_indices = [
            value
            for value in evidence_indices
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        expected = {
            message_index
            for span_id in ids
            if span_id in span_map
            for message_index in span_map[span_id]["user_message_indices"]
        }
        # evidence refs 是锚点，可以只引用任务范围的一部分；
        # 任务范围仍保留所有 user 消息。
        if (
            evidence_invalid
            or any(message_index not in expected for message_index in evidence_indices)
            or (expected and not evidence_indices)
        ):
            errors.append(f"TASK_{index}_EVIDENCE_SCOPE_MISMATCH")
        tasks.append(
            {
                "span_ids": ids,
                "message_indices": sorted(expected),
                "evidence_message_indices": sorted(set(evidence_indices)),
                "task_kind": str(raw.get("task_kind") or "task"),
            }
        )
    context = [str(value) for value in raw_context]
    for span_id in context:
        if span_id not in known:
            errors.append(f"UNKNOWN_CONTEXT_SPAN:{span_id}")
        if span_id in assigned:
            errors.append(f"SPAN_OVERLAP_CONTEXT:{span_id}")
        assigned[span_id] = "context"
    missing = sorted(known - set(assigned))
    if missing:
        errors.append("UNASSIGNED_SPANS:" + ",".join(missing))
    if len(assigned) != len(known):
        errors.append("SPAN_COVERAGE_NOT_EXACT")
    relations = [item for item in (payload.get("relations") or []) if isinstance(item, dict)]
    for relation in relations:
        if relation.get("from_span_id") not in known or relation.get("to_span_id") not in known:
            errors.append("RELATION_UNKNOWN_SPAN")
    return tasks, context, relations, errors


def _result_payload(agent_result: Any) -> tuple[dict[str, Any], list[str], bool, str | None]:
    payload = getattr(agent_result, "payload", None)
    errors = list(getattr(agent_result, "errors", []) or [])
    completed = bool(getattr(agent_result, "completed", False))
    final_text = getattr(agent_result, "final_text", None)
    if not isinstance(payload, dict):
        payload = {}
    return payload, errors, completed, final_text


def build_raw_session_source(
    *,
    raw_line: str,
    line_number: int,
    source_ref: str,
    agent: AgentRuntime,
    output_root: Path,
) -> dict[str, Any]:
    """构造 RAW_SESSION source，并持久化可审计的分组收据。"""
    root = Path(output_root)
    line_hash = hashlib.sha256(raw_line.encode("utf-8")).hexdigest()
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        receipt = {
            "schema_version": SEGMENTATION_SCHEMA,
            "status": "INPUT_INVALID",
            "errors": ["INVALID_JSON", str(exc)],
            "line_number": line_number,
            "line_sha256": line_hash,
        }
        _persist(root, "session_task_segmentation.json", receipt)
        raise RawSessionSourceError("原始行不是合法 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        receipt = {
            "schema_version": SEGMENTATION_SCHEMA,
            "status": "INPUT_INVALID",
            "errors": ["MESSAGES_MISSING"],
            "line_number": line_number,
            "line_sha256": line_hash,
        }
        _persist(root, "session_task_segmentation.json", receipt)
        raise RawSessionSourceError("原始行缺少 messages 数组")
    raw_messages = [item if isinstance(item, dict) else {} for item in payload["messages"]]
    spans, span_meta = build_spans(raw_messages)
    span_map = span_records(spans, raw_messages)
    users = _user_records(raw_messages)
    prompt = _segmentation_prompt(spans=spans, user_records=users, raw_messages=raw_messages)
    session = AgentSession(
        user_records=users,
        user_texts=[item["text"] for item in users],
        session_context=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    try:
        result = agent.run(
            role=SESSION_TASK_ROLE, instruction=prompt, session=session, output_root=root
        )
    except (
        Exception
    ) as exc:  # 运行失败不能误记为原始任务的边界问题
        receipt = {
            "schema_version": SEGMENTATION_SCHEMA,
            "status": "SESSION_TASK_AGENT_FAILED",
            "errors": ["AGENT_EXCEPTION", str(exc)],
            "line_number": line_number,
            "line_sha256": line_hash,
            "span_count": len(spans),
        }
        _persist(root, "session_task_segmentation.json", receipt)
        raise RawSessionSourceError("session task 分组 Agent 异常") from exc
    model_payload, agent_errors, completed, final_text = _result_payload(result)
    if agent_errors or not completed:
        task_groups, context_ids, relations = [], [], []
        errors = agent_errors or ["AGENT_INCOMPLETE"]
        status = "SESSION_TASK_AGENT_FAILED"
    else:
        task_groups, context_ids, relations, errors = _parse_segmentation(
            model_payload, spans=spans, span_map=span_map
        )
        status = "SESSION_TASK_REVIEW" if errors else "READY"
    receipt = {
        "schema_version": SEGMENTATION_SCHEMA,
        "status": status,
        "line_number": line_number,
        "source_ref": source_ref,
        "prompt_version": "session-boundaries-v2-prior-state",
        "line_sha256": line_hash,
        "span_count": len(spans),
        "assigned_span_count": len(
            {sid for item in task_groups for sid in item["span_ids"]} | set(context_ids)
        ),
        "task_count": len(task_groups),
        "context_span_ids": context_ids,
        "errors": errors,
        "agent": {
            "role": SESSION_TASK_ROLE.name,
            "backend": getattr(result, "backend", "unknown"),
            "completed": completed,
            "turns": len(getattr(result, "turns", []) or []),
        },
        "model_payload": model_payload,
        "model_response_text": final_text,
    }
    _persist(root, "session_task_segmentation.json", receipt)
    if status == "SESSION_TASK_AGENT_FAILED":
        raise RawSessionSourceError(
            "任务分组 Agent 运行失败：" + ", ".join(errors)
            + f"；详情见 {root / 'session_task_segmentation.json'}"
        )
    if status != "READY":
        raise RawSessionSourceError("原始 session 任务边界需要人工复核")
    source_tasks: list[dict[str, Any]] = []
    selected_ids: list[str] = []
    for group in task_groups:
        task_id = _task_id(line_hash, group["span_ids"])
        indices = group["message_indices"]
        texts = [
            message_text(raw_messages[index])
            for index in indices
            if 0 <= index < len(raw_messages) and raw_messages[index].get("role") == "user"
        ]
        evidence = {
            "message_indices": group["evidence_message_indices"],
            "span_ids": group["span_ids"],
        }
        task = {
            "task_id": task_id,
            "span_ids": group["span_ids"],
            "message_indices": indices,
            "user_texts": texts,
            "evidence_refs": evidence,
            "task_kind": group["task_kind"],
        }
        source_tasks.append(task)
        selected_ids.append(task_id)
    timeline = tool_timeline(raw_messages, spans)
    source = {
        "schema_version": RAW_SOURCE_SCHEMA,
        "entry_mode": "RAW_SESSION",
        "source_ref": source_ref,
        "line_number": line_number,
        "line_sha256": line_hash,
        "label_status": "RAW_SESSION_SEGMENTED",
        "selected_task_ids": selected_ids,
        "selected_span_ids": [sid for task in source_tasks for sid in task["span_ids"]],
        "session_tags": ["RAW_SESSION"],
        "context_span_ids": context_ids,
        "tasks": source_tasks,
        "relations": relations,
        "tool_timeline": timeline,
        "selected_tool_timeline": timeline,
        "selected_span_has_file_ops": bool(normalize_file_ops(timeline)),
        "raw_session": payload,
        "session": {
            "message_count": len(raw_messages),
            "span_count": len(spans),
            "tool_call_count": len(timeline),
            "context_span_count": len(context_ids),
        },
        "span_meta": span_meta,
        "segmentation_receipt": "session_task_segmentation.json",
        "privacy": {"raw_session": "verbatim"},
        "policy": {
            "compile": False,
            "evidence_join": False,
            "unit": "raw_session",
            "projection": False,
        },
    }
    return source
