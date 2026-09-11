import json

import pytest

from traceforge.failure_analysis.agentrx_pipeline import normalize_trajectory, run_agentrx_diagnosis
from traceforge.reconstruction.model_gateway import ModelResponse


class FixtureModel:
    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if request.response_schema != "json_object":
            raise AssertionError
        if "PROMPT_SOURCE" in request.prompt and "static_invariants" in request.prompt:
            payload = {
                "invariants": [
                    {
                        "criterion": "event must cite evidence",
                        "check_type": "nl_check",
                        "trigger_step": 1,
                        "evidence_ref_ids": ["ev-1"],
                        "confidence": 0.8,
                    }
                ]
            }
        elif "PREFIX:" in request.prompt:
            payload = {
                "invariants": [
                    {
                        "criterion": "computed result agrees with tool output",
                        "check_type": "python_check",
                        "code": "assert True",
                        "trigger_step": 1,
                        "evidence_ref_ids": ["ev-1"],
                        "confidence": 0.7,
                    }
                ]
            }
        else:
            payload = {
                "reason_for_failure": "first unresolved violation",
                "failure_case": 3,
                "failure_step": 1,
                "confidence": 0.9,
                "evidence_ref_ids": ["ev-1"],
            }
        return ModelResponse(
            request.request_id, request.model, "fixture", json.dumps(payload), 1, 0.01
        )


def traj():
    return {
        "trajectory_id": "t1",
        "instruction": "do X",
        "steps": [
            {
                "index": 1,
                "substeps": [
                    {
                        "event_id": "e1",
                        "role": "assistant",
                        "content": "call tool",
                        "evidence_ref_ids": ["ev-1"],
                    }
                ],
            }
        ],
    }


def test_normalize_requires_event_id():
    bad = traj()
    bad["steps"][0]["substeps"][0].pop("event_id")
    with pytest.raises(ValueError, match="EVENT"):
        normalize_trajectory(bad)


def test_multistage_and_strict_check_statuses():
    model = FixtureModel()
    report = run_agentrx_diagnosis(traj(), model)
    assert len(model.requests) == 3
    assert report.schema_version.endswith("v1")
    assert len(report.static_invariants) == 1
    assert len(report.dynamic_invariants_by_prefix) == 1
    assert {c.status for c in report.checks} == {"UNCLEAR", "NEEDS_SANDBOX"}
    assert report.coverage["status_counts"]["UNCLEAR"] == 1
    assert all(r.status == "COMPLETED" for r in report.receipts)
    assert report.root_cause["failure_case"] == 3


def test_unknown_evidence_is_rejected_and_receipt_recorded():
    class Unknown(FixtureModel):
        def complete(self, request):
            self.requests.append(request)
            payload = (
                {
                    "invariants": [
                        {
                            "criterion": "bad",
                            "check_type": "nl_check",
                            "evidence_ref_ids": ["missing"],
                        }
                    ]
                }
                if "static_invariants" in request.prompt
                else {
                    "reason_for_failure": "x",
                    "failure_case": 10,
                    "evidence_ref_ids": ["missing"],
                }
            )
            return ModelResponse(
                request.request_id, request.model, "fixture", json.dumps(payload), 1, 0.0
            )

    report = run_agentrx_diagnosis(traj(), Unknown())
    assert "STATIC_0_UNKNOWN_EVIDENCE:missing" in report.errors
    assert "JUDGE_UNKNOWN_EVIDENCE" in report.errors
