from __future__ import annotations

import json

import pytest

from traceforge.failure_analysis.trace_capabilities import (
    UNAVAILABLE_MISSING_OUTCOME_LABELS,
    CapabilityValidationError,
    aggregate_capability_runs,
    build_discovery_prompt,
    build_label_prompt,
    compute_metrics,
    label_capabilities,
)
from traceforge.reconstruction.model_gateway import ModelResponse

OUTCOMES = {"f1": "FAILURE", "f2": "FAILURE", "s1": "SUCCESS", "s2": "SUCCESS"}


def labels(*, overlap: bool = False):
    value = {
        "planning": {
            "lacking_failed": ["f1"],
            "present_failed": ["f2"],
            "na_failed": [],
            "lacking_passed": [],
            "present_passed": ["s1"],
            "na_passed": ["s2"],
        }
    }
    if overlap:
        value["planning"]["na_passed"] = ["s1", "s2"]
    return value


def test_compute_metrics_matches_trace_formula():
    metrics = compute_metrics(labels(), OUTCOMES)
    assert metrics is not None
    metric = metrics["planning"]
    assert metric.coverage == pytest.approx(0.5)
    assert metric.contrastive_gap == pytest.approx(0.5)


def test_aggregation_reports_consistency_ratio_and_pass_count():
    result = aggregate_capability_runs(
        [{"labels": labels()}, {"labels": labels()}], OUTCOMES, rho=0.4, delta=0.5, consistency_k=2
    )
    assert result.status == "AVAILABLE"
    summary = result.metrics["planning"]
    assert summary.pass_count == 2
    assert summary.consistency_ratio == pytest.approx(1.0)
    assert summary.passes_consistency is True


def test_missing_trusted_control_group_has_null_metrics_without_zero_fallback():
    result = aggregate_capability_runs([{"labels": labels()}], {"f1": "FAILURE", "u": "UNCERTAIN"})
    assert result.status == UNAVAILABLE_MISSING_OUTCOME_LABELS
    assert result.metrics is None
    assert result.run_metrics == ()


def test_partition_rejects_overlap_and_untrusted_outcome():
    with pytest.raises(CapabilityValidationError) as overlap:
        aggregate_capability_runs([{"labels": labels(overlap=True)}], OUTCOMES)
    assert overlap.value.code == "LABEL_LIST_OVERLAP"
    unknown = dict(OUTCOMES, u="INCOMPLETE")
    bad = labels()
    bad["planning"]["na_passed"].append("u")
    with pytest.raises(CapabilityValidationError) as untrusted:
        aggregate_capability_runs([{"labels": bad}], unknown)
    assert untrusted.value.code == "UNTRUSTED_OUTCOME_LABEL"


def test_prompts_include_trace_structure_and_independent_attempt():
    discovery = build_discovery_prompt(model_name="opus", trajectories=[{"id": "f1"}])
    labeling = build_label_prompt(
        model_name="opus",
        trajectories=[{"id": "f1"}],
        candidates={"candidates": []},
        attempt_number=3,
    )
    assert "Phase 1" in discovery and "candidates" in discovery
    assert "attempt 3" in labeling and "six lists" in labeling


def test_label_capabilities_validates_model_output():
    class Fake:
        def complete(self, request):
            return ModelResponse(
                request.request_id,
                request.model,
                "fake",
                json.dumps({"attempt": 1, "labels": labels()}),
                1,
                0.0,
            )

    payload, response = label_capabilities(
        model=Fake(),
        model_name="opus",
        trajectories=[],
        candidates={},
        attempt_number=1,
        trajectory_outcomes=OUTCOMES,
    )
    assert payload["attempt"] == 1
    assert response.provider == "fake"
