from traceforge.curation.sft import CurationThresholds, curate_candidates


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


def test_leakage_is_rejected():
    candidates, _ = curate_candidates([_row(solution_leakage=True)], CurationThresholds())
    assert candidates[0].eligibility == "REJECT"
    assert "SOLUTION_LEAKAGE" in candidates[0].rejection_reasons
