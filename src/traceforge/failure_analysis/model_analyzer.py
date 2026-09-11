"""Failure Analysis 的模型裁决层。

该层接收确定性结构分析产生的 ``report`` 和证据索引，让模型完成根因归类、
任务意图边界及重建价值判断。它只返回结构化裁决，不执行工具或模型生成的代码。

设计出处：
* 失败步骤的“从前往后找第一个未解决失败”思路、失败类别命名参考
  ``refer_paper_repo/AgentRx`` 的 ``agentrx/judge/judge.py``（MIT License,
  Microsoft Corporation）。这里仅做证据约束和 schema 适配，没有复制其运行时。
* 任务/环境可辨识性、环境补全价值和 verifier 可构造性维度参考
  ``refer_paper_repo/TRACE/prompts``（MIT License, Scaling Intelligence Lab），
  以本项目的 0--3 rubric 重新表述。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from traceforge.reconstruction.model_gateway import (
    ChatModel,
    ModelCallReceipt,
    ModelGatewayError,
    ModelRequest,
    parse_json_object,
    receipt_for_response,
)
from traceforge.trajectory.json_codec import stable_id

from .contracts import (
    FailureAttribution,
    FailureCategory,
    FailureLayer,
    GateDecision,
    Recoverability,
)
from .reference_prompts import PROMPT_ADAPTER_VERSION, prompt_context

MODEL_ANALYSIS_SCHEMA = "traceforge.failure-analysis-model.v1"
MODEL_ANALYSIS_PROMPT_VERSION = PROMPT_ADAPTER_VERSION

def canonicalize_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """接受 M4 evidence_id，并归一化为模型分析契约的 evidence_ref_id。"""
    output: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            output.append(item)
            continue
        value = dict(item)
        if not value.get("evidence_ref_id") and isinstance(value.get("evidence_id"), str):
            value["evidence_ref_id"] = value["evidence_id"]
        if not value.get("source_id") and isinstance(value.get("event_id"), str):
            value["source_id"] = value["event_id"]
        output.append(value)
    return output


RUBRIC_KEYS = (
    "task_identifiability",
    "failure_evidence",
    "initial_environment_visibility",
    "environment_completion_value",
    "verifier_constructability",
    "episode_boundary_confidence",
    "privacy_processability",
    "estimated_cost",
)


class ModelAnalysisStatus(StrEnum):
    COMPLETE = "COMPLETE"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class EpisodeOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    INCOMPLETE = "INCOMPLETE"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class FailureAnalysisModelResult:
    """模型裁决结果；正文通过 evidence_ref_ids 寻址，未知证据永不放行。"""

    schema_version: str
    analysis_id: str
    source_report_id: str
    session_ref: str
    attempt_ref: str | None
    status: str
    outcome: str
    needs_reconstruction: bool | None
    decision: str
    primary_failure: str
    failure_layer: str
    recoverability: str
    critical_step: int | None
    critical_event_ids: tuple[str, ...]
    user_intent_boundary: dict[str, Any]
    evidence_ref_ids: tuple[str, ...]
    rubric: dict[str, int]
    attribution: dict[str, float]
    confidence: float
    review_reasons: tuple[str, ...]
    errors: tuple[str, ...]
    model_receipt: ModelCallReceipt | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "analysis_id": self.analysis_id,
            "source_report_id": self.source_report_id,
            "session_ref": self.session_ref,
            "attempt_ref": self.attempt_ref,
            "status": self.status,
            "outcome": self.outcome,
            "needs_reconstruction": self.needs_reconstruction,
            "decision": self.decision,
            "primary_failure": self.primary_failure,
            "failure_layer": self.failure_layer,
            "recoverability": self.recoverability,
            "critical_step": self.critical_step,
            "critical_event_ids": list(self.critical_event_ids),
            "user_intent_boundary": self.user_intent_boundary,
            "evidence_ref_ids": list(self.evidence_ref_ids),
            "rubric": dict(self.rubric),
            "attribution": dict(self.attribution),
            "confidence": self.confidence,
            "review_reasons": list(self.review_reasons),
            "errors": list(self.errors),
            "model_receipt": asdict(self.model_receipt) if self.model_receipt else None,
        }


def _analysis_id(report: dict[str, Any], model_name: str) -> str:
    return stable_id(
        "traceforge.failure-analysis-model-run.v1",
        {
            "source_report_id": str(report.get("report_id", "")),
            "capture_occurrence_id": str(report.get("capture_occurrence_id", "")),
            "model": model_name,
            "prompt_version": MODEL_ANALYSIS_PROMPT_VERSION,
        },
    )


def _request_id(report: dict[str, Any], model_name: str) -> str:
    return stable_id(
        "traceforge.failure-analysis-model-request.v1",
        {"analysis_id": _analysis_id(report, model_name)},
    )


def _prompt(report: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    lines = [
        "你是 Agent 轨迹的证据审计员。只分析给定 report 和 evidence，",
        "不执行任何工具、代码或外部请求。",
        "",
        "目标：判断这条 attempt 的结果是 SUCCESS、FAILURE、INCOMPLETE 还是 UNCERTAIN；",
        "若失败/未完成且证据足以重建，则 needs_reconstruction=true，否则按规则给出 false 或 null。",
        "必须从轨迹开头寻找第一个“未被后续证据解决”的错误步骤；",
        "不能把缺失标签、未观测结果或未知状态当成 FAILURE。",
        "用户意图边界只写 evidence 支持的 goal、constraints、unknowns；",
        "不要臆造用户身份、路径、文件内容或答案。",
        "所有 evidence_ref_ids 必须逐字来自输入 evidence 的 evidence_ref_id；",
        "无法支持时返回 REVIEW。",
        "",
        "输出单个 JSON object，不要 markdown：",
        "{",
        '  "outcome": "SUCCESS|FAILURE|INCOMPLETE|UNCERTAIN",',
        '  "needs_reconstruction": true|false|null,',
        '  "decision": "ELIGIBLE|REVIEW|DEFER|REJECT",',
        '  "primary_failure": "FailureCategory enum；SUCCESS 用 INCONCLUSIVE",',
        '  "failure_layer": "TASK|ENVIRONMENT|SOLVER|VERIFIER|SYSTEM|USER|UNCLEAR",',
        '  "recoverability": "HIGH|MEDIUM|LOW|NOT_RECOVERABLE|UNKNOWN",',
        '  "critical_step": 0 或非负整数或 null,',
        '  "critical_event_ids": ["event id"],',
        '  "user_intent_boundary": {"goal": "...", "constraints": ["..."], "unknowns": ["..."]},',
        '  "evidence_ref_ids": ["known evidence_ref_id"],',
        '  "rubric": {',
        '    "task_identifiability": 0, "failure_evidence": 0,',
        '    "initial_environment_visibility": 0, "environment_completion_value": 0,',
        '    "verifier_constructability": 0, "episode_boundary_confidence": 0,',
        '    "privacy_processability": 0, "estimated_cost": 0',
        "  },",
        '  "attribution": {"TASK": 0.0, "ENVIRONMENT": 0.0, "SOLVER": 0.0,',
        '    "VERIFIER": 0.0, "SYSTEM": 0.0, "USER": 0.0, "UNKNOWN": 0.0},',
        '  "confidence": 0.0,',
        '  "review_reasons": ["..."]',
        "}",
        "每个 rubric 分数是 0--3 整数：0=无证据，1=弱，2=部分，3=充分；",
        "estimated_cost 是成本而非质量分。",
        "若 outcome=UNCERTAIN，needs_reconstruction 必须为 null、decision 必须为 REVIEW、",
        "primary_failure 必须为 INCONCLUSIVE。",
        "若 outcome=SUCCESS，needs_reconstruction 必须为 false；",
        "若 outcome=FAILURE/INCOMPLETE 且 decision=ELIGIBLE，",
        "needs_reconstruction 必须为 true。",
        "",
        prompt_context(),
        "REPORT:",
        repr(report),
        "",
        "EVIDENCE INDEX:",
        repr(evidence),
    ]
    return "\n".join(lines)


def _base_result(
    report: dict[str, Any],
    model_name: str,
    *,
    status: ModelAnalysisStatus,
    receipt: ModelCallReceipt | None = None,
    errors: tuple[str, ...] = (),
) -> FailureAnalysisModelResult:
    return FailureAnalysisModelResult(
        schema_version=MODEL_ANALYSIS_SCHEMA,
        analysis_id=_analysis_id(report, model_name),
        source_report_id=str(report.get("report_id", "")),
        session_ref=str(report.get("session_ref", "")),
        attempt_ref=(str(report["attempt_ref"]) if report.get("attempt_ref") is not None else None),
        status=status.value,
        outcome=EpisodeOutcome.UNCERTAIN.value,
        needs_reconstruction=None,
        decision=GateDecision.REVIEW.value,
        primary_failure=FailureCategory.INCONCLUSIVE.value,
        failure_layer=FailureLayer.UNCLEAR.value,
        recoverability=Recoverability.UNKNOWN.value,
        critical_step=None,
        critical_event_ids=(),
        user_intent_boundary={"goal": "", "constraints": [], "unknowns": []},
        evidence_ref_ids=(),
        rubric={key: 0 for key in RUBRIC_KEYS},
        attribution={"UNKNOWN": 1.0},
        confidence=0.0,
        review_reasons=errors,
        errors=errors,
        model_receipt=receipt,
    )


def _string_list(value: Any, field: str, errors: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        errors.append(f"{field}_MUST_BE_ARRAY")
        return ()
    if any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f"{field}_ITEM_INVALID")
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return tuple(value)


def _parse_result(
    payload: dict[str, Any],
    report: dict[str, Any],
    evidence: list[dict[str, Any]],
    model_name: str,
    receipt: ModelCallReceipt,
) -> FailureAnalysisModelResult:
    errors: list[str] = []
    reviews = list(_string_list(payload.get("review_reasons", []), "review_reasons", errors))
    outcome = str(payload.get("outcome", ""))
    if outcome not in {item.value for item in EpisodeOutcome}:
        errors.append("OUTCOME_INVALID")
        outcome = EpisodeOutcome.UNCERTAIN.value
    decision = str(payload.get("decision", ""))
    if decision not in {item.value for item in GateDecision}:
        errors.append("DECISION_INVALID")
        decision = GateDecision.REVIEW.value
    failure = str(payload.get("primary_failure", FailureCategory.INCONCLUSIVE.value))
    try:
        FailureCategory(failure)
    except ValueError:
        errors.append("PRIMARY_FAILURE_INVALID")
        failure = FailureCategory.INCONCLUSIVE.value
    layer = str(payload.get("failure_layer", FailureLayer.UNCLEAR.value))
    try:
        FailureLayer(layer)
    except ValueError:
        errors.append("FAILURE_LAYER_INVALID")
        layer = FailureLayer.UNCLEAR.value
    recoverability = str(payload.get("recoverability", Recoverability.UNKNOWN.value))
    try:
        Recoverability(recoverability)
    except ValueError:
        errors.append("RECOVERABILITY_INVALID")
        recoverability = Recoverability.UNKNOWN.value

    raw_need = payload.get("needs_reconstruction")
    need: bool | None
    if raw_need is None:
        need = None
    elif isinstance(raw_need, bool):
        need = raw_need
    else:
        errors.append("NEEDS_RECONSTRUCTION_INVALID")
        need = None

    raw_step = payload.get("critical_step")
    if raw_step is None:
        critical_step = None
    elif isinstance(raw_step, int) and not isinstance(raw_step, bool) and raw_step >= 0:
        critical_step = raw_step
    else:
        errors.append("CRITICAL_STEP_INVALID")
        critical_step = None
    critical_events = _string_list(
        payload.get("critical_event_ids", []), "critical_event_ids", errors
    )
    allowed_events = {
        str(item.get(key))
        for item in evidence
        if isinstance(item, dict)
        for key in ("source_id", "event_id")
        if isinstance(item.get(key), str) and item.get(key)
    }
    allowed_events.update(
        str(event_id)
        for event_id in report.get("critical_event_ids", [])
        if isinstance(event_id, str) and event_id
    )
    unknown_events = sorted(set(critical_events) - allowed_events)
    if unknown_events:
        errors.append("UNKNOWN_CRITICAL_EVENT_ID:" + ",".join(unknown_events))

    intent = payload.get("user_intent_boundary")
    if not isinstance(intent, dict):
        errors.append("USER_INTENT_BOUNDARY_INVALID")
        intent = {"goal": "", "constraints": [], "unknowns": []}
    else:
        goal = intent.get("goal")
        constraints = intent.get("constraints", [])
        unknowns = intent.get("unknowns", [])
        if not isinstance(goal, str) or not goal.strip():
            errors.append("USER_INTENT_GOAL_MISSING")
            goal = ""
        constraints = list(_string_list(constraints, "user_intent_constraints", errors))
        unknowns = list(_string_list(unknowns, "user_intent_unknowns", errors))
        intent = {"goal": goal, "constraints": constraints, "unknowns": unknowns}

    allowed_refs = {
        str(item.get("evidence_ref_id"))
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("evidence_ref_id"), str)
    }
    refs = _string_list(payload.get("evidence_ref_ids", []), "evidence_ref_ids", errors)
    unknown_refs = sorted(set(refs) - allowed_refs)
    if unknown_refs:
        errors.append("UNKNOWN_EVIDENCE_REF:" + ",".join(unknown_refs))
    refs = tuple(ref for ref in refs if ref in allowed_refs)
    if decision == GateDecision.ELIGIBLE.value and not refs:
        errors.append("ELIGIBLE_REQUIRES_EVIDENCE")
    if outcome in {EpisodeOutcome.FAILURE.value, EpisodeOutcome.INCOMPLETE.value} and not refs:
        errors.append("NON_SUCCESS_REQUIRES_EVIDENCE")

    rubric_value = payload.get("rubric")
    rubric: dict[str, int] = {}
    if not isinstance(rubric_value, dict):
        errors.append("RUBRIC_MUST_BE_OBJECT")
        rubric_value = {}
    for key in RUBRIC_KEYS:
        value = rubric_value.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3:
            rubric[key] = value
        else:
            errors.append(f"RUBRIC_{key.upper()}_INVALID")
            rubric[key] = 0

    attribution: dict[str, float] = {}
    raw_attr = payload.get("attribution", {})
    if not isinstance(raw_attr, dict):
        errors.append("ATTRIBUTION_MUST_BE_OBJECT")
    else:
        for key, value in raw_attr.items():
            try:
                FailureAttribution(key)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0 <= float(value) <= 1
                ):
                    raise ValueError
                attribution[key] = float(value)
            except (ValueError, TypeError):
                errors.append(f"ATTRIBUTION_INVALID:{key}")
    if not attribution:
        attribution = {"UNKNOWN": 1.0}

    confidence = payload.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= float(confidence) <= 1
    ):
        errors.append("CONFIDENCE_INVALID")
        confidence = 0.0
    else:
        confidence = float(confidence)

    # Consistency checks keep unknown labels from becoming failure labels or a
    # reconstruction admission decision.
    if outcome == EpisodeOutcome.UNCERTAIN.value:
        if need is not None:
            errors.append("UNCERTAIN_NEEDS_RECONSTRUCTION_MUST_BE_NULL")
        need = None
        if decision != GateDecision.REVIEW.value:
            errors.append("UNCERTAIN_DECISION_MUST_BE_REVIEW")
        decision = GateDecision.REVIEW.value
        if failure != FailureCategory.INCONCLUSIVE.value:
            errors.append("UNCERTAIN_FAILURE_MUST_BE_INCONCLUSIVE")
        failure = FailureCategory.INCONCLUSIVE.value
    elif outcome == EpisodeOutcome.SUCCESS.value:
        if need is not False:
            errors.append("SUCCESS_NEEDS_RECONSTRUCTION_MUST_BE_FALSE")
        need = False
        if failure != FailureCategory.INCONCLUSIVE.value:
            errors.append("SUCCESS_FAILURE_MUST_BE_INCONCLUSIVE")
            failure = FailureCategory.INCONCLUSIVE.value
    elif outcome in {EpisodeOutcome.FAILURE.value, EpisodeOutcome.INCOMPLETE.value}:
        if decision == GateDecision.ELIGIBLE.value and need is not True:
            errors.append("ELIGIBLE_FAILURE_NEEDS_RECONSTRUCTION_MUST_BE_TRUE")
            need = None
            decision = GateDecision.REVIEW.value
        if decision in {GateDecision.REVIEW.value, GateDecision.DEFER.value} and need is False:
            errors.append("UNCERTAIN_RECONSTRUCTION_DECISION")
            need = None
    if errors:
        # Any schema, evidence, or consistency error blocks automatic admission.
        # Keep the outcome only as a model claim; the actionable reconstruction
        # decision is reset to REVIEW and the need flag is unknown.
        status = ModelAnalysisStatus.REVIEW
        reviews.extend(errors)
        decision = GateDecision.REVIEW.value
        need = None
    else:
        status = ModelAnalysisStatus.COMPLETE
    return FailureAnalysisModelResult(
        schema_version=MODEL_ANALYSIS_SCHEMA,
        analysis_id=_analysis_id(report, model_name),
        source_report_id=str(report.get("report_id", "")),
        session_ref=str(report.get("session_ref", "")),
        attempt_ref=(str(report["attempt_ref"]) if report.get("attempt_ref") is not None else None),
        status=status.value,
        outcome=outcome,
        needs_reconstruction=need,
        decision=decision,
        primary_failure=failure,
        failure_layer=layer,
        recoverability=recoverability,
        critical_step=critical_step,
        critical_event_ids=critical_events,
        user_intent_boundary=intent,
        evidence_ref_ids=refs,
        rubric=rubric,
        attribution=attribution,
        confidence=confidence,
        review_reasons=tuple(dict.fromkeys(reviews)),
        errors=tuple(dict.fromkeys(errors)),
        model_receipt=receipt,
    )


def analyze_failure(
    *,
    report: dict[str, Any],
    evidence: list[dict[str, Any]],
    model: ChatModel,
    model_name: str = "claude-opus-4-8",
) -> FailureAnalysisModelResult:
    """调用模型分析一条 attempt；任何不确定性都会返回 REVIEW。"""

    if not isinstance(report, dict):
        raise TypeError("report 必须是 object")
    if not isinstance(evidence, list):
        raise TypeError("evidence 必须是 array")
    # M4 记录历史上使用 ``evidence_id``；在模型边界统一，保证提示词与校验器寻址一致。
    evidence = canonicalize_evidence(evidence)
    if not isinstance(report.get("report_id"), str) or not report["report_id"].strip():
        return _base_result(
            report, model_name, status=ModelAnalysisStatus.REVIEW, errors=("REPORT_ID_MISSING",)
        )
    request = ModelRequest(
        request_id=_request_id(report, model_name),
        model=model_name,
        system=(
            "你是只读的失败分析器。证据不足时必须输出 UNCERTAIN/REVIEW；"
            "永远不要把未知状态当作 FAILURE。只输出 JSON object。"
        ),
        prompt=_prompt(report, evidence),
        response_schema=MODEL_ANALYSIS_SCHEMA,
    )
    try:
        response = model.complete(request)
        receipt = receipt_for_response(response)
        payload = parse_json_object(response.text)
    except ModelGatewayError as exc:
        status = (
            ModelAnalysisStatus.BLOCKED
            if exc.code == "API_KEY_MISSING"
            else ModelAnalysisStatus.FAILED
        )
        return _base_result(report, model_name, status=status, errors=(exc.code,))
    return _parse_result(payload, report, evidence, model_name, receipt)


__all__ = [
    "MODEL_ANALYSIS_PROMPT_VERSION",
    "MODEL_ANALYSIS_SCHEMA",
    "RUBRIC_KEYS",
    "EpisodeOutcome",
    "FailureAnalysisModelResult",
    "ModelAnalysisStatus",
    "analyze_failure",
    "canonicalize_evidence",
]
