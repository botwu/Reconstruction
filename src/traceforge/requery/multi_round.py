"""Terminal-Universe depth expansion：多轮用户代理与验证反馈。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Requirement:
    requirement_id: str
    text: str
    status: str = "ACTIVE"


@dataclass(slots=True)
class RequirementTracker:
    requirements: dict[str, Requirement] = field(default_factory=dict)

    def apply(self, updates: list[dict[str, Any]]) -> None:
        for item in updates:
            rid, text, status = item.get("id"), item.get("text"), item.get("status", "ACTIVE")
            if not isinstance(rid, str) or not rid or not isinstance(text, str) or not text:
                raise ValueError("需求更新必须包含 id/text")
            if status not in {"ACTIVE", "SATISFIED", "UPDATED", "REPLACED"}:
                raise ValueError("非法需求状态")
            self.requirements[rid] = Requirement(rid, text, status)

    def active(self) -> tuple[Requirement, ...]:
        return tuple(x for x in self.requirements.values() if x.status in {"ACTIVE", "UPDATED"})

    def to_dict(self) -> dict[str, Any]:
        # Requirement 使用 slots=True，不存在 __dict__；asdict 同时保证输出
        # 与论文中的私有 requirement ledger 保持稳定、可序列化。
        return {"requirements": [asdict(r) for r in self.requirements.values()]}


@dataclass(frozen=True, slots=True)
class RoundResult:
    round_index: int
    verifier_status: str
    user_feedback: str
    requirement_ids: tuple[str, ...]


def build_followup_prompt(tracker: RequirementTracker, result: RoundResult) -> str:
    if result.verifier_status not in {"PASS", "FAIL"}:
        raise ValueError("verifier_status 必须为 PASS/FAIL")
    reqs = "\n".join(f"- {r.requirement_id}: {r.text}" for r in tracker.active())
    feedback = result.user_feedback.strip() or (
        "请修复上一轮未通过的行为。"
        if result.verifier_status == "FAIL"
        else "请在现有实现上提出一个有依据的增量需求。"
    )
    return (
        f"保持 workspace 不变并继续当前任务。当前有效需求：\n{reqs}\n"
        f"验证结果：{result.verifier_status}\n用户反馈：{feedback}"
    )


def retain_verified_session(rounds: list[RoundResult], *, minimum_passes: int = 2) -> bool:
    if minimum_passes < 1:
        raise ValueError("minimum_passes 必须大于 0")
    if not rounds or any(r.round_index != i for i, r in enumerate(rounds)):
        return False
    # 与论文筛选条件一致：至少两个通过轮次；失败轮次可以保留用于恢复监督。
    return sum(r.verifier_status == "PASS" for r in rounds) >= minimum_passes
