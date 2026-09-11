"""M4 Failure Analysis 的数据契约与稳定 ID。

本模块只描述模型分析结果及其证据边界，不执行模型调用、轨迹读取或业务判断。
所有正文都通过证据引用寻址；契约本身不携带原始回流文本。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from traceforge.trajectory.contracts import SerializableContract
from traceforge.trajectory.json_codec import stable_id

FAILURE_ANALYSIS_CONTRACT_VERSION = "failure-analysis-m4-v1"

EVIDENCE_REF_SCHEMA = "traceforge.failure-analysis-evidence-ref.v1"
INVARIANT_CHECK_SCHEMA = "traceforge.failure-analysis-invariant-check.v1"
FAILURE_ANALYSIS_REPORT_SCHEMA = "traceforge.failure-analysis-report.v1"
RECONSTRUCTABILITY_GATE_SCHEMA = "traceforge.reconstructability-gate.v1"

FAILURE_ANALYSIS_RUN_ID_NAMESPACE = "m4-failure-analysis-run-v1"
EVIDENCE_REF_ID_NAMESPACE = "m4-evidence-ref-v1"
FAILURE_REPORT_ID_NAMESPACE = "m4-failure-report-v1"
RECONSTRUCTABILITY_GATE_ID_NAMESPACE = "m4-reconstructability-gate-v1"


class EvidenceKind(StrEnum):
    """证据来源类型；原始正文只保留在上游受控 artifact。"""

    EVENT = "EVENT"
    QUERY_TURN = "QUERY_TURN"
    TOOL_RESULT = "TOOL_RESULT"
    TERMINAL = "TERMINAL"
    FILE_SNAPSHOT = "FILE_SNAPSHOT"
    ATTEMPT = "ATTEMPT"
    SESSION = "SESSION"


class EvidenceRole(StrEnum):
    """证据在 M4 结论中的作用。"""

    FAILURE = "FAILURE"
    TASK = "TASK"
    ENVIRONMENT = "ENVIRONMENT"
    OUTCOME = "OUTCOME"
    CONTEXT = "CONTEXT"


class InvariantKind(StrEnum):
    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"


class InvariantResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCLEAR = "UNCLEAR"
    NOT_RUN = "NOT_RUN"
    ERROR = "ERROR"


class FailureCategory(StrEnum):
    """与 AgentRx 对齐、并补充环境重建所需类别的封闭集合。"""

    TASK_INTENT_MISALIGNMENT = "TASK_INTENT_MISALIGNMENT"
    UNDERSPECIFIED_INTENT = "UNDERSPECIFIED_INTENT"
    INVALID_TOOL_INVOCATION = "INVALID_TOOL_INVOCATION"
    TOOL_OUTPUT_MISINTERPRETATION = "TOOL_OUTPUT_MISINTERPRETATION"
    PLAN_EXECUTION_FAILURE = "PLAN_EXECUTION_FAILURE"
    ENVIRONMENT_CONTEXT_GAP = "ENVIRONMENT_CONTEXT_GAP"
    STATE_MUTATION_FAILURE = "STATE_MUTATION_FAILURE"
    EXTERNAL_DEPENDENCY_FAILURE = "EXTERNAL_DEPENDENCY_FAILURE"
    VERIFIER_OR_TERMINATION_FAILURE = "VERIFIER_OR_TERMINATION_FAILURE"
    SYSTEM_FAILURE = "SYSTEM_FAILURE"
    USER_INTERRUPTION = "USER_INTERRUPTION"
    INCONCLUSIVE = "INCONCLUSIVE"


class FailureLayer(StrEnum):
    """第一版确定性层无法归因时必须使用 UNCLEAR。

    M1B 的结构完整性问题不能直接归因给 solver 或 verifier。
    """

    TASK = "TASK"
    ENVIRONMENT = "ENVIRONMENT"
    SOLVER = "SOLVER"
    VERIFIER = "VERIFIER"
    SYSTEM = "SYSTEM"
    USER = "USER"
    UNCLEAR = "UNCLEAR"


class FailureAttribution(StrEnum):
    TASK = "TASK"
    ENVIRONMENT = "ENVIRONMENT"
    SOLVER = "SOLVER"
    VERIFIER = "VERIFIER"
    SYSTEM = "SYSTEM"
    USER = "USER"
    UNKNOWN = "UNKNOWN"


class Recoverability(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NOT_RECOVERABLE = "NOT_RECOVERABLE"
    UNKNOWN = "UNKNOWN"


class GateDecision(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    REVIEW = "REVIEW"
    DEFER = "DEFER"
    REJECT = "REJECT"


class GateRoute(StrEnum):
    ELIGIBLE_CODE_FILE = "ELIGIBLE_CODE_FILE"
    ELIGIBLE_RETRIEVAL = "ELIGIBLE_RETRIEVAL"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class EvidenceRefV1(SerializableContract):
    """可审计证据引用，不复制原始正文。"""

    schema_version: str
    evidence_id: str
    evidence_kind: str
    source_id: str
    source_pointer: str | None
    role: str
    content_sha256: str | None


@dataclass(frozen=True, slots=True)
class InvariantCheckV1(SerializableContract):
    """一个静态或动态不变量的可复核结果。"""

    schema_version: str
    invariant_id: str
    kind: str
    check_code: str
    result: str
    trigger_event_id: str | None
    evidence_ref_ids: tuple[str, ...]
    taxonomy_targets: tuple[str, ...]
    error_code: str | None


@dataclass(frozen=True, slots=True)
class FailureAnalysisReportV1(SerializableContract):
    """单个失败 attempt 的根因定位与重建提示。"""

    schema_version: str
    report_id: str
    session_ref: str
    episode_ref: str
    attempt_ref: str
    capture_occurrence_id: str
    primary_failure: str
    failure_layer: str
    critical_event_ids: tuple[str, ...]
    critical_step: int | None
    evidence_ref_ids: tuple[str, ...]
    violated_invariant_ids: tuple[str, ...]
    causal_hypotheses: tuple[dict[str, Any], ...]
    recoverability: str
    attribution: dict[str, float]
    reconstruction_targets: tuple[str, ...]
    reconstruction_relevance: dict[str, str]
    open_questions: tuple[str, ...]
    confidence: float
    uncertainty_codes: tuple[str, ...]

    @property
    def task_episode_id(self) -> str:
        """兼容旧称；episode_ref 是唯一序列化字段。"""

        return self.episode_ref

    @property
    def target_attempt_id(self) -> str:
        """兼容旧称；attempt_ref 是唯一序列化字段。"""

        return self.attempt_ref

    @property
    def primary_step(self) -> int | None:
        """兼容旧称；critical_step 是唯一序列化字段。"""

        return self.critical_step


@dataclass(frozen=True, slots=True)
class ReconstructabilityGateV1(SerializableContract):
    """判断 attempt 是否值得进入任务/环境重建的门禁结果。

    八个维度均为 0--3 分，含义由 P0 rubric 冻结；estimated_cost 越高表示成本越高，
    不应被误解为质量分。
    """

    schema_version: str
    gate_id: str
    session_ref: str
    episode_ref: str
    attempt_ref: str
    decision: str
    route: str
    task_identifiability: int
    failure_evidence: int
    initial_environment_visibility: int
    environment_completion_value: int
    verifier_constructability: int
    episode_boundary_confidence: int
    privacy_processability: int
    estimated_cost: int
    evidence_ref_ids: tuple[str, ...]
    blocking_reason_codes: tuple[str, ...]
    confidence: float
    review_required: bool

    @property
    def task_episode_id(self) -> str:
        return self.episode_ref

    @property
    def target_attempt_id(self) -> str:
        return self.attempt_ref


def failure_analysis_run_id(*, m1b_run_id: str, m1d_run_id: str, input_manifest_sha256: str) -> str:
    """M4 运行身份绑定到上游结构产物及输入清单。"""

    return stable_id(
        FAILURE_ANALYSIS_RUN_ID_NAMESPACE,
        {
            "contract_version": FAILURE_ANALYSIS_CONTRACT_VERSION,
            "m1b_run_id": m1b_run_id,
            "m1d_run_id": m1d_run_id,
            "input_manifest_sha256": input_manifest_sha256,
        },
    )


def failure_analysis_report_id(*, m4_run_id: str, task_episode_id: str, target_attempt_id: str) -> str:
    """报告身份绑定到 M4 run 和目标 attempt，重复运行可区分。"""

    return stable_id(
        FAILURE_REPORT_ID_NAMESPACE,
        {
            "contract_version": FAILURE_ANALYSIS_CONTRACT_VERSION,
            "m4_run_id": m4_run_id,
            "task_episode_id": task_episode_id,
            "target_attempt_id": target_attempt_id,
        },
    )


def evidence_ref_id(
    *,
    m4_run_id: str,
    evidence_kind: EvidenceKind | str,
    source_id: str,
    source_pointer: str | None,
) -> str:
    """证据引用的稳定身份；pointer 允许同一事件有多个字段证据。"""

    return stable_id(
        EVIDENCE_REF_ID_NAMESPACE,
        {
            "contract_version": FAILURE_ANALYSIS_CONTRACT_VERSION,
            "m4_run_id": m4_run_id,
            "evidence_kind": EvidenceKind(evidence_kind).value,
            "source_id": source_id,
            "source_pointer": source_pointer,
        },
    )


def reconstructability_gate_id(
    *, m4_run_id: str, task_episode_id: str, target_attempt_id: str
) -> str:
    return stable_id(
        RECONSTRUCTABILITY_GATE_ID_NAMESPACE,
        {
            "contract_version": FAILURE_ANALYSIS_CONTRACT_VERSION,
            "m4_run_id": m4_run_id,
            "task_episode_id": task_episode_id,
            "target_attempt_id": target_attempt_id,
        },
    )


def _check_score(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
        raise ValueError(f"{name} 必须是 0 到 3 的整数")


def validate_reconstructability_gate(gate: ReconstructabilityGateV1) -> None:
    """校验门禁的闭合枚举、评分范围和置信度，不做业务阈值判定。"""

    GateDecision(gate.decision)
    GateRoute(gate.route)
    for name in (
        "task_identifiability",
        "failure_evidence",
        "initial_environment_visibility",
        "environment_completion_value",
        "verifier_constructability",
        "episode_boundary_confidence",
        "privacy_processability",
        "estimated_cost",
    ):
        _check_score(name, getattr(gate, name))
    if not isinstance(gate.review_required, bool):
        raise ValueError("review_required 必须是布尔值")
    if not isinstance(gate.confidence, (int, float)) or isinstance(gate.confidence, bool):
        raise ValueError("confidence 必须是数值")
    if not math.isfinite(float(gate.confidence)) or not 0.0 <= float(gate.confidence) <= 1.0:
        raise ValueError("confidence 必须位于 [0, 1]")


def validate_failure_analysis_report(report: FailureAnalysisReportV1) -> None:
    """校验报告中可被确定性层约束的部分；不替代模型裁决。"""

    FailureCategory(report.primary_failure)
    FailureLayer(report.failure_layer)
    Recoverability(report.recoverability)
    for key, value in report.attribution.items():
        FailureAttribution(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ValueError(f"attribution[{key}] 必须位于 [0, 1]")
    if not isinstance(report.confidence, (int, float)) or isinstance(report.confidence, bool):
        raise ValueError("confidence 必须是数值")
    if not math.isfinite(float(report.confidence)) or not 0.0 <= float(report.confidence) <= 1.0:
        raise ValueError("confidence 必须位于 [0, 1]")
    if report.primary_step is not None and (
        isinstance(report.primary_step, bool)
        or not isinstance(report.primary_step, int)
        or report.primary_step < 0
    ):
        raise ValueError("primary_step 必须为非负整数或 None")


def validate_invariant_check(check: InvariantCheckV1) -> None:
    """校验不变量结果；UNCLEAR 是有效结果，不应被强制转换为 FAIL。"""

    InvariantKind(check.kind)
    InvariantResult(check.result)
    if check.result == InvariantResult.ERROR and not check.error_code:
        raise ValueError("ERROR 结果必须提供 error_code")
    if check.result != InvariantResult.ERROR and check.error_code is not None:
        raise ValueError("非 ERROR 结果不应携带 error_code")


__all__ = [
    "EVIDENCE_REF_SCHEMA",
    "FAILURE_ANALYSIS_CONTRACT_VERSION",
    "FAILURE_ANALYSIS_RUN_ID_NAMESPACE",
    "FAILURE_ANALYSIS_REPORT_SCHEMA",
    "INVARIANT_CHECK_SCHEMA",
    "RECONSTRUCTABILITY_GATE_SCHEMA",
    "EvidenceKind",
    "EvidenceRole",
    "InvariantKind",
    "InvariantResult",
    "FailureCategory",
    "FailureLayer",
    "FailureAttribution",
    "Recoverability",
    "GateDecision",
    "GateRoute",
    "EvidenceRefV1",
    "InvariantCheckV1",
    "FailureAnalysisReportV1",
    "ReconstructabilityGateV1",
    "failure_analysis_run_id",
    "analysis_run_id",
    "evidence_ref_id",
    "failure_analysis_report_id",
    "reconstructability_gate_id",
    "validate_invariant_check",
    "validate_failure_analysis_report",
    "validate_reconstructability_gate",
]

def analysis_run_id(*, m1b_run_id: str, m1b_manifest_sha256: str) -> str:
    """兼容 M4 pipeline 的上游 M1B 运行身份公式。"""

    return stable_id(
        FAILURE_ANALYSIS_RUN_ID_NAMESPACE,
        {
            "contract_version": FAILURE_ANALYSIS_CONTRACT_VERSION,
            "m1b_run_id": m1b_run_id,
            "m1b_manifest_sha256": m1b_manifest_sha256,
        },
    )
