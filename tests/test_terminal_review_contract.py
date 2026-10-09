
"""实跑后审必须覆盖实际 trial 与原义务，不能以空意见或伪引用通过。"""

import copy

import pytest

from traceforge.reconstruction.workspace_sufficiency import _validate_rollout_review


def _case():
    task = {"acceptance_obligations": [{"id": "code"}, {"id": "analysis"}]}
    evidence = {"trials": [{"trial": "a"}, {"trial": "b"}],
                "evidence_refs": ["a/answer", "b/answer", "a/initial/main.py"]}
    payload = {"requirements": [
        {"trial": trial, "obligation_id": obligation, "status": "SOLVER_ERROR",
         "reason": "答卷将模拟响应当成线上根因，实际证据不支持该断言。",
         "evidence_refs": [trial + "/answer"]}
        for trial in ("a", "b") for obligation in ("code", "analysis")
    ]}
    return task, evidence, payload


def test_complete_semantic_review_never_grants_training_acceptance():
    task, evidence, payload = _case()
    result = _validate_rollout_review(task, evidence, payload, [])
    assert result["status"] == "COMPLETE"
    assert result["requirements"] == payload["requirements"]
    assert result["acceptance"] == "NOT_ASSESSED"
    assert result["sft_eligible"] is False


@pytest.mark.parametrize("change", ["missing", "duplicate", "foreign_trial", "foreign_ref",
                                   "empty_refs", "wrong_trial_ref", "unknown_status",
                                   "empty_reason", "malformed_row", "bad_id", "no_payload"])
def test_incomplete_or_ungrounded_review_cannot_complete(change):
    task, evidence, original = _case()
    payload = copy.deepcopy(original)
    row = payload["requirements"][0]
    if change == "missing":
        payload["requirements"].pop()
    elif change == "duplicate":
        payload["requirements"].append(copy.deepcopy(row))
    elif change == "foreign_trial":
        row["trial"] = "invented"
    elif change == "foreign_ref":
        row["evidence_refs"] = ["a/files/invented.py"]
    elif change == "empty_refs":
        row["evidence_refs"] = []
    elif change == "wrong_trial_ref":
        row["evidence_refs"] = ["b/answer"]
    elif change == "unknown_status":
        row["status"] = "PASS"
    elif change == "empty_reason":
        row["reason"] = ""
    elif change == "malformed_row":
        payload["requirements"][0] = "all passed"
    elif change == "bad_id":
        row["obligation_id"] = []
    elif change == "no_payload":
        payload = None
    result = _validate_rollout_review(task, evidence, payload, [])
    assert result["status"] == "REVIEW_INCOMPLETE"
    assert result["errors"]
    assert result["sft_eligible"] is False


def test_model_transport_failure_cannot_be_masked_by_valid_json():
    task, evidence, payload = _case()
    result = _validate_rollout_review(task, evidence, payload, ["UPSTREAM_TRANSPORT_ERROR"])
    assert result["status"] == "REVIEW_INCOMPLETE"
    assert "UPSTREAM_TRANSPORT_ERROR" in result["errors"]
