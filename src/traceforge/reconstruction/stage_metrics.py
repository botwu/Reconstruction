"""按任务统计重建阶段漏斗；阶段未运行与执行失败分别计数。"""

from __future__ import annotations

from collections import Counter
from typing import Any

STAGE_METRICS_SCHEMA = "traceforge.reconstruction-stage-metrics.v1"


def reconstruction_stage_metrics(
    source: dict[str, Any], intent: dict[str, Any], results: list[dict[str, Any]]
) -> dict[str, Any]:
    """每项原因在同一任务中只计一次，候选数与任务通过数不得混用。"""

    intent_by_id = {
        str((item.get("task") or {}).get("task_id") or item.get("task_id")): item
        for item in intent.get("tasks") or []
    }
    stages: dict[str, Counter[str]] = {
        name: Counter()
        for name in ("intent", "completion", "sufficiency", "task_fit", "verification")
    }
    routes: Counter[str] = Counter()
    stops: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for result in results:
        support = result.get("execution_support_route") or {}
        routes[support.get("route", "UNKNOWN")] += 1
        stops[result.get("stopped_at") or "COMPLETE"] += 1
        stages["intent"][intent_by_id.get(result["task_id"], {}).get("status", "NOT_RUN")] += 1
        stages["completion"][(result.get("completion") or {}).get("status", "NOT_RUN")] += 1
        sufficiency = result.get("sufficiency") or {}
        sufficiency_status = sufficiency.get("decision") or sufficiency.get("status")
        if sufficiency_status is None and "sufficiency_audit" in result:
            sufficiency_status = "REVIEW"
        stages["sufficiency"][sufficiency_status or "NOT_RUN"] += 1
        fit = result.get("task_fit") or {}
        stages["task_fit"][fit.get("decision") or "NOT_RUN"] += 1
        verification_status = (result.get("verification") or {}).get("status")
        if verification_status is None and result.get("status") == "PENDING_EXECUTION":
            verification_status = "PENDING_EXECUTION"
        stages["verification"][verification_status or "NOT_RUN"] += 1
        reasons.update(set(result.get("errors") or []) | set(support.get("reason_codes") or []))
    metrics: dict[str, Any] = {
        "schema_version": STAGE_METRICS_SCHEMA,
        "screening_count": len(source.get("selected_task_ids") or []),
        "task_count": len(results),
        "support_routed_count": sum(
            (item.get("execution_support_route") or {}).get("allow_completion") is True
            for item in results
        ),
        "support_route_counts": dict(sorted(routes.items())),
        "stopped_at_counts": dict(sorted(stops.items())),
        "rejection_reason_counts": dict(sorted(reasons.items())),
        # 这是验证器对 rollout 的许可，最终 SFT 数量以 curation.json 为准。
        "rollout_eligible_count": sum(
            (item.get("verification") or {}).get("sft_eligible") is True for item in results
        ),
    }
    for name, counts in stages.items():
        metrics[f"{name}_status_counts"] = dict(sorted(counts.items()))
        metrics[f"{name}_ready_count"] = counts["READY"] + counts["READY_VARIANT"]
        metrics[f"{name}_ready_variant_count"] = counts["READY_VARIANT"]
        metrics[f"{name}_review_count"] = counts["REVIEW"]
    return metrics
