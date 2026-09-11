from __future__ import annotations

import json

import pytest

from traceforge.failure_analysis.model_analyzer import (
    EpisodeOutcome,
    ModelAnalysisStatus,
    analyze_failure,
)
from traceforge.reconstruction.model_gateway import ModelResponse


class FakeModel:
    def __init__(self, payload: dict):
        self.payload = payload
        self.request = None

    def complete(self, request):
        self.request = request
        return ModelResponse(
            request_id=request.request_id,
            model=request.model,
            provider="fake",
            text=json.dumps(self.payload),
            attempts=1,
            latency_seconds=0.001,
        )


REPORT = {
    "report_id": "report-1",
    "session_ref": "session-1",
    "attempt_ref": "attempt-1",
    "capture_occurrence_id": "capture-1",
    "primary_failure": "INCONCLUSIVE",
}
EVIDENCE = [
    {"evidence_ref_id": "ev-1", "source_id": "event-1", "role": "FAILURE"},
    {"evidence_ref_id": "ev-2", "source_id": "event-2", "role": "TASK"},
]


def payload(**overrides):
    value = {
        "outcome": "FAILURE",
        "needs_reconstruction": True,
        "decision": "ELIGIBLE",
        "primary_failure": "INVALID_TOOL_INVOCATION",
        "failure_layer": "SOLVER",
        "recoverability": "HIGH",
        "critical_step": 4,
        "critical_event_ids": ["event-1"],
        "user_intent_boundary": {
            "goal": "完成用户要求",
            "constraints": ["遵守用户约束"],
            "unknowns": [],
        },
        "evidence_ref_ids": ["ev-1", "ev-2"],
        "rubric": {
            "task_identifiability": 3,
            "failure_evidence": 3,
            "initial_environment_visibility": 2,
            "environment_completion_value": 3,
            "verifier_constructability": 2,
            "episode_boundary_confidence": 2,
            "privacy_processability": 3,
            "estimated_cost": 1,
        },
        "attribution": {"SOLVER": 1.0},
        "confidence": 0.9,
        "review_reasons": [],
    }
    value.update(overrides)
    return value


def test_failure_analysis_returns_reconstruction_decision_and_rubric():
    model = FakeModel(payload())
    result = analyze_failure(report=REPORT, evidence=EVIDENCE, model=model)
    assert result.status == ModelAnalysisStatus.COMPLETE
    assert result.outcome == EpisodeOutcome.FAILURE
    assert result.needs_reconstruction is True
    assert result.decision == "ELIGIBLE"
    assert result.critical_step == 4
    assert result.evidence_ref_ids == ("ev-1", "ev-2")
    assert result.rubric["failure_evidence"] == 3
    assert model.request.response_schema.endswith("failure-analysis-model.v1")


def test_unknown_outcome_forces_review_and_cannot_be_failure():
    model = FakeModel(
        payload(
            outcome="UNCERTAIN",
            needs_reconstruction=None,
            decision="REVIEW",
            primary_failure="INCONCLUSIVE",
        )
    )
    result = analyze_failure(report=REPORT, evidence=EVIDENCE, model=model)
    assert result.status == ModelAnalysisStatus.COMPLETE
    assert result.outcome == "UNCERTAIN"
    assert result.needs_reconstruction is None
    assert result.decision == "REVIEW"
    assert result.primary_failure == "INCONCLUSIVE"


def test_unknown_evidence_ref_is_reviewed_and_not_admitted():
    model = FakeModel(payload(evidence_ref_ids=["ev-1", "not-provided"]))
    result = analyze_failure(report=REPORT, evidence=EVIDENCE, model=model)
    assert result.status == ModelAnalysisStatus.REVIEW
    assert result.decision == "REVIEW"
    assert result.needs_reconstruction is None
    assert result.evidence_ref_ids == ("ev-1",)
    assert any("UNKNOWN_EVIDENCE_REF" in reason for reason in result.review_reasons)


def test_missing_intent_and_rubric_never_pass():
    value = payload(user_intent_boundary={}, rubric={})
    result = analyze_failure(report=REPORT, evidence=EVIDENCE, model=FakeModel(value))
    assert result.status == "REVIEW"
    assert "USER_INTENT_GOAL_MISSING" in result.errors
    assert any(item.startswith("RUBRIC_") for item in result.errors)


def test_invalid_json_is_failed_without_inference():
    class InvalidModel:
        def complete(self, request):
            return ModelResponse(request.request_id, request.model, "fake", "not json", 1, 0.0)

    result = analyze_failure(report=REPORT, evidence=EVIDENCE, model=InvalidModel())
    assert result.status == ModelAnalysisStatus.FAILED
    assert result.outcome == EpisodeOutcome.UNCERTAIN
    assert result.needs_reconstruction is None
    assert "INVALID_JSON" in result.errors


def test_non_object_inputs_are_rejected_before_model_call():
    with pytest.raises(TypeError):
        analyze_failure(report=[], evidence=EVIDENCE, model=FakeModel(payload()))
    with pytest.raises(TypeError):
        analyze_failure(report=REPORT, evidence={}, model=FakeModel(payload()))


def test_unknown_critical_event_is_reviewed():
    result = analyze_failure(
        report=REPORT,
        evidence=EVIDENCE,
        model=FakeModel(payload(critical_event_ids=["event-not-provided"])),
    )
    assert result.status == ModelAnalysisStatus.REVIEW
    assert result.decision == "REVIEW"
    assert result.needs_reconstruction is None
    assert any("UNKNOWN_CRITICAL_EVENT_ID" in reason for reason in result.review_reasons)
