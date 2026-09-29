"""初始请求摘要绑定原字节；业务字段不脱敏，私有思考仍按既有视图约定过滤。"""

import hashlib
import json

import pytest

from traceforge.reconstruction.agents import VERIFIER_ROLE, AgentSession
from traceforge.reconstruction.agents.runtime import HermesNativeRuntime, write_agent_trace


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
    assert "fixture-private-thought" not in trace_text
    assert "fixture-unpersisted-reasoning" not in trace_text
    if private_prefix:
        assert trace["instruction"].startswith("api_key=fixture-secret-value\n\n")
    else:
        assert trace["instruction"] == instruction


def test_tool_trace_preserves_business_keys_and_original_text(tmp_path):
    event = {
        "name": "read_file",
        "arguments": {"max_output_tokens": 4096, "key_column": "name", "token": "标记"},
        "result_preview": "api_key=fixture-original-value\n",
    }
    path = write_agent_trace(
        tmp_path, role=VERIFIER_ROLE, backend="fixture", instruction="原始输入",
        turns=[], final_text='{"token": "原始输出"}', model_name="fixture",
        tool_events=[event],
    )
    saved = json.loads(path.read_text())
    assert saved["tool_events"] == [event]
    assert saved["final_text"] == '{"token": "原始输出"}'
