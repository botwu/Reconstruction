"""筛选模型输出的任务标签归一化与关系校验。

模型只提出标签和证据；本模块将其归一为稳定的派生记录，并对范围、时序、
证据引用和任务覆盖做 fail-closed 校验。原始 session 仍由 observable 模块完整
提供，标签不能覆盖原始事实。
"""

from __future__ import annotations

import hashlib
from typing import Any

from .contracts import (
    OUTCOME_TAGS,
    RELATION_TYPES,
    TASK_LABEL_SCHEMA,
    TASK_OUTCOMES,
)


def _span_bounds(evidence: dict[str, Any]) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for span in evidence.get("spans") or ():
        if not isinstance(span, dict):
            continue
        span_id = span.get("span_id")
        if not isinstance(span_id, str) or not span_id:
            continue
        try:
            start = int(span["message_start"])
            end = int(span["message_end"])
        except (KeyError, TypeError, ValueError):
            continue
        result[span_id] = (start, end)
    return result


def _stable_task_id(source_ref: object, span_ids: list[str]) -> str:
    # Span IDs are content-addressed. Include source_ref to prevent IDs from
    # colliding when the same session is screened in different captures.
    payload = f"{source_ref or ''}|{'|'.join(span_ids)}"
    return "task_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _refs(value: object) -> tuple[list[int], list[str], list[str]]:
    if not isinstance(value, dict):
        return [], [], ["EVIDENCE_REFS_REQUIRED"]
    raw_messages = value.get("message_indices")
    raw_spans = value.get("span_ids")
    errors: list[str] = []
    messages: list[int] = []
    spans: list[str] = []
    if not isinstance(raw_messages, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in raw_messages
    ):
        errors.append("EVIDENCE_MESSAGE_INDICES_INVALID")
    else:
        messages = list(dict.fromkeys(raw_messages))
    if not isinstance(raw_spans, list) or any(not isinstance(item, str) or not item for item in raw_spans):
        errors.append("EVIDENCE_SPAN_IDS_INVALID")
    else:
        spans = list(dict.fromkeys(raw_spans))
    if not messages and not spans:
        errors.append("EVIDENCE_REFS_EMPTY")
    return messages, spans, errors


def derive_task_tags(
    *,
    outcome: str,
    actionable: bool,
    needs_reconstruction: bool | None,
    reconstruction_eligible: bool = False,
    eligibility_decision: str | None = None,
    superseded: bool = False,
) -> list[str]:
    """冻结的 task tag 派生：只由 outcome / 门禁决定，模型不能写入。"""

    tags = {"actionable" if actionable else "non_actionable"}
    tags.add(OUTCOME_TAGS.get(outcome, "outcome_unknown"))
    if needs_reconstruction is True:
        tags.add("needs_reconstruction")
    elif needs_reconstruction is False:
        tags.add("no_reconstruction_needed")
    if reconstruction_eligible:
        tags.update({"selected_for_reconstruction", "rubric_pass"})
    else:
        tags.add("rubric_reject" if eligibility_decision == "REJECT" else "rubric_review")
    if superseded:
        tags.add("superseded_by_later_success")
    return sorted(tags)


def apply_task_tags(task: dict[str, Any]) -> list[str]:
    """按当前 task 字段重写 tags，供 screening 与 reconstruct 共用。"""

    eligibility = task.get("eligibility") if isinstance(task.get("eligibility"), dict) else {}
    tags = derive_task_tags(
        outcome=str(task.get("outcome") or ""),
        actionable=bool(task.get("is_actionable", True)),
        needs_reconstruction=task.get("needs_reconstruction"),
        reconstruction_eligible=task.get("reconstruction_eligible") is True,
        eligibility_decision=eligibility.get("decision"),
        superseded="LATER_SAME_GOAL_SUCCESS" in (eligibility.get("blocking_reason_codes") or []),
    )
    task["tags"] = tags
    return tags


def _base_task_tags(*, outcome: str, actionable: bool, needs_reconstruction: bool | None) -> list[str]:
    return derive_task_tags(
        outcome=outcome,
        actionable=actionable,
        needs_reconstruction=needs_reconstruction,
        reconstruction_eligible=False,
        eligibility_decision="REVIEW",
    )


def is_selected_reconstruction_task(task: dict[str, Any]) -> bool:
    """下游选任务：布尔门禁与 selected tag 必须同时成立。"""

    if task.get("is_actionable") is False:
        return False
    if task.get("reconstruction_eligible") is not True:
        return False
    return "selected_for_reconstruction" in set(task.get("tags") or [])


def build_session_tags(*, tasks: list[dict[str, Any]], relations: list[dict[str, Any]], decision: str | None = None) -> list[str]:
    """整条 capture 的索引标签。

    作用：检索、Intent 读会话形态、审计。不决定 ELIGIBLE，不挑选重建 task，
    不分流 TERMINAL_FILE。入选仍看 decision / reconstruction_eligible /
    selected_for_reconstruction；执行域仍看 domain_route + 回放树。
    """
    tags: set[str] = set()
    if len(tasks) > 1:
        tags.add("multiple_tasks")
    if any(t.get("is_actionable") is False for t in tasks):
        tags.add("contains_non_actionable")
    if any(t.get("outcome") in {"FAILURE", "INCOMPLETE"} for t in tasks):
        tags.add("contains_unfinished_task")
    if any(t.get("reconstruction_eligible") is True for t in tasks):
        tags.update({"contains_reconstruction_candidate", "needs_reconstruction"})
    if any(t.get("outcome") == "SUCCESS" for t in tasks):
        tags.add("contains_success_task")
    for relation in relations:
        kind = relation.get("type")
        if kind in {"continuation", "correction", "dependency"}:
            tags.add("has_" + str(kind))
    if decision:
        tags.add("screening_" + str(decision).lower())
    return sorted(tags)


def _task_label(
    item: dict[str, Any],
    *,
    index: int,
    source_ref: object,
    span_bounds: dict[str, tuple[int, int]],
    all_message_count: int,
) -> tuple[dict[str, Any], list[str], str | None]:
    errors: list[str] = []
    raw_ids = item.get("span_ids")
    if not isinstance(raw_ids, list) or not raw_ids or any(
        not isinstance(value, str) or not value for value in raw_ids
    ):
        errors.append(f"TASK_SPAN_IDS_INVALID:{index}")
        span_ids: list[str] = []
    else:
        span_ids = list(dict.fromkeys(raw_ids))
        if len(span_ids) != len(raw_ids):
            errors.append(f"TASK_SPAN_IDS_DUPLICATED:{index}")
    unknown = [span_id for span_id in span_ids if span_id not in span_bounds]
    if unknown:
        errors.append(f"TASK_SPAN_ID_UNKNOWN:{index}")
    known = [span_id for span_id in span_ids if span_id in span_bounds]
    ordered_known = sorted(known, key=lambda span_id: span_bounds[span_id][0])
    stable_id_value = _stable_task_id(source_ref, ordered_known or span_ids)
    raw_task_id = item.get("task_id")
    if not isinstance(raw_task_id, str) or not raw_task_id.strip():
        errors.append(f"TASK_ID_REQUIRED:{index}")
        model_id = f"task_{index + 1}"
    else:
        model_id = raw_task_id.strip()

    actionable = item.get("is_actionable")
    if not isinstance(actionable, bool):
        errors.append(f"IS_ACTIONABLE_REQUIRED:{index}")
        actionable = False
    outcome = item.get("outcome")
    if outcome not in TASK_OUTCOMES:
        errors.append(f"OUTCOME_INVALID:{index}")
        outcome = "UNCERTAIN"
    need = item.get("needs_reconstruction")
    if actionable:
        if outcome == "SUCCESS" and need is not False:
            errors.append(f"SUCCESS_NEEDS_RECONSTRUCTION_MUST_BE_FALSE:{index}")
        elif outcome == "UNCERTAIN" and need is not None:
            errors.append(f"UNCERTAIN_NEEDS_RECONSTRUCTION_MUST_BE_NULL:{index}")
        elif outcome in {"FAILURE", "INCOMPLETE"} and need is not True:
            errors.append(f"FAILED_TASK_NEEDS_RECONSTRUCTION_MUST_BE_TRUE:{index}")
    elif need is not False:
        errors.append(f"NON_ACTIONABLE_NEEDS_RECONSTRUCTION_MUST_BE_FALSE:{index}")
    if need not in {True, False, None}:
        errors.append(f"NEEDS_RECONSTRUCTION_INVALID:{index}")
        need = None

    refs_messages, refs_spans, ref_errors = _refs(item.get("evidence_refs"))
    errors.extend(f"{code}:{index}" for code in ref_errors)
    if any(message >= all_message_count for message in refs_messages):
        errors.append(f"EVIDENCE_MESSAGE_INDEX_UNKNOWN:{index}")
    if any(span not in span_bounds for span in refs_spans):
        errors.append(f"EVIDENCE_SPAN_ID_UNKNOWN:{index}")
    if known and not set(refs_spans).intersection(known):
        errors.append(f"EVIDENCE_DOES_NOT_TOUCH_TASK:{index}")
    if actionable and not refs_messages and not refs_spans:
        errors.append(f"ACTIONABLE_EVIDENCE_REQUIRED:{index}")

    rubric = item.get("rubric")
    if not isinstance(rubric, dict):
        rubric = {}
        errors.append(f"RUBRIC_REQUIRED:{index}")
    reason = item.get("reason")
    if not isinstance(reason, str):
        reason = ""
        errors.append(f"REASON_REQUIRED:{index}")
    task = {
        "schema_version": TASK_LABEL_SCHEMA,
        "task_id": stable_id_value,
        "model_task_id": model_id,
        "span_ids": span_ids,
        "is_actionable": actionable,
        "outcome": outcome,
        "needs_reconstruction": need,
        "domain_route": str(item.get("domain_route") or "other"),
        "rubric": rubric,
        "reason": reason,
        "evidence_refs": {"message_indices": refs_messages, "span_ids": refs_spans},
        # 标签是规则可复现的派生字段；模型不能直接伪造 selected 标签。
        "tags": _base_task_tags(outcome=outcome, actionable=actionable, needs_reconstruction=need),
        "reconstruction_eligible": False,
        "eligibility": {"decision": "REVIEW", "blocking_reason_codes": []},
    }
    return task, errors, model_id


def _validate_relations(
    raw_relations: object,
    *,
    tasks: list[dict[str, Any]],
    span_bounds: dict[str, tuple[int, int]],
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    if raw_relations is None:
        return [], ["RELATIONS_REQUIRED"]
    if not isinstance(raw_relations, list):
        return [], ["RELATIONS_NOT_ARRAY"]
    by_model = {str(task["model_task_id"]): task for task in tasks}
    by_stable = {str(task["task_id"]): task for task in tasks}
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    starts = {
        task["task_id"]: min((span_bounds[s][0] for s in task["span_ids"] if s in span_bounds), default=10**9)
        for task in tasks
    }
    for index, item in enumerate(raw_relations):
        if not isinstance(item, dict):
            errors.append(f"RELATION_NOT_OBJECT:{index}")
            continue
        source = item.get("from_task_id")
        target = item.get("to_task_id")
        relation_type = item.get("type")
        source_task = by_model.get(str(source)) or by_stable.get(str(source))
        target_task = by_model.get(str(target)) or by_stable.get(str(target))
        if source_task is None or target_task is None:
            errors.append(f"RELATION_TASK_UNKNOWN:{index}")
            continue
        source_id = str(source_task["task_id"])
        target_id = str(target_task["task_id"])
        key = (source_id, target_id, str(relation_type))
        if source_id == target_id:
            errors.append(f"RELATION_SELF_LOOP:{index}")
        if relation_type not in RELATION_TYPES:
            errors.append(f"RELATION_TYPE_INVALID:{index}")
        if starts[source_id] >= starts[target_id]:
            errors.append(f"RELATION_NOT_FORWARD:{index}")
        if key in seen:
            errors.append(f"RELATION_DUPLICATED:{index}")
        seen.add(key)
        messages, spans, ref_errors = _refs(item.get("evidence_refs"))
        errors.extend(f"{code}:{index}" for code in ref_errors)
        if any(span not in span_bounds for span in spans):
            errors.append(f"RELATION_EVIDENCE_SPAN_UNKNOWN:{index}")
        endpoint_spans = set(source_task["span_ids"]) | set(target_task["span_ids"])
        if spans and not set(spans).issubset(endpoint_spans):
            errors.append(f"RELATION_EVIDENCE_OUTSIDE_ENDPOINTS:{index}")
        source_bounds = [span_bounds[s] for s in source_task["span_ids"] if s in span_bounds]
        target_bounds = [span_bounds[s] for s in target_task["span_ids"] if s in span_bounds]
        source_cited = bool(set(spans).intersection(source_task["span_ids"])) or any(
            any(start <= message < end for start, end in source_bounds) for message in messages
        )
        target_cited = bool(set(spans).intersection(target_task["span_ids"])) or any(
            any(start <= message < end for start, end in target_bounds) for message in messages
        )
        if not source_cited:
            errors.append(f"RELATION_SOURCE_EVIDENCE_MISSING:{index}")
        if not target_cited:
            errors.append(f"RELATION_TARGET_EVIDENCE_MISSING:{index}")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"RELATION_REASON_REQUIRED:{index}")
            reason = ""
        result.append(
            {
                "from_task_id": source_id,
                "to_task_id": target_id,
                "type": relation_type,
                "evidence_refs": {"message_indices": messages, "span_ids": spans},
                "reason": reason,
            }
        )
    return result, errors


def normalize_task_labels(
    payload: dict[str, Any], *, evidence: dict[str, Any], source_ref: object
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], tuple[str, ...], str]:
    """归一化 v10 标签；任何契约错误均由上层 fail closed。"""

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return [], [], ("TASKS_NOT_ARRAY" if not isinstance(raw_tasks, list) else "TASKS_EMPTY",), "INVALID"
    bounds = _span_bounds(evidence)
    all_message_count = int((evidence.get("span_audit") or {}).get("message_count") or 0)
    errors: list[str] = []
    tasks: list[dict[str, Any]] = []
    model_ids: set[str] = set()
    seen_spans: set[str] = set()
    for index, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            errors.append(f"TASK_NOT_OBJECT:{index}")
            continue
        task, task_errors, model_id = _task_label(
            raw,
            index=index,
            source_ref=source_ref,
            span_bounds=bounds,
            all_message_count=all_message_count,
        )
        if model_id in model_ids:
            errors.append(f"TASK_MODEL_ID_DUPLICATED:{index}")
        model_ids.add(model_id)
        overlap = seen_spans.intersection(task["span_ids"])
        if overlap:
            errors.append(f"TASK_SPAN_OVERLAP:{index}:{','.join(sorted(overlap))}")
        seen_spans.update(task["span_ids"])
        errors.extend(task_errors)
        tasks.append(task)
    if set(bounds) != seen_spans:
        missing = sorted(set(bounds) - seen_spans)
        if missing:
            errors.append("TASK_SPAN_UNASSIGNED:" + ",".join(missing))
    relations, relation_errors = _validate_relations(
        payload.get("relations"), tasks=tasks, span_bounds=bounds
    )
    errors.extend(relation_errors)
    # A v9 response without the detailed fields remains readable but is never
    # admitted. This makes compatibility explicit instead of silently guessing.
    legacy = any("is_actionable" not in raw or "evidence_refs" not in raw for raw in raw_tasks if isinstance(raw, dict))
    status = "LEGACY_INCOMPLETE" if legacy else ("INVALID" if errors else "COMPLETE")
    return tasks, relations, tuple(dict.fromkeys(errors)), status


__all__ = [
    "apply_task_tags",
    "build_session_tags",
    "derive_task_tags",
    "is_selected_reconstruction_task",
    "normalize_task_labels",
]
