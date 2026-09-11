"""重建流程的可控执行内核。

ControlPlane 将阶段顺序、预算、证据门禁、重试、人审和不可变产物统一为
纯确定性决策。模型、Harbor 和文件执行器只需提供 observation 字典
即可接入。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .control_plane_contracts import (
    ARTIFACT_REF_SCHEMA,
    CONTROL_PLANE_SCHEMA,
    GATE_DECISION_SCHEMA,
    ControlPlanePolicyV1,
    ControlPlaneStateV1,
    GateDecisionV1,
    GateStatus,
    ImmutableArtifactRefV1,
    RunStatus,
    Stage,
    StageBudgetV1,
)


def default_policy() -> ControlPlanePolicyV1:
    """返回安全默认策略；环境候选最多 5 个，rollout 默认需要人审。"""

    budgets = {
        stage.value: StageBudgetV1(
            max_attempts=2
            if stage in {Stage.FAILURE_ANALYSIS, Stage.TASK_RECOVERY, Stage.ENVIRONMENT_COMPLETION}
            else 1,
            max_candidates=5 if stage is Stage.ENVIRONMENT_COMPLETION else 1,
            temperature=0.2
            if stage in {Stage.FAILURE_ANALYSIS, Stage.TASK_RECOVERY, Stage.ENVIRONMENT_COMPLETION}
            else 0.0,
            require_evidence=stage not in {Stage.ROLLOUT, Stage.RED_CHECK},
            require_human_review=stage is Stage.CURATION,
        )
        for stage in Stage
    }
    return ControlPlanePolicyV1(budgets=budgets)


def seal_artifact(path: str | Path, *, artifact_id: str | None = None) -> ImmutableArtifactRefV1:
    """读取产物并生成不可变引用；不修改文件。"""

    target = Path(path)
    data = target.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return ImmutableArtifactRefV1(
        schema_version=ARTIFACT_REF_SCHEMA,
        artifact_id=artifact_id or digest[:16],
        path=str(target),
        sha256=digest,
        size_bytes=len(data),
    )


def verify_artifact(ref: ImmutableArtifactRefV1) -> bool:
    """验证 sealed 产物仍与登记摘要一致。"""

    try:
        target = Path(ref.path)
        return target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == ref.sha256
    except OSError:
        return False


def _stage_budget(policy: ControlPlanePolicyV1, stage: Stage) -> StageBudgetV1:
    return policy.budgets.get(stage.value, StageBudgetV1())


def evaluate_gate(
    *,
    run_id: str,
    stage: Stage,
    attempt: int,
    observation: dict[str, Any],
    policy: ControlPlanePolicyV1,
    artifact: ImmutableArtifactRefV1 | None = None,
) -> GateDecisionV1:
    """依据 observation 计算确定性的阶段门禁。"""

    budget = _stage_budget(policy, stage)
    reasons: list[str] = []
    retryable = False
    status = GateStatus.PASS
    error_code = observation.get("error_code")
    duration = observation.get("duration_seconds")
    if isinstance(duration, (int, float)) and duration > budget.timeout_seconds:
        error_code = "TIMEOUT"
        observation = {**observation, "retryable": True}
    candidate_count = observation.get("candidate_count", 1)
    evidence_count = observation.get("evidence_count", 0)
    try:
        candidate_count = int(candidate_count)
        evidence_count = int(evidence_count)
    except (TypeError, ValueError):
        candidate_count, evidence_count = -1, -1
    if (
        artifact is None
        and policy.immutable_artifacts
        and observation.get("artifact_required", False)
    ):
        status, reasons = GateStatus.BLOCKED, ["IMMUTABLE_ARTIFACT_REQUIRED"]
    elif artifact is not None and (not artifact.sealed or not verify_artifact(artifact)):
        status, reasons = GateStatus.BLOCKED, ["ARTIFACT_HASH_MISMATCH"]
    elif isinstance(error_code, str) and error_code:
        retryable = error_code in budget.retryable_errors or bool(observation.get("retryable"))
        if retryable and attempt < budget.max_attempts:
            status, reasons = GateStatus.RETRY, [f"RETRYABLE_ERROR:{error_code}"]
        else:
            status, reasons = GateStatus.FAIL, [f"STAGE_ERROR:{error_code}"]
    elif budget.max_candidates < int(observation.get("candidate_count", 1)):
        status, reasons = GateStatus.REVIEW, ["CANDIDATE_LIMIT_EXCEEDED"]
    elif (
        budget.require_evidence
        and int(observation.get("evidence_count", 0)) < policy.min_evidence_refs
    ):
        status, reasons = GateStatus.BLOCKED, ["INSUFFICIENT_EVIDENCE"]
    elif observation.get("uncertain") and policy.require_human_review_for_uncertain:
        status, reasons = GateStatus.REVIEW, ["UNCERTAIN_REQUIRES_HUMAN_REVIEW"]
    elif budget.require_human_review and observation.get("human_reviewed") is not True:
        status, reasons = GateStatus.REVIEW, ["HUMAN_REVIEW_REQUIRED"]
    elif observation.get("quality_gate") is False:
        status, reasons = GateStatus.FAIL, ["QUALITY_GATE_FAILED"]
    elif observation.get("status") in {"FAIL", "REJECT", "BLOCKED"}:
        status, reasons = GateStatus.FAIL, [f"UPSTREAM_STATUS:{observation['status']}"]
    else:
        reasons = ["ALL_GATES_PASSED"]
    next_stage = None
    if status is GateStatus.PASS:
        values = [item.value for item in Stage]
        index = values.index(stage.value)
        next_stage = values[index + 1] if index + 1 < len(values) else None
    return GateDecisionV1(
        schema_version=GATE_DECISION_SCHEMA,
        run_id=run_id,
        stage=stage.value,
        status=status.value,
        attempt=attempt,
        reasons=tuple(reasons),
        artifact_id=artifact.artifact_id if artifact else None,
        next_stage=next_stage,
        retryable=retryable,
        metrics={
            "candidate_count": observation.get("candidate_count", 0),
            "evidence_count": observation.get("evidence_count", 0),
        },
    )


class ControlPlane:
    """维护单次运行状态；每次 gate 都产生不可变 decision。"""

    def __init__(self, run_id: str, policy: ControlPlanePolicyV1 | None = None) -> None:
        self.policy = policy or default_policy()
        self.state = ControlPlaneStateV1(
            schema_version=CONTROL_PLANE_SCHEMA,
            run_id=run_id,
            policy_id=self.policy.policy_id,
            current_stage=Stage.INGESTION.value,
        )

    def gate(
        self,
        stage: Stage,
        observation: dict[str, Any],
        *,
        attempt: int = 1,
        artifact: ImmutableArtifactRefV1 | None = None,
    ) -> GateDecisionV1:
        """检查阶段是否为当前阶段并推进状态。"""

        if self.state.status in {RunStatus.COMPLETE.value, RunStatus.BLOCKED.value}:
            raise RuntimeError("控制平面已终止")
        if stage.value != self.state.current_stage:
            raise ValueError(
                "阶段顺序错误: 期望 %s, 收到 %s" % (self.state.current_stage, stage.value)
            )
        if (
            self.state.status == RunStatus.REVIEW.value
            and observation.get("review_resolution") != "APPROVED"
        ):
            observation = {**observation, "uncertain": True}
        decision = evaluate_gate(
            run_id=self.state.run_id,
            stage=stage,
            attempt=attempt,
            observation=observation,
            policy=self.policy,
            artifact=artifact,
        )
        status = self.state.status
        total_retries = self.state.total_retries + (
            1 if decision.status == GateStatus.RETRY.value else 0
        )
        if total_retries > self.policy.max_total_retries:
            decision = GateDecisionV1(
                schema_version=GATE_DECISION_SCHEMA,
                run_id=decision.run_id,
                stage=decision.stage,
                status=GateStatus.BLOCKED.value,
                attempt=decision.attempt,
                reasons=("GLOBAL_RETRY_BUDGET_EXCEEDED",),
                artifact_id=decision.artifact_id,
                retryable=False,
                metrics=decision.metrics,
            )
        elif decision.status == GateStatus.PASS.value:
            if decision.next_stage is None:
                status = RunStatus.COMPLETE.value
            else:
                self.state = ControlPlaneStateV1(
                    schema_version=CONTROL_PLANE_SCHEMA,
                    run_id=self.state.run_id,
                    policy_id=self.state.policy_id,
                    current_stage=decision.next_stage,
                    status=RunStatus.RUNNING.value,
                    total_retries=total_retries,
                    decisions=(*self.state.decisions, decision),
                )
                return decision
        elif decision.status == GateStatus.REVIEW.value:
            status = RunStatus.REVIEW.value
        elif decision.status in {GateStatus.FAIL.value, GateStatus.BLOCKED.value}:
            status = RunStatus.BLOCKED.value
        self.state = ControlPlaneStateV1(
            schema_version=CONTROL_PLANE_SCHEMA,
            run_id=self.state.run_id,
            policy_id=self.state.policy_id,
            current_stage=self.state.current_stage,
            status=status,
            total_retries=total_retries,
            decisions=(*self.state.decisions, decision),
        )
        return decision


__all__ = [
    "ControlPlane",
    "ControlPlanePolicyV1",
    "GateDecisionV1",
    "GateStatus",
    "ImmutableArtifactRefV1",
    "Stage",
    "StageBudgetV1",
    "default_policy",
    "evaluate_gate",
    "seal_artifact",
    "verify_artifact",
]
