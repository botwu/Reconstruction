"""同一研究者跨阶段保留实际消息；独立模型运行不得获得其私有上下文。"""

import json

from traceforge.reconstruction.agents import AgentSession, INTENT_ROLE
from traceforge.reconstruction.agents.runtime import HermesNativeRuntime
from traceforge.reconstruction.agents.session import AgentConversation


def test_researcher_continues_history_but_independent_session_starts_empty(tmp_path):
    calls = []

    class Native:
        def run_conversation(self, instruction, **kwargs):
            history = kwargs.get("conversation_history") or []
            calls.append(list(history))
            return {"messages": [*history, {"role": "user", "content": instruction},
                                 {"role": "assistant", "content": '{"ok":true}'}],
                    "api_calls": 1, "completed": True, "final_response": '{"ok":true}'}

    runtime = HermesNativeRuntime(factory=lambda **_: Native(), base_url="https://example.test",
                                  api_key="unit", model_name="fixture", provider="gpt")
    conversation = AgentConversation()
    for index, (text, state) in enumerate([
        ("原始证据及构建", conversation), ("真实失败，继续修复", conversation), ("独立求解", None),
    ]):
        result = runtime.run(role=INTENT_ROLE, instruction=text,
                             session=AgentSession(conversation=state), output_root=tmp_path / str(index))
        assert result.completed
    assert calls[0] == [] and calls[2] == []
    assert calls[1][0]["content"] == "原始证据及构建"
    saved = json.loads((tmp_path / "1/private/conversation.json").read_text())
    assert saved == conversation.messages
    assert [m["content"] for m in saved if m["role"] == "user"] == ["原始证据及构建", "真实失败，继续修复"]


def test_failed_request_does_not_erase_previous_conversation(tmp_path):
    class Failing:
        def run_conversation(self, *args, **kwargs):
            raise ConnectionError("gateway unavailable")

    history = [{"role": "user", "content": "已有原始证据"}]
    conversation = AgentConversation(messages=list(history))
    runtime = HermesNativeRuntime(factory=lambda **_: Failing(), base_url="https://example.test",
                                  api_key="unit", model_name="fixture", provider="gpt")
    result = runtime.run(role=INTENT_ROLE, instruction="继续", session=AgentSession(conversation=conversation),
                         output_root=tmp_path)
    assert not result.completed and conversation.messages == history


def test_search_has_explicit_output_budget_without_changing_other_roles(tmp_path):
    from traceforge.reconstruction.search_environment import SEARCH_SOLVER_ROLE

    calls = []

    class Native:
        def run_conversation(self, instruction, **kwargs):
            return {"completed": True, "final_response": '{"ok":true}', "api_calls": 1}

    def factory(**kwargs):
        calls.append(kwargs)
        return Native()

    runtime = HermesNativeRuntime(factory=factory, base_url="https://example.test",
                                  api_key="unit", model_name="fixture", provider="gpt")
    for index, role in enumerate([INTENT_ROLE, SEARCH_SOLVER_ROLE]):
        assert runtime.run(role=role, instruction="任务", session=AgentSession(),
                           output_root=tmp_path / str(index)).completed
    assert "max_tokens" not in calls[0]
    assert calls[1]["max_tokens"] == 16384
