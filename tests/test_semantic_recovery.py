from __future__ import annotations

import json

import pytest

from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    OpusClient,
    parse_json_object,
)
from traceforge.reconstruction.semantic_recovery import RecoveryStatus, recover, recovery_metrics


class FakeModel:
    def __init__(self, text: str):
        self.text = text

    def complete(self, request: ModelRequest) -> ModelResponse:
        assert request.response_schema.endswith("recovery-candidates.v1")
        return ModelResponse(request.request_id, request.model, "fake", self.text, 1, 0.01)


def _task(ref: str) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "task_title": "整理文件",
                    "task_instruction": "整理 workspace 中的文件",
                    "user_intent": "用户希望整理文件",
                    "acceptance_obligations": [],
                    "explicit_constraints": [],
                    "ambiguities": [],
                    "do_not_infer": [],
                    "evidence": [
                        {"evidence_ref_id": ref, "role": "observed", "source_pointer": "e"}
                    ],
                    "confidence": 0.8,
                    "decision": "READY",
                }
            ],
            "open_questions": [],
        }
    )


def test_task_recovery_accepts_only_known_evidence():
    outcome = recover(
        kind="task",
        attempt_ref="a",
        source_report_id="r",
        report={},
        evidence=[{"evidence_ref_id": "e"}],
        model=FakeModel(_task("unknown")),
    )
    assert outcome.status == RecoveryStatus.REVIEW
    assert outcome.candidates == ()


def test_task_recovery_and_metrics():
    outcome = recover(
        kind="task",
        attempt_ref="a",
        source_report_id="r",
        report={},
        evidence=[{"evidence_ref_id": "e"}],
        model=FakeModel(_task("e")),
    )
    assert outcome.status == RecoveryStatus.COMPLETE
    assert recovery_metrics([outcome])["complete_rate"] == 1.0


def test_invalid_model_json_is_failed_without_candidate():
    outcome = recover(
        kind="task",
        attempt_ref="a",
        source_report_id="r",
        report={},
        evidence=[],
        model=FakeModel("not json"),
    )
    assert outcome.status == RecoveryStatus.FAILED
    with pytest.raises(ModelGatewayError):
        parse_json_object("[]")


def test_missing_key_is_blocked(monkeypatch):
    monkeypatch.delenv("TRACEFORGE_TEST_KEY", raising=False)
    client = OpusClient(api_key_env="TRACEFORGE_TEST_KEY", max_retries=0)
    request = ModelRequest("r", "claude-opus-4-8", "s", "p", "schema")
    with pytest.raises(ModelGatewayError, match="TRACEFORGE_TEST_KEY"):
        client.complete(request)
