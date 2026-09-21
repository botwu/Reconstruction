"""确定性重建筛选规则。第一期不给出 ELIGIBLE。"""

from __future__ import annotations

from typing import Any

from .contracts import ScreeningDecision, ScreeningRoute


def decide_rule(features: dict[str, Any]) -> dict[str, Any]:
    """第一层只挡垃圾，不因轨迹长短暂缓。

    扔掉：坏 JSON、没有用户任务、助手没动手。
    其余一律交给模型细筛，从中挑没做好、没做完的事。
    """

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
