from traceforge.curation.sft import (
    CurationInputError,
    CurationThresholds,
    curate_candidates,
    write_reconstruction_sft_curation,
)


def _row(**updates):
    row = {
        "candidate_id": "c1",
        "bundle_id": "b1",
        "rollout_id": "r1",
        "trial_id": "t1",
        "verifier_status": "PASS",
        "reward": 1.0,
        "task_recovery_confidence": 0.9,
        "environment_recovery_confidence": 0.9,
        "trajectory_quality": 0.9,
        "reproducible": True,
        "solution_leakage": False,
        "trajectory_artifact": "trajectory.json",
    }
    row.update(updates)
    return row


def test_pass_only_curation():
    candidates, metrics = curate_candidates([_row(), _row(candidate_id="c2", reward=0.0)])
    assert candidates[0].eligibility == "ELIGIBLE"
    assert candidates[1].eligibility == "REJECT"
    assert "REWARD_BELOW_THRESHOLD" in candidates[1].rejection_reasons
    assert metrics["eligible_count"] == 1
    assert metrics["review_count"] == 0
    assert metrics["rejected_count"] == 1


def test_leakage_is_rejected():
    candidates, _ = curate_candidates([_row(solution_leakage=True)], CurationThresholds())
    assert candidates[0].eligibility == "REJECT"
    assert "SOLUTION_LEAKAGE" in candidates[0].rejection_reasons


def test_missing_audit_evidence_requires_review():
    row = _row()
    del row["solution_leakage"]
    del row["reproducible"]
    candidates, metrics = curate_candidates([row])
    assert candidates[0].eligibility == "REVIEW"
    assert "SOLUTION_LEAKAGE_UNVERIFIED" in candidates[0].rejection_reasons
    assert "REPRODUCIBILITY_UNVERIFIED" in candidates[0].rejection_reasons
    assert metrics["eligible_count"] == 0
    assert metrics["review_count"] == 1
    assert metrics["rejected_count"] == 0


def test_non_boolean_audit_evidence_is_input_error():
    try:
        curate_candidates([_row(solution_leakage="false")])
    except CurationInputError as exc:
        assert "solution_leakage" in str(exc)
    else:
        raise AssertionError("non-boolean leakage evidence must not be interpreted by truthiness")


def test_non_finite_or_out_of_range_reward_is_input_error():
    import math

    row = _row(reward=math.nan)
    try:
        curate_candidates([row])
    except CurationInputError as exc:
        assert "有限" in str(exc)
    else:
        raise AssertionError("NaN reward must not enter curation")
    try:
        curate_candidates([_row(reward=2.0)])
    except CurationInputError as exc:
        assert "[0,1]" in str(exc)
    else:
        raise AssertionError("out-of-range reward must not enter curation")


def test_write_reconstruction_sft_curation_pending_and_not_applicable(tmp_path) -> None:
    path = write_reconstruction_sft_curation(
        tmp_path,
        [
            {"task_id": "t1", "status": "PENDING_EXECUTION", "errors": []},
            {
                "task_id": "t2",
                "status": "REVIEW",
                "verification": {"status": "NOT_APPLICABLE", "errors": ["NO_FILE_ACCEPTANCE"]},
            },
        ],
    )
    payload = path.read_text(encoding="utf-8")
    assert "traceforge.sft-curation.v1" in payload
    assert "PENDING" in payload
    assert "NO_FILE_ACCEPTANCE" in payload
