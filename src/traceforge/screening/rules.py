"""确定性重建筛选规则。第一期不给出 ELIGIBLE。"""

from __future__ import annotations

from typing import Any

from .contracts import ScreeningDecision, ScreeningRoute

MAX_MESSAGES_FOR_AUTOMATIC_TRIAGE = 200
MAX_SOURCE_REQUESTS_FOR_AUTOMATIC_TRIAGE = 20


def decide_rule(features: dict[str, Any]) -> dict[str, Any]:
    """根据扫描特征给出 REJECT / DEFER / REVIEW。"""

    reasons: list[str] = []
    if not features.get("parse_ok"):
        reasons.extend(features.get("reason_hints") or ("PARSE_FAILED",))
        return _result(ScreeningDecision.REJECT, ScreeningRoute.RULE_HARD_REJECT, reasons, False)
    if not features.get("has_user_task_like_turn"):
        reasons.append("NO_USER_TASK")
        return _result(ScreeningDecision.REJECT, ScreeningRoute.RULE_HARD_REJECT, reasons, False)
    if not features.get("has_agent_attempt"):
        reasons.append("NO_AGENT_ATTEMPT")
        return _result(ScreeningDecision.REJECT, ScreeningRoute.RULE_HARD_REJECT, reasons, False)

    message_count = int(features.get("message_count") or 0)
    request_count = int(features.get("source_request_count") or 0)
    if (
        message_count > MAX_MESSAGES_FOR_AUTOMATIC_TRIAGE
        or request_count > MAX_SOURCE_REQUESTS_FOR_AUTOMATIC_TRIAGE
    ):
        reasons.append("ESTIMATED_COST_HIGH")
        return _result(ScreeningDecision.DEFER, ScreeningRoute.COST_DEFERRED, reasons, True)

    reasons.append("NEEDS_MODEL_TRIAGE")
    if features.get("has_failure_or_unfinished_signal"):
        reasons.extend(str(item) for item in features.get("reason_hints") or ())
    else:
        reasons.append("OUTCOME_UNKNOWN")
    return _result(ScreeningDecision.REVIEW, ScreeningRoute.NEEDS_MODEL_TRIAGE, reasons, True)


def _result(
    decision: ScreeningDecision,
    route: ScreeningRoute,
    reasons: list[str],
    rule_pass: bool,
) -> dict[str, Any]:
    return {
        "decision": decision.value,
        "route": route.value,
        "rule_pass": rule_pass,
        "blocking_reason_codes": tuple(reasons),
    }
