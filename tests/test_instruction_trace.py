"""初始模型请求完整可审计，摘要绑定原字节，私有轨迹仍过滤敏感信息。"""

import hashlib
import json

import pytest

from traceforge.reconstruction.agents import VERIFIER_ROLE, AgentSession
from traceforge.reconstruction.agents.runtime import HermesNativeRuntime


@pytest.mark.parametrize("private_prefix", [
    "",
    "api_key=fixture-secret-value\n<thinking>fixture-private-thought</thinking>\n",
])
def test_initial_instruction_full_feedback_is_saved_without_changing_model_exchange(
    tmp_path, private_prefix,
):
    feedback = "PREVIOUS_CALIBRATION_FEEDBACK:\n" + json.dumps({
        "error": "ImportError: cannot import name target",
        "repair": "保留行为断言并修正导入位置",
    }, ensure_ascii=False)
    instruction = private_prefix + "原始任务上下文\n" * 900 + feedback
    final_text = '{"status":"READY","reason":"fixture result"}'

    class RecordingAgent:
        def run_conversation(self, received, **kwargs):
            self.received = received
            return {
                "final_response": final_text, "completed": True, "api_calls": 1,
                "messages": [{"role": "assistant", "content": final_text,
                              "reasoning_content": "fixture-unpersisted-reasoning"}],
            }

    agent = RecordingAgent()
    runtime = HermesNativeRuntime(
        factory=lambda **kwargs: agent, base_url="https://example.test", api_key="unit",
        model_name="fixture", provider="gpt",
    )
    result = runtime.run(
        role=VERIFIER_ROLE, instruction=instruction, session=AgentSession(), output_root=tmp_path,
    )
    assert agent.received == instruction
    assert result.completed and not result.errors
    assert result.final_text == final_text
    assert result.payload == json.loads(final_text)
    trace_text = (tmp_path / "private/agent_trace.json").read_text(encoding="utf-8")
    trace = json.loads(trace_text)
    assert trace["instruction"].endswith(feedback)
    assert len(trace["instruction"]) > 4096
    assert "[truncated]" not in trace["instruction"]
    assert trace["instruction_sha256"] == hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    assert trace["privacy"]["instruction_sha256_basis"] == "original_utf8_before_privacy_filtering"
    assert trace["privacy"]["private_thinking_reasoning"] == "omitted"
    assert "fixture-secret-value" not in trace_text
    assert "fixture-private-thought" not in trace_text
    assert "fixture-unpersisted-reasoning" not in trace_text
    if private_prefix:
        assert trace["instruction"].startswith("api_key=<redacted>\n\n")
    else:
        assert trace["instruction"] == instruction
