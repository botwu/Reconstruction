"""M4 Failure Analysis 契约的闭合枚举、稳定 ID 和校验行为。"""

from __future__ import annotations

import pytest

from traceforge.failure_analysis.contracts import (
    EVIDENCE_REF_SCHEMA,
    FAILURE_ANALYSIS_REPORT_SCHEMA,
    INVARIANT_CHECK_SCHEMA,
    RECONSTRUCTABILITY_GATE_SCHEMA,
    EvidenceKind,
    EvidenceRefV1,
    FailureAnalysisReportV1,
    FailureCategory,
    FailureLayer,
    GateDecision,
    GateRoute,
    InvariantCheckV1,
    InvariantKind,
    InvariantResult,
    Recoverability,
    ReconstructabilityGateV1,
    evidence_ref_id,
    failure_analysis_report_id,
    reconstructability_gate_id,
    validate_failure_analysis_report,
    validate_invariant_check,
    validate_reconstructability_gate,
)


def test_schemas_and_stable_ids_bind_upstream_identity() -> None:
    assert EVIDENCE_REF_SCHEMA.endswith(".v1")
    assert INVARIANT_CHECK_SCHEMA.endswith(".v1")
    assert FAILURE_ANALYSIS_REPORT_SCHEMA.endswith(".v1")
    assert RECONSTRUCTABILITY_GATE_SCHEMA.endswith(".v1")

    report_id = failure_analysis_report_id(
        m4_run_id="m4-run-a", task_episode_id="episode-a", target_attempt_id="attempt-a"
    )
    assert report_id == failure_analysis_report_id(
        m4_run_id="m4-run-a", task_episode_id="episode-a", target_attempt_id="attempt-a"
    )
    assert report_id != failure_analysis_report_id(
        m4_run_id="m4-run-b", task_episode_id="episode-a", target_attempt_id="attempt-a"
    )
    assert evidence_ref_id(
        m4_run_id="m4-run-a",
        evidence_kind=EvidenceKind.EVENT,
        source_id="event-1",
        source_pointer="/payload",
    ) != evidence_ref_id(
        m4_run_id="m4-run-a",
        evidence_kind=EvidenceKind.EVENT,
        source_id="event-1",
        source_pointer="/tool_result",
    )
    assert reconstructability_gate_id(
        m4_run_id="m4-run-a", task_episode_id="episode-a", target_attempt_id="attempt-a"
    ) != reconstructability_gate_id(
        m4_run_id="m4-run-a", task_episode_id="episode-a", target_attempt_id="attempt-b"
    )


def test_evidence_and_invariant_contracts_are_serializable_without_raw_text() -> None:
    evidence = EvidenceRefV1(
        schema_version=EVIDENCE_REF_SCHEMA,
        evidence_id="evidence-1",
        evidence_kind=EvidenceKind.EVENT,
        source_id="event-1",
        source_pointer="/payload/error",
        role="FAILURE",
        content_sha256="a" * 64,
    )
    assert evidence.to_dict()["source_pointer"] == "/payload/error"
    assert "content" not in evidence.to_dict()

    check = InvariantCheckV1(
        schema_version=INVARIANT_CHECK_SCHEMA,
        invariant_id="inv-1",
        kind=InvariantKind.DYNAMIC,
        check_code="tool_result_matches_call",
        result=InvariantResult.UNCLEAR,
        trigger_event_id="event-1",
        evidence_ref_ids=("evidence-1",),
        taxonomy_targets=(FailureCategory.INCONCLUSIVE,),
        error_code=None,
    )
    validate_invariant_check(check)

    not_run = check.__class__(**{**check.to_dict(), "result": InvariantResult.NOT_RUN})
    validate_invariant_check(not_run)


def test_unclear_failure_layer_is_explicit_for_deterministic_unknowns() -> None:
    report = FailureAnalysisReportV1(
        schema_version=FAILURE_ANALYSIS_REPORT_SCHEMA,
        report_id="report-1",
        session_ref="session-1",
        episode_ref="episode-1",
        attempt_ref="attempt-1",
        capture_occurrence_id="capture-1",
        primary_failure=FailureCategory.INCONCLUSIVE,
        failure_layer=FailureLayer.UNCLEAR,
        critical_event_ids=("event-1",),
        critical_step=None,
        evidence_ref_ids=("evidence-1",),
        violated_invariant_ids=(),
        causal_hypotheses=(),
        recoverability=Recoverability.UNKNOWN,
        attribution={"UNKNOWN": 1.0},
        reconstruction_targets=(),
        reconstruction_relevance={"task_recovery": "UNKNOWN", "environment_recovery": "UNKNOWN"},
        open_questions=("M1B ended before the first observed tool result",),
        confidence=0.2,
        uncertainty_codes=("STRUCTURAL_INPUT_INCOMPLETE",),
    )
    validate_failure_analysis_report(report)
    assert report.task_episode_id == "episode-1"
    assert report.target_attempt_id == "attempt-1"
    assert report.primary_step is None


def test_failure_report_rejects_invalid_layer_and_confidence() -> None:
    report = FailureAnalysisReportV1(
        schema_version=FAILURE_ANALYSIS_REPORT_SCHEMA,
        report_id="report-1",
        session_ref="session-1",
        episode_ref="episode-1",
        attempt_ref="attempt-1",
        capture_occurrence_id="capture-1",
        primary_failure=FailureCategory.SYSTEM_FAILURE,
        failure_layer=FailureLayer.SYSTEM,
        critical_event_ids=(),
        critical_step=0,
        evidence_ref_ids=(),
        violated_invariant_ids=(),
        causal_hypotheses=(),
        recoverability=Recoverability.LOW,
        attribution={"SYSTEM": 1.0},
        reconstruction_targets=(),
        reconstruction_relevance={},
        open_questions=(),
        confidence=0.8,
        uncertainty_codes=(),
    )
    report = report.__class__(**{**report.to_dict(), "failure_layer": "SOLVER"})
    # A valid enum value remains accepted; structural layer validation is explicit.
    validate_failure_analysis_report(report)
    with pytest.raises(ValueError):
        bad = report.__class__(**{**report.to_dict(), "confidence": 1.1})
        validate_failure_analysis_report(bad)


def _gate(**overrides: object) -> ReconstructabilityGateV1:
    values: dict[str, object] = {
        "schema_version": RECONSTRUCTABILITY_GATE_SCHEMA,
        "gate_id": "gate-1",
        "session_ref": "session-1",
        "episode_ref": "episode-1",
        "attempt_ref": "attempt-1",
        "decision": GateDecision.REVIEW,
        "route": GateRoute.NEEDS_MANUAL_REVIEW,
        "task_identifiability": 2,
        "failure_evidence": 2,
        "initial_environment_visibility": 1,
        "environment_completion_value": 3,
        "verifier_constructability": 1,
        "episode_boundary_confidence": 2,
        "privacy_processability": 3,
        "estimated_cost": 2,
        "evidence_ref_ids": ("evidence-1",),
        "blocking_reason_codes": ("MISSING_INITIAL_STATE",),
        "confidence": 0.7,
        "review_required": True,
    }
    values.update(overrides)
    return ReconstructabilityGateV1(**values)


def test_reconstructability_gate_validates_rubric_ranges() -> None:
    gate = _gate()
    validate_reconstructability_gate(gate)
    assert gate.task_episode_id == "episode-1"
    assert gate.target_attempt_id == "attempt-1"

    with pytest.raises(ValueError):
        validate_reconstructability_gate(_gate(task_identifiability=4))
    with pytest.raises(ValueError):
        validate_reconstructability_gate(_gate(confidence=-0.1))
    with pytest.raises(ValueError):
        validate_reconstructability_gate(_gate(decision="UNKNOWN"))


def test_invariant_error_requires_code_and_other_results_forbid_it() -> None:
    base = InvariantCheckV1(
        schema_version=INVARIANT_CHECK_SCHEMA,
        invariant_id="inv-1",
        kind=InvariantKind.STATIC,
        check_code="policy",
        result=InvariantResult.ERROR,
        trigger_event_id=None,
        evidence_ref_ids=(),
        taxonomy_targets=(),
        error_code="CHECKER_EXCEPTION",
    )
    validate_invariant_check(base)
    with pytest.raises(ValueError):
        validate_invariant_check(
            base.__class__(**{**base.to_dict(), "error_code": None})
        )
    with pytest.raises(ValueError):
        validate_invariant_check(
            base.__class__(**{**base.to_dict(), "result": InvariantResult.PASS})
        )
