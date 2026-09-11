from pathlib import Path

import pytest

from traceforge.reconstruction.control_plane import (
    ControlPlane,
    GateStatus,
    Stage,
    default_policy,
    evaluate_gate,
    seal_artifact,
    verify_artifact,
)
from traceforge.reconstruction.control_plane_contracts import StageBudgetV1


def test_default_policy_is_versioned_and_bounded():
    policy = default_policy()
    assert policy.schema_version.endswith("v1")
    assert policy.budgets[Stage.ENVIRONMENT_COMPLETION.value].max_candidates == 5
    assert policy.budgets[Stage.ENVIRONMENT_COMPLETION.value].temperature <= 2


def test_evidence_and_artifact_are_hard_gates(tmp_path: Path):
    policy = default_policy()
    result = evaluate_gate(
        run_id="run",
        stage=Stage.TASK_RECOVERY,
        attempt=1,
        observation={"evidence_count": 0, "artifact_required": True},
        policy=policy,
    )
    assert result.status == GateStatus.BLOCKED
    artifact_path = tmp_path / "a.json"
    artifact_path.write_text("{}", encoding="utf-8")
    ref = seal_artifact(artifact_path)
    assert verify_artifact(ref)
    artifact_path.write_text("changed", encoding="utf-8")
    assert not verify_artifact(ref)


def test_retry_and_global_budget():
    policy = default_policy()
    policy = type(policy)(
        budgets={
            **policy.budgets,
            Stage.TASK_RECOVERY.value: StageBudgetV1(max_attempts=2, retryable_errors=("TIMEOUT",)),
        },
        max_total_retries=1,
    )
    first = evaluate_gate(
        run_id="run",
        stage=Stage.TASK_RECOVERY,
        attempt=1,
        observation={"error_code": "TIMEOUT"},
        policy=policy,
    )
    assert first.status == GateStatus.RETRY
    second = evaluate_gate(
        run_id="run",
        stage=Stage.TASK_RECOVERY,
        attempt=2,
        observation={"error_code": "TIMEOUT"},
        policy=policy,
    )
    assert second.status == GateStatus.FAIL


def test_control_plane_enforces_order_and_review():
    cp = ControlPlane("run")
    with pytest.raises(ValueError):
        cp.gate(Stage.TASK_RECOVERY, {"evidence_count": 1})
    decision = cp.gate(Stage.INGESTION, {"evidence_count": 1})
    assert decision.status == GateStatus.PASS
    assert cp.state.current_stage == Stage.FAILURE_ANALYSIS.value
    review = cp.gate(Stage.FAILURE_ANALYSIS, {"evidence_count": 1, "uncertain": True})
    assert review.status == GateStatus.REVIEW
    assert cp.state.status == "REVIEW"


def test_review_cannot_be_bypassed_without_resolution():
    cp = ControlPlane("run")
    cp.gate(Stage.INGESTION, {"evidence_count": 1})
    cp.gate(Stage.FAILURE_ANALYSIS, {"evidence_count": 1, "uncertain": True})
    blocked = cp.gate(Stage.FAILURE_ANALYSIS, {"evidence_count": 1})
    assert blocked.status == GateStatus.REVIEW
    assert cp.state.status == "REVIEW"
