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


def test_projection_exposes_only_citable_ref_not_confusable_hashes():
    # 回归：投影曾在同一行同时暴露 evidence_ref_id / source_id / content_sha256 三个
    # 同形 sha256，而只有 evidence_ref_id 可被引用。推理模型（gpt-5）据此引用了不可
    # 引用的 source_id，触发 EVIDENCE_REF_UNKNOWN、打断整条重建。模型可见行只应保留
    # 可引用的 evidence_ref_id 与人类可读的 source_pointer，不得暴露 source_id /
    # content_sha256 这类不可引用的同形哈希（其权威值另由输入索引在校验期回填）。
    from traceforge.reconstruction.semantic_recovery import _prompt_evidence

    evidence = [
        {
            "evidence_ref_id": "a" * 64,
            "source_id": "b" * 64,
            "content_sha256": "c" * 64,
            "source_pointer": "/events/7",
            "role": "TARGET_REQUEST",
            "text": "用户请求",
        }
    ]
    row = _prompt_evidence(evidence)[0]
    assert row["evidence_ref_id"] == "a" * 64
    assert row["source_pointer"] == "/events/7"
    assert "source_id" not in row
    assert "content_sha256" not in row


def test_recover_completes_when_model_cites_ref_amid_distinct_source_id():
    # 端到端：证据带有与 evidence_ref_id 不同的 source_id / content_sha256，模型正确
    # 引用 evidence_ref_id 时应 COMPLETE —— 证明收窄投影未破坏真实多哈希证据的正常路径。
    outcome = recover(
        kind="task",
        attempt_ref="a",
        source_report_id="r",
        report={},
        evidence=[
            {
                "evidence_ref_id": "e" * 64,
                "source_id": "s" * 64,
                "content_sha256": "h" * 64,
                "source_pointer": "/events/1",
            }
        ],
        model=FakeModel(_task("e" * 64)),
    )
    assert outcome.status == RecoveryStatus.COMPLETE
    assert outcome.candidates[0].evidence[0].evidence_ref_id == "e" * 64


def test_projection_marks_long_trace_omissions_and_preserves_refs():
    from traceforge.reconstruction.semantic_recovery import _prompt_evidence

    evidence = [
        {"evidence_ref_id": f"e-{i}", "source_pointer": f"/events/{i}", "text": "x" * 200}
        for i in range(20)
    ]
    projected = _prompt_evidence(evidence, max_chars=900)
    coverage = projected[-1]["_projection_coverage"]
    assert coverage["truncated"] is False
    assert coverage["omitted_count"] == 0
    assert coverage["covered_count"] == len(evidence)


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


def test_source_quality_gate_overrides_ready():
    outcome = recover(
        kind="task",
        attempt_ref="a",
        source_report_id="r",
        report={"status": "INCONCLUSIVE", "selection": {"pending_tool_call_count": 1}},
        evidence=[{"evidence_ref_id": "e", "phase": "TARGET_REQUEST"}],
        model=FakeModel(_task("e")),
    )
    assert outcome.status == RecoveryStatus.REVIEW
    assert "SOURCE_QUALITY_REVIEW_REQUIRED" in outcome.errors


def test_paired_observed_tool_trajectory_completes():
    # 工具调用已配对+已观测、候选全 READY、report 干净：应自动 COMPLETE。
    # 仅凭 evidence 里出现 ATTEMPT_ACTION 阶段不构成来源质量风险，
    # 否则任何带工具调用的真实轨迹都无法通过 COMPLETE 门禁。
    outcome = recover(
        kind="task",
        attempt_ref="a",
        source_report_id="r",
        report={},
        evidence=[
            {"evidence_ref_id": "e", "phase": "ATTEMPT_ACTION"},
            {"evidence_ref_id": "obs", "phase": "ATTEMPT_OBSERVATION"},
        ],
        model=FakeModel(_task("e")),
    )
    assert outcome.status == RecoveryStatus.COMPLETE
    assert "SOURCE_QUALITY_REVIEW_REQUIRED" not in outcome.errors


def test_pending_tool_call_still_reviews():
    # 未配对/未观测的工具调用（pending>0）才是真实的“工具交互不完整”风险，仍须人审。
    outcome = recover(
        kind="task",
        attempt_ref="a",
        source_report_id="r",
        report={"selection": {"pending_tool_call_count": 1}},
        evidence=[{"evidence_ref_id": "e", "phase": "ATTEMPT_ACTION"}],
        model=FakeModel(_task("e")),
    )
    assert outcome.status == RecoveryStatus.REVIEW
    assert "SOURCE_QUALITY_REVIEW_REQUIRED" in outcome.errors


def test_inconclusive_report_still_reviews():
    # 失败分析未能定性（INCONCLUSIVE）时，即使候选 READY 也须人审。
    outcome = recover(
        kind="task",
        attempt_ref="a",
        source_report_id="r",
        report={"status": "INCONCLUSIVE"},
        evidence=[{"evidence_ref_id": "e"}],
        model=FakeModel(_task("e")),
    )
    assert outcome.status == RecoveryStatus.REVIEW
    assert "SOURCE_QUALITY_REVIEW_REQUIRED" in outcome.errors
