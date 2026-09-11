"""Verifier 的 RED-check：在独立执行器返回结果后检查验证器是否可信。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RedCheckCase:
    """一条 verifier 对照试验结果；执行由 Harbor/AGS 负责。"""

    case_id: str
    kind: str
    status: str
    reward: float | None
    expected_status: str
    expected_reward: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RedCheckReport:
    """RED-check 的可审计结果。"""

    passed: bool
    checked_count: int
    failed_case_ids: tuple[str, ...]
    metrics: dict[str, Any]


def evaluate_red_check(cases: tuple[RedCheckCase, ...]) -> RedCheckReport:
    """验证 no-op、oracle、mutation 等对照是否符合预期。"""
    failed: list[str] = []
    for case in cases:
        if case.status != case.expected_status or case.reward != case.expected_reward:
            failed.append(case.case_id)
    kinds = {case.kind for case in cases}
    required = {"oracle_pass", "nop_fail", "mutation_fail"}
    missing = sorted(required - kinds)
    if missing:
        failed.extend(f"missing:{kind}" for kind in missing)
    return RedCheckReport(
        passed=not failed and bool(cases),
        checked_count=len(cases),
        failed_case_ids=tuple(failed),
        metrics={
            "metric_version": "traceforge.verifier-red-check.v1",
            "case_count": len(cases),
            "passed_case_count": len(cases)
            - len([x for x in failed if not x.startswith("missing:")]),
            "missing_required_cases": missing,
            "red_check_pass": not failed and bool(cases),
        },
    )


__all__ = ["RedCheckCase", "RedCheckReport", "evaluate_red_check"]
