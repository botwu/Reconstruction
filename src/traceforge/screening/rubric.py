"""重建筛选 rubric：定义分数含义，并把模型分数量化成入选门禁。

模型只打分和分类；ELIGIBLE / REJECT / REVIEW / DEFER 由本模块的规则决定。
规则层硬拒绝不可被模型改写。
硬门槛是有效任务（R1）且没完成或完成不好（R2）。工具调用只辅助判断环境和过程，不是入选条件。
"""

from __future__ import annotations

from typing import Any

from .contracts import (
    RUBRIC_KEYS,
    DomainRoute,
    ScreeningDecision,
    ScreeningRoute,
)
from .task_labels import apply_task_tags

RUBRIC_SPEC = {
    "task_identifiability": {
        "id": "R1",
        "title": "任务可识别性",
        "scores": {
            0: "无任务",
            1: "只能猜测",
            2: "目标基本明确",
            3: "目标、交付物和关键约束都有用户证据",
        },
    },
    "failure_evidence": {
        "id": "R2",
        "title": "失败或未完成证据",
        "scores": {
            0: "用户目标已交付，过程正常结束",
            1: "有失败或未完成迹象但很弱",
            2: "没完成或完成不好：环境问题、模型没做好、缺必要工具、截断或轨迹不合理",
            3: "失败或未完成的边界可定位",
        },
    },
}

ZERO_REJECT_KEYS = (
    "task_identifiability",
    "failure_evidence",
)
REVIEW_IF_ONE_KEYS = (
    "task_identifiability",
    "failure_evidence",
)
TASK_MIN_KEYS = (
    "task_identifiability",
    "failure_evidence",
)


def _eligible_route(domain_route: str) -> str:
    if domain_route == DomainRoute.CODE_FILE.value:
        return ScreeningRoute.ELIGIBLE_CODE_FILE.value
    return ScreeningRoute.ELIGIBLE_TASK.value


def empty_rubric() -> dict[str, int]:
    return {key: 0 for key in RUBRIC_KEYS}


def coerce_rubric(raw: object) -> tuple[dict[str, int], tuple[str, ...]]:
    """把模型 rubric 收成 0--3 整数；缺项记 0 并报错码。"""

    errors: list[str] = []
    rubric = empty_rubric()
    if not isinstance(raw, dict):
        return rubric, ("RUBRIC_NOT_OBJECT",)
    for key in RUBRIC_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
            errors.append(f"RUBRIC_INVALID:{key}")
            rubric[key] = 0
        else:
            rubric[key] = value
    return rubric, tuple(errors)


def admit_after_model(
    *,
    rule: dict[str, Any],
    outcome: str,
    needs_reconstruction: bool | None,
    domain_route: str,
    rubric: dict[str, int],
    parse_errors: tuple[str, ...],
) -> dict[str, Any]:
    """规则硬拒绝优先；否则用 rubric 关门，不把模型 decision 当终态。"""

    if rule.get("decision") == ScreeningDecision.REJECT.value:
        return {
            "decision": ScreeningDecision.REJECT.value,
            "route": ScreeningRoute.RULE_HARD_REJECT.value,
            "rule_pass": False,
            "blocking_reason_codes": tuple(rule.get("blocking_reason_codes") or ()),
        }
    if rule.get("decision") == ScreeningDecision.DEFER.value:
        return {
            "decision": ScreeningDecision.DEFER.value,
            "route": ScreeningRoute.COST_DEFERRED.value,
            "rule_pass": True,
            "blocking_reason_codes": tuple(rule.get("blocking_reason_codes") or ()),
        }
    if parse_errors:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.MODEL_TRIAGE_FAILED.value,
            "rule_pass": True,
            "blocking_reason_codes": parse_errors,
        }

    reasons: list[str] = []
    if outcome == "SUCCESS":
        return {
            "decision": ScreeningDecision.REJECT.value,
            "route": ScreeningRoute.SUCCESS_NOT_RECONSTRUCTED.value,
            "rule_pass": True,
            "blocking_reason_codes": ("OUTCOME_SUCCESS",),
        }
    if outcome == "UNCERTAIN":
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.RUBRIC_REVIEW.value,
            "rule_pass": True,
            "blocking_reason_codes": ("OUTCOME_UNCERTAIN",),
        }
    if outcome not in {"FAILURE", "INCOMPLETE"}:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.MODEL_TRIAGE_FAILED.value,
            "rule_pass": True,
            "blocking_reason_codes": ("OUTCOME_INVALID",),
        }
    if domain_route == DomainRoute.RETRIEVAL.value:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.RETRIEVAL_BACKEND_NOT_READY.value,
            "rule_pass": True,
            "blocking_reason_codes": ("SEARCH_DOMAIN_OUT_OF_SCOPE",),
        }
    if needs_reconstruction is not True:
        reasons.append("NEEDS_RECONSTRUCTION_NOT_TRUE")

    for key in ZERO_REJECT_KEYS:
        if rubric.get(key, 0) == 0:
            reasons.append(f"RUBRIC_ZERO:{key}")
            return {
                "decision": ScreeningDecision.REJECT.value,
                "route": ScreeningRoute.RUBRIC_REJECT.value,
                "rule_pass": True,
                "blocking_reason_codes": tuple(reasons),
            }
    for key in REVIEW_IF_ONE_KEYS:
        if rubric.get(key, 0) == 1:
            reasons.append(f"RUBRIC_WEAK:{key}")
            return {
                "decision": ScreeningDecision.REVIEW.value,
                "route": ScreeningRoute.RUBRIC_REVIEW.value,
                "rule_pass": True,
                "blocking_reason_codes": tuple(reasons),
            }
    if reasons:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.RUBRIC_REVIEW.value,
            "rule_pass": True,
            "blocking_reason_codes": tuple(reasons),
        }
    missing = [key for key in TASK_MIN_KEYS if rubric.get(key, 0) < 2]
    if missing:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.RUBRIC_REVIEW.value,
            "rule_pass": True,
            "blocking_reason_codes": tuple(f"RUBRIC_BELOW_MIN:{key}" for key in missing),
        }
    return {
        "decision": ScreeningDecision.ELIGIBLE.value,
        "route": _eligible_route(domain_route),
        "rule_pass": True,
        "blocking_reason_codes": (),
        "selected_span_ids": (),
    }


def _task_gate_result(
    *,
    outcome: str,
    needs_reconstruction: bool | None,
    domain_route: str,
    rubric: dict[str, int],
    is_actionable: bool = True,
    label_status: str | None = None,
) -> dict[str, Any]:
    """对单个 task 应用与 admit_after_model 相同的硬门槛（不含规则层）。"""

    reasons: list[str] = []
    if label_status is not None and label_status != "COMPLETE":
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.MODEL_TRIAGE_FAILED.value,
            "blocking_reason_codes": ("TASK_LABELS_INCOMPLETE",),
        }
    if not is_actionable:
        return {
            "decision": ScreeningDecision.REJECT.value,
            "route": ScreeningRoute.SUCCESS_NOT_RECONSTRUCTED.value,
            "blocking_reason_codes": ("TASK_NOT_ACTIONABLE",),
        }
    if outcome == "SUCCESS":
        return {
            "decision": ScreeningDecision.REJECT.value,
            "route": ScreeningRoute.SUCCESS_NOT_RECONSTRUCTED.value,
            "blocking_reason_codes": ("OUTCOME_SUCCESS",),
        }
    if outcome == "UNCERTAIN":
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.RUBRIC_REVIEW.value,
            "blocking_reason_codes": ("OUTCOME_UNCERTAIN",),
        }
    if outcome not in {"FAILURE", "INCOMPLETE"}:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.MODEL_TRIAGE_FAILED.value,
            "blocking_reason_codes": ("OUTCOME_INVALID",),
        }
    if domain_route == DomainRoute.RETRIEVAL.value:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.RETRIEVAL_BACKEND_NOT_READY.value,
            "blocking_reason_codes": ("SEARCH_DOMAIN_OUT_OF_SCOPE",),
        }
    if needs_reconstruction is not True:
        reasons.append("NEEDS_RECONSTRUCTION_NOT_TRUE")

    for key in ZERO_REJECT_KEYS:
        if rubric.get(key, 0) == 0:
            reasons.append(f"RUBRIC_ZERO:{key}")
            return {
                "decision": ScreeningDecision.REJECT.value,
                "route": ScreeningRoute.RUBRIC_REJECT.value,
                "blocking_reason_codes": tuple(reasons),
            }
    for key in REVIEW_IF_ONE_KEYS:
        if rubric.get(key, 0) == 1:
            reasons.append(f"RUBRIC_WEAK:{key}")
            return {
                "decision": ScreeningDecision.REVIEW.value,
                "route": ScreeningRoute.RUBRIC_REVIEW.value,
                "blocking_reason_codes": tuple(reasons),
            }
    if reasons:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.RUBRIC_REVIEW.value,
            "blocking_reason_codes": tuple(reasons),
        }
    missing = [key for key in TASK_MIN_KEYS if rubric.get(key, 0) < 2]
    if missing:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.RUBRIC_REVIEW.value,
            "blocking_reason_codes": tuple(f"RUBRIC_BELOW_MIN:{key}" for key in missing),
        }
    return {
        "decision": ScreeningDecision.ELIGIBLE.value,
        "route": _eligible_route(domain_route),
        "blocking_reason_codes": (),
    }


def admit_after_tasks(
    *,
    rule: dict[str, Any],
    tasks: list[dict[str, Any]],
    parse_errors: tuple[str, ...],
    relations: list[dict[str, Any]] | None = None,
    label_status: str | None = None,
) -> dict[str, Any]:
    """对任务列表关门：任一有效且没做好的任务过线则整条 ELIGIBLE。"""

    if rule.get("decision") == ScreeningDecision.REJECT.value:
        return {
            "decision": ScreeningDecision.REJECT.value,
            "route": ScreeningRoute.RULE_HARD_REJECT.value,
            "rule_pass": False,
            "blocking_reason_codes": tuple(rule.get("blocking_reason_codes") or ()),
            "selected_span_ids": (),
        }
    if rule.get("decision") == ScreeningDecision.DEFER.value:
        return {
            "decision": ScreeningDecision.DEFER.value,
            "route": ScreeningRoute.COST_DEFERRED.value,
            "rule_pass": True,
            "blocking_reason_codes": tuple(rule.get("blocking_reason_codes") or ()),
            "selected_span_ids": (),
        }
    force_review = bool(parse_errors)
    if not tasks and parse_errors:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.MODEL_TRIAGE_FAILED.value,
            "rule_pass": True,
            "blocking_reason_codes": parse_errors,
            "selected_span_ids": (),
        }
    if not tasks:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.MODEL_TRIAGE_FAILED.value,
            "rule_pass": True,
            "blocking_reason_codes": ("TASKS_EMPTY",),
            "selected_span_ids": (),
        }

    selected: list[str] = []
    selected_task_ids: list[str] = []
    selected_domains: list[str] = []
    all_success = True
    all_terminal_reject = True
    blocking: list[str] = []
    relations = relations or []
    success_ids = {
        str(task.get("task_id"))
        for task in tasks
        if task.get("outcome") == "SUCCESS" and task.get("is_actionable", True)
    }
    superseded: dict[str, set[str]] = {str(task.get("task_id")): set() for task in tasks}
    adjacency: dict[str, list[str]] = {}
    for relation in relations:
        if relation.get("type") not in {"continuation", "correction"}:
            continue
        source_id = str(relation.get("from_task_id"))
        target_id = str(relation.get("to_task_id"))
        adjacency.setdefault(source_id, []).append(target_id)
    for source_id in superseded:
        frontier = list(adjacency.get(source_id, ()))
        visited: set[str] = set()
        while frontier:
            target_id = frontier.pop()
            if target_id in visited:
                continue
            visited.add(target_id)
            if target_id in success_ids:
                superseded[source_id].add(target_id)
            frontier.extend(adjacency.get(target_id, ()))

    for index, task in enumerate(tasks):
        outcome = str(task.get("outcome") or "")
        if outcome != "SUCCESS":
            all_success = False
        rubric = task.get("rubric") if isinstance(task.get("rubric"), dict) else empty_rubric()
        domain_route = str(task.get("domain_route") or "")
        gate = _task_gate_result(
            outcome=outcome,
            needs_reconstruction=task.get("needs_reconstruction"),
            domain_route=domain_route,
            rubric=rubric,
            is_actionable=bool(task.get("is_actionable", True)),
            label_status=label_status,
        )
        task_errors = list(gate["blocking_reason_codes"])
        task_id = str(task.get("task_id") or f"task[{index}]")
        if superseded.get(task_id):
            task_errors.append("LATER_SAME_GOAL_SUCCESS")
            gate = {
                **gate,
                "decision": ScreeningDecision.REJECT.value,
                "route": ScreeningRoute.SUCCESS_NOT_RECONSTRUCTED.value,
            }
        if force_review:
            task_errors.extend(parse_errors)
            gate = {
                "decision": ScreeningDecision.REVIEW.value,
                "route": ScreeningRoute.MODEL_TRIAGE_FAILED.value,
                "blocking_reason_codes": tuple(dict.fromkeys(task_errors)),
            }
        task["reconstruction_eligible"] = (
            gate["decision"] == ScreeningDecision.ELIGIBLE.value and not force_review
        )
        task["eligibility"] = {
            "decision": gate["decision"],
            "route": gate["route"],
            "blocking_reason_codes": list(dict.fromkeys(task_errors)),
        }
        apply_task_tags(task)
        if gate["decision"] != ScreeningDecision.REJECT.value or any(
            code not in {"OUTCOME_SUCCESS", "LATER_SAME_GOAL_SUCCESS", "TASK_NOT_ACTIONABLE"}
            for code in task_errors
        ):
            all_terminal_reject = False
        span_ids = [
            str(item)
            for item in (task.get("span_ids") or ())
            if isinstance(item, str) and item
        ]
        if task["reconstruction_eligible"]:
            selected.extend(span_ids)
            selected_task_ids.append(str(task.get("task_id") or f"task[{index}]"))
            selected_domains.append(domain_route)
            continue
        for code in task_errors:
            blocking.append(f"task[{index}]:{code}")

    if force_review:
        return {
            "decision": ScreeningDecision.REVIEW.value,
            "route": ScreeningRoute.MODEL_TRIAGE_FAILED.value,
            "rule_pass": True,
            "blocking_reason_codes": parse_errors,
            "selected_span_ids": (),
        }

    if selected:
        route = (
            ScreeningRoute.ELIGIBLE_CODE_FILE.value
            if DomainRoute.CODE_FILE.value in selected_domains
            else ScreeningRoute.ELIGIBLE_TASK.value
        )
        return {
            "decision": ScreeningDecision.ELIGIBLE.value,
            "route": route,
            "rule_pass": True,
            "blocking_reason_codes": (),
            "selected_span_ids": tuple(dict.fromkeys(selected)),
            "selected_task_ids": tuple(dict.fromkeys(selected_task_ids)),
        }
    if all_success or all_terminal_reject:
        return {
            "decision": ScreeningDecision.REJECT.value,
            "route": ScreeningRoute.SUCCESS_NOT_RECONSTRUCTED.value,
            "rule_pass": True,
            "blocking_reason_codes": ("OUTCOME_SUCCESS",),
            "selected_span_ids": (),
        }
    return {
        "decision": ScreeningDecision.REVIEW.value,
        "route": ScreeningRoute.RUBRIC_REVIEW.value,
        "rule_pass": True,
        "blocking_reason_codes": tuple(blocking) or ("NO_ELIGIBLE_TASK",),
        "selected_span_ids": (),
    }
