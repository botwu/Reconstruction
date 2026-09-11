"""重建控制平面的版本化数据契约。

这里不绑定具体模型或 Harbor 实现，只描述阶段、预算、门禁和不可变
产物引用，使 workflow 可以在任意执行器上获得相同的控制语义。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from traceforge.trajectory.contracts import SerializableContract

CONTROL_PLANE_SCHEMA = "traceforge.control-plane.v1"
POLICY_SCHEMA = "traceforge.control-policy.v1"
GATE_DECISION_SCHEMA = "traceforge.gate-decision.v1"
ARTIFACT_REF_SCHEMA = "traceforge.immutable-artifact-ref.v1"


class Stage(StrEnum):
    """重建闭环中的有序阶段。"""

    INGESTION = "INGESTION"
    FAILURE_ANALYSIS = "FAILURE_ANALYSIS"
    TASK_RECOVERY = "TASK_RECOVERY"
    ENVIRONMENT_COMPLETION = "ENVIRONMENT_COMPLETION"
    SUFFICIENCY = "SUFFICIENCY"
    VERIFIER = "VERIFIER"
    BUNDLE = "BUNDLE"
    ROLLOUT = "ROLLOUT"
    RED_CHECK = "RED_CHECK"
    CURATION = "CURATION"


class GateStatus(StrEnum):
    """阶段门禁结果。"""

    PASS = "PASS"
    REVIEW = "REVIEW"
    RETRY = "RETRY"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class RunStatus(StrEnum):
    """控制平面运行状态。"""

    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class StageBudgetV1(SerializableContract):
    """单阶段资源和生成约束。"""

    max_attempts: int = 1
    timeout_seconds: int = 900
    max_candidates: int = 1
    temperature: float = 0.0
    require_evidence: bool = True
    require_human_review: bool = False
    retryable_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.timeout_seconds < 1 or self.max_candidates < 1:
            raise ValueError("阶段预算必须为正数")
        if isinstance(self.temperature, bool) or not math.isfinite(self.temperature):
            raise ValueError("temperature 必须是有限数")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature 必须在 [0,2]")


@dataclass(frozen=True, slots=True)
class ControlPlanePolicyV1(SerializableContract):
    """可审计、可复现的控制策略；修改策略必须提升 policy_id/version。"""

    schema_version: str = POLICY_SCHEMA
    policy_id: str = "reconstruction-default-v1"
    stages: tuple[str, ...] = tuple(stage.value for stage in Stage)
    budgets: dict[str, StageBudgetV1] = field(default_factory=dict)
    min_evidence_refs: int = 1
    max_total_retries: int = 2
    immutable_artifacts: bool = True
    require_human_review_for_uncertain: bool = True
    max_session_seconds: int = 7200

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_SCHEMA:
            raise ValueError(f"不支持的 policy schema: {self.schema_version}")
        if self.min_evidence_refs < 0 or self.max_total_retries < 0 or self.max_session_seconds < 1:
            raise ValueError("全局策略预算非法")
        expected = tuple(stage.value for stage in Stage)
        if self.stages != expected:
            raise ValueError("stages 必须严格匹配控制平面的固定顺序")
        unknown = set(self.budgets).difference(self.stages)
        if unknown:
            raise ValueError(f"存在未知阶段预算: {sorted(unknown)}")


@dataclass(frozen=True, slots=True)
class ImmutableArtifactRefV1(SerializableContract):
    """阶段输出的不可变引用；下游只接受 sealed 且有 SHA256 的产物。"""

    schema_version: str
    artifact_id: str
    path: str
    sha256: str
    size_bytes: int
    sealed: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != ARTIFACT_REF_SCHEMA:
            raise ValueError("artifact schema 不匹配")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("sha256 必须是 64 位小写十六进制")
        if self.size_bytes < 0 or not self.path or not self.sealed:
            raise ValueError("产物必须有路径、非负大小并已 sealed")


@dataclass(frozen=True, slots=True)
class GateDecisionV1(SerializableContract):
    """一次阶段门禁的不可变记录。"""

    schema_version: str
    run_id: str
    stage: str
    status: str
    attempt: int
    reasons: tuple[str, ...]
    artifact_id: str | None = None
    next_stage: str | None = None
    retryable: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != GATE_DECISION_SCHEMA:
            raise ValueError("gate schema 不匹配")
        if self.stage not in {stage.value for stage in Stage}:
            raise ValueError(f"未知阶段: {self.stage}")
        if self.status not in {status.value for status in GateStatus}:
            raise ValueError(f"未知门禁状态: {self.status}")
        if self.attempt < 1:
            raise ValueError("attempt 必须从 1 开始")


@dataclass(frozen=True, slots=True)
class ControlPlaneStateV1(SerializableContract):
    """可持久化的控制平面状态快照。"""

    schema_version: str
    run_id: str
    policy_id: str
    current_stage: str
    status: str = RunStatus.RUNNING.value
    total_retries: int = 0
    decisions: tuple[GateDecisionV1, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != CONTROL_PLANE_SCHEMA:
            raise ValueError("control plane schema 不匹配")
        if self.current_stage not in {stage.value for stage in Stage}:
            raise ValueError(f"未知 current_stage: {self.current_stage}")
        if self.status not in {status.value for status in RunStatus}:
            raise ValueError(f"未知 run status: {self.status}")
        if self.total_retries < 0:
            raise ValueError("total_retries 不能为负")
