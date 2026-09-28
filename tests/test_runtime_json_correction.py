"""模型格式纠正必须有界、沿用原会话并保留原始结果。"""

import hashlib
import json
from pathlib import Path

import pytest

from traceforge.reconstruction.agents import INTENT_ROLE, AgentSession
from traceforge.reconstruction.agents.runtime import HermesNativeRuntime

BROKEN = (
    '{"task_instruction":"review","response_contract":{"checks":['
    '{"kind":"basic_summary","obligation_id":"o2","match_report":true"},'
    '{"kind":"acceptance_report","obligation_id":"o2"}]}}'
)
CORRECTED = (
    '{"task_instruction":"review","response_contract":{"checks":['
    '{"kind":"basic_summary","obligation_id":"o2","match_report":true},'
    '{"kind":"acceptance_report","obligation_id":"o2"}]}}'
)


class FormatAgent:
    def __init__(self, outputs, *, completed=True):
        self.outputs = outputs
        self.completed = completed
        self.calls = []
        self.history = [
            {"role": "user", "content": "recover the complete task"},
            {"role": "assistant", "content": outputs[0]},
        ]

    def run_conversation(self, instruction, system_message=None, task_id=None, conversation_history=None):
        self.calls.append((instruction, system_message, task_id, conversation_history))
        if len(self.calls) == 2:
            assert conversation_history == self.history
            assert self.tools == []
            assert self.max_iterations == 1
        return {"final_response": self.outputs[len(self.calls) - 1], "completed": self.completed,
                "messages": self.history, "api_calls": 1}


def run_agent(tmp_path: Path, agent: FormatAgent):
    factories = []

    def factory(**kwargs):
        factories.append(kwargs)
        return agent

    runtime = HermesNativeRuntime(factory=factory, base_url="https://example.test", api_key="unit",
                                  model_name="fixture", provider="gpt")
    result = runtime.run(role=INTENT_ROLE, instruction="recover the complete task",
                         session=AgentSession(), output_root=tmp_path)
    assert len(factories) == 1
    return result, json.loads((tmp_path / "private/agent_trace.json").read_text())


def test_invalid_json_is_corrected_once_in_same_role_and_both_outputs_remain(tmp_path):
    agent = FormatAgent([BROKEN, CORRECTED])
    result, trace = run_agent(tmp_path, agent)
    assert result.completed and result.errors == []
    assert result.payload == json.loads(CORRECTED)
    assert len(agent.calls) == 2
    assert agent.calls[0][1:3] == agent.calls[1][1:3] == (INTENT_ROLE.identity, INTENT_ROLE.name)
    assert "INVALID_JSON" in agent.calls[1][0]
    assert len(result.turns) == len(trace["turns"]) == 2
    assert trace["turns"][0]["output_error"] == "INVALID_JSON"
    assert [turn["final_response"] for turn in trace["turns"]] == [BROKEN, CORRECTED]
    assert trace["turns"][0]["response_sha256"] == hashlib.sha256(BROKEN.encode()).hexdigest()


def test_still_invalid_after_one_correction_is_explicit_failure(tmp_path):
    agent = FormatAgent([BROKEN, BROKEN])
    result, trace = run_agent(tmp_path, agent)
    assert not result.completed
    assert result.errors == ["INVALID_JSON"]
    assert result.payload == {}
    assert len(agent.calls) == len(trace["turns"]) == 2


@pytest.mark.parametrize("output,completed,expected_error", [
    (BROKEN, False, "INVALID_JSON"),
    ("Connection error: failed", True, "MODEL_CONNECTION_ERROR"),
])
def test_incomplete_or_infrastructure_failure_does_not_start_format_retry(tmp_path, output, completed, expected_error):
    agent = FormatAgent([output], completed=completed)
    result, _ = run_agent(tmp_path, agent)
    assert not result.completed
    assert expected_error in result.errors
    assert len(agent.calls) == 1
