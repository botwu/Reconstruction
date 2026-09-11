"""判断任务与初始环境是否足以启动独立执行。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from traceforge.trajectory.json_codec import stable_id

SCHEMA = "traceforge.workspace-sufficiency.v1"
_ALLOWED = frozenset({"READY", "REVIEW", "BLOCKED"})


class SufficiencyInputError(ValueError):
    """Judge 输入不满足严格契约。"""


@dataclass(frozen=True, slots=True)
class SufficiencyDecision:
    schema_version: str
    decision_id: str
    task_sufficient: bool
    environment_sufficient: bool
    decision: str
    blocking_items: tuple[str, ...]
    reason_codes: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "task_sufficient": self.task_sufficient,
            "environment_sufficient": self.environment_sufficient,
            "decision": self.decision,
            "blocking_items": list(self.blocking_items),
            "reason_codes": list(self.reason_codes),
            "confidence": self.confidence,
        }


def judge(
    *,
    candidate_id: str,
    task_sufficient: bool,
    environment_sufficient: bool,
    decision: str,
    blocking_items: list[str] | tuple[str, ...] = (),
    reason_codes: list[str] | tuple[str, ...] = (),
    confidence: float = 0.0,
) -> SufficiencyDecision:
    """消费模型或规则层的结构化判断，不自行补全缺失字段。"""
    if not candidate_id:
        raise SufficiencyInputError("candidate_id 不能为空")
    if decision not in _ALLOWED:
        raise SufficiencyInputError(f"decision 必须属于 {sorted(_ALLOWED)}")
    if not isinstance(task_sufficient, bool) or not isinstance(environment_sufficient, bool):
        raise SufficiencyInputError("sufficiency 必须是 bool")
    if not 0.0 <= confidence <= 1.0:
        raise SufficiencyInputError("confidence 必须在 [0,1]")
    blocks = tuple(item for item in blocking_items if isinstance(item, str) and item)
    reasons = tuple(item for item in reason_codes if isinstance(item, str) and item)
    if decision == "READY" and (not task_sufficient or not environment_sufficient or blocks):
        raise SufficiencyInputError("READY 必须同时满足 task/environment 且无 blocking_items")
    if decision == "BLOCKED" and not blocks:
        raise SufficiencyInputError("BLOCKED 必须提供 blocking_items")
    return SufficiencyDecision(
        SCHEMA,
        stable_id(
            "traceforge.workspace-sufficiency-v1",
            {
                "candidate_id": candidate_id,
                "task": task_sufficient,
                "environment": environment_sufficient,
                "decision": decision,
                "blocking_items": blocks,
            },
        ),
        task_sufficient,
        environment_sufficient,
        decision,
        blocks,
        reasons,
        confidence,
    )


def validate_payload(candidate_id: str, payload: dict[str, Any]) -> SufficiencyDecision:
    """严格校验注入的模型 JSON；未知字段允许保留在上游审计层。"""
    required = {
        "task_sufficient",
        "environment_sufficient",
        "decision",
        "blocking_items",
        "reason_codes",
        "confidence",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise SufficiencyInputError(f"缺少字段：{','.join(missing)}")
    if not isinstance(payload["blocking_items"], list) or not isinstance(
        payload["reason_codes"], list
    ):
        raise SufficiencyInputError("blocking_items/reason_codes 必须是数组")
    return judge(
        candidate_id=candidate_id,
        task_sufficient=payload["task_sufficient"],
        environment_sufficient=payload["environment_sufficient"],
        decision=payload["decision"],
        blocking_items=payload["blocking_items"],
        reason_codes=payload["reason_codes"],
        confidence=payload["confidence"],
    )
