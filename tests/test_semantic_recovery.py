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


def test_prompt_projection_keeps_structured_calls_and_auditable_coverage():
    from traceforge.reconstruction.semantic_recovery import _prompt, _prompt_evidence

    evidence = [
        {
            "evidence_ref_id": "e-call",
            "role": "AGENT_ACTION",
            "event_kind": "TOOL_CALL",
            "phase": "attempt",
            "source_id": "event-1",
            "source_pointer": "/events/1",
            "content_sha256": "abc",
            "tool_name": "chat_history_get",
            "tool_args": {"rounds": [1, 19]},
            "tool_result": {"status": "RESULT_NOT_OBSERVED", "messages": ["x"]},
            "text": "user visible text",
        }
    ]
    projected = _prompt_evidence(evidence)
    assert projected[0]["tool_name"] == "chat_history_get"
    assert projected[0]["tool_args"]["rounds"] == [1, 19]
    assert projected[0]["source_pointer"] == "/events/1"
    assert projected[-1]["_projection_coverage"]["covered_count"] == 1
    prompt = _prompt("task", {}, evidence)
    assert "原 agent 建议/猜测" in prompt
    assert "RESULT_NOT_OBSERVED 不是执行失败" in prompt


def test_projection_marks_long_trace_omissions_and_preserves_refs():
    from traceforge.reconstruction.semantic_recovery import _prompt_evidence

    evidence = [
        {"evidence_ref_id": f"e-{i}", "source_pointer": f"/events/{i}", "text": "x" * 200}
        for i in range(20)
    ]
    projected = _prompt_evidence(evidence, max_chars=900)
    coverage = projected[-1]["_projection_coverage"]
    assert coverage["truncated"] is True
    assert coverage["omitted_count"] > 0
    assert all(ref.startswith("e-") for ref in coverage["omitted_evidence_ref_ids"])


def test_model_evidence_reference_uses_authoritative_metadata():
    response = _task("e")

    class CapturingModel(FakeModel):
        def complete(self, request):
            return super().complete(request)

    outcome = recover(
        kind="task",
        attempt_ref="a",
        source_report_id="r",
        report={},
        evidence=[
            {
                "evidence_ref_id": "e",
                "role": "USER",
                "source_pointer": "/authoritative",
                "content_sha256": "hash",
            }
        ],
        model=FakeModel(
            response.replace(
                '"role": "observed", "source_pointer": "e"',
                '"role": "MODEL", "source_pointer": "/rewritten", "content_sha256": "bad"',
            )
        ),
    )
    assert outcome.candidates[0].evidence[0].role == "USER"
    assert outcome.candidates[0].evidence[0].source_pointer == "/authoritative"
    assert outcome.candidates[0].evidence[0].content_sha256 == "hash"


def test_priority_target_and_attempt_events_are_covered_before_context():
    from traceforge.reconstruction.semantic_recovery import _prompt_evidence

    evidence = [
        {"evidence_ref_id": "ctx-0", "role": "SYSTEM", "text": "c" * 500},
        {"evidence_ref_id": "target", "role": "TARGET_REQUEST", "text": "do task"},
        {
            "evidence_ref_id": "action",
            "role": "ATTEMPT_ACTION",
            "tool_name": "chat_history_get",
            "tool_args": {"rounds": [1, 19]},
        },
        {
            "evidence_ref_id": "obs",
            "role": "ATTEMPT_OBSERVATION",
            "tool_result": {"status": "RESULT_NOT_OBSERVED"},
        },
        {"evidence_ref_id": "response", "role": "ATTEMPT_RESPONSE", "text": "unfinished"},
        {"evidence_ref_id": "ctx-1", "role": "SYSTEM", "text": "c" * 500},
    ]
    projected = _prompt_evidence(evidence, max_chars=1000)
    coverage = projected[-1]["_projection_coverage"]
    assert {"target", "action", "obs", "response"}.issubset(
        set(coverage["priority_covered_evidence_ref_ids"])
    )
