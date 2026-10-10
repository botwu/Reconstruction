"""完整原轨迹主动交付给作者；角色边界不改变原消息与索引。"""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from traceforge.reconstruction import search_environment, session_source
from traceforge.reconstruction.agents import COMPLETION_REPLAYED_ROLE, AgentSession
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.researcher import ReconstructionRuntime


@pytest.fixture
def raw_session():
    return {
        "tools": [
            {
                "type": "function",
                "function": {"name": "native_tool", "parameters": {"type": "object"}},
            }
        ],
        "metadata": {"harness": "fixture", "timestamp": "2026-10-01T00:00:00Z"},
        "messages": [
            {"role": "system", "content": "历史系统原文", "scope": "original"},
            {"role": "developer", "content": [{"type": "text", "text": "历史工具定义"}]},
            {"role": "user", "content": "原始任务", "message_index": 99},
            {
                "role": "assistant",
                "content": "执行意图",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "native_tool", "arguments": '{"path":"src/a.py"}'},
                    }
                ],
                "extra": {"child_agent": "worker"},
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "原始正文\r\n",
                "is_error": False,
            },
            {"role": "assistant", "content": "最后用户之后的历史回答"},
        ],
    }


def assert_original_session(view, raw):
    assert view["session_fields"] == {key: value for key, value in raw.items() if key != "messages"}
    assert view["messages"] == [
        {"message_index": index, "message": message}
        for index, message in enumerate(raw["messages"])
    ]


def test_terminal_author_receives_complete_indexed_session(tmp_path, raw_session):
    original = copy.deepcopy(raw_session)
    native = Mock(model_name="fixture")
    native.run.return_value = AgentResult(
        role="completion", backend="fixture", completed=True, payload={}
    )
    runtime = ReconstructionRuntime(
        native, source={"raw_session": raw_session}, task={"task_id": "t"}, runtime_factory=Mock()
    )
    runtime.agent = native
    runtime.run(
        role=COMPLETION_REPLAYED_ROLE,
        instruction="原文应在替换后保留",
        session=AgentSession(),
        output_root=tmp_path,
    )
    instruction = native.run.call_args.kwargs["instruction"]
    source_line = next(
        line for line in instruction.splitlines() if line.startswith("SOURCE_SESSION=")
    )
    assert_original_session(json.loads(source_line.removeprefix("SOURCE_SESSION=")), raw_session)
    assert raw_session == original


def test_search_author_receives_tail_tools_and_top_level_fields(tmp_path, monkeypatch, raw_session):
    network = SimpleNamespace(search=None, open=None, pages={}, calls=[], ready=lambda: False)
    monkeypatch.setattr(search_environment, "SearchTools", lambda _: network)
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            completed=True,
            errors=[],
            payload={
                "status": "REVIEW",
                "requires_live_web": False,
                "retrieval_reason": "需要先恢复原始材料",
                "missing_inputs": ["固定停止，检查作者输入"],
                "context_references": [],
            },
        )

    search_environment.run_search_task(
        source={"raw_session": raw_session},
        task={"task_id": "t", "source_task": {"message_indices": [2]}},
        agent=SimpleNamespace(run=run),
        output_root=tmp_path,
    )
    assert len(calls) == 1
    prompt = json.loads(calls[0]["instruction"])
    assert_original_session(prompt["SOURCE_SESSION"], raw_session)
    assert prompt["task_last_user_message_index"] == 2
    assert "conversation_messages" not in prompt
    assert "SOURCE_SYSTEM_MESSAGES" not in prompt


@pytest.mark.parametrize(
    "raw",
    [
        {"messages": "不能把字符串当消息列表"},
        {"messages": [{"role": "user", "content": "有效"}, "不能静默丢掉的非法消息"]},
    ],
)
def test_indexed_session_rejects_unaddressable_source(raw):
    with pytest.raises(session_source.ReconstructionSourceError, match="messages"):
        session_source.indexed_session(raw)


def test_indexed_session_empty_fixture_is_explicit():
    assert session_source.indexed_session({}) == {"session_fields": {}, "messages": []}


def test_terminal_continuation_keeps_source_once_and_failure_resends(tmp_path, raw_session):
    native = Mock(model_name="fixture")
    native.run.return_value = AgentResult(
        role="completion", backend="fixture", completed=False, payload={}
    )
    runtime = ReconstructionRuntime(
        native, source={"raw_session": raw_session}, task={"task_id": "t"}, runtime_factory=Mock()
    )
    runtime.agent = native
    for index in (0, 1):
        runtime.run(
            role=COMPLETION_REPLAYED_ROLE,
            instruction="",
            session=AgentSession(),
            output_root=tmp_path / str(index),
        )
        assert "SOURCE_SESSION=" in native.run.call_args.kwargs["instruction"]
    first = native.run.call_args.kwargs["instruction"]
    runtime.conversation.messages = [{"role": "user", "content": first}]
    runtime.run(
        role=COMPLETION_REPLAYED_ROLE,
        instruction="",
        session=AgentSession(),
        output_root=tmp_path / "continued",
    )
    assert "SOURCE_SESSION=" not in native.run.call_args.kwargs["instruction"]
    assert runtime.conversation.messages[0]["content"] == first


def test_search_derived_body_without_original_is_not_counted_inline(tmp_path, monkeypatch):
    network = SimpleNamespace(search=None, open=None, pages={}, calls=[], ready=lambda: False)
    monkeypatch.setattr(search_environment, "SearchTools", lambda _: network)

    def run(**kwargs):
        return SimpleNamespace(
            completed=True,
            errors=[],
            payload={
                "status": "READY",
                "requires_live_web": False,
                "retrieval_reason": "核对本地原文",
                "missing_inputs": [],
                "context_references": [],
                "excluded_events": [],
            },
        )

    search_environment.run_search_task(
        source={
            "raw_session": {"messages": []},
            "tool_timeline": [
                {
                    "name": "read",
                    "result_text": "只有派生副本",
                    "tool_message_index": 0,
                    "session_parse": {
                        "file_ops": [
                            {
                                "kind": "read",
                                "path": "a.py",
                                "content": "只有派生副本",
                                "content_ref": {"block_index": 0, "start_line": 1, "end_line": 1},
                            }
                        ]
                    },
                }
            ],
        },
        task={"task_id": "t", "task_instruction": "核对可用来源及其原文边界"},
        agent=SimpleNamespace(run=run),
        output_root=tmp_path,
    )
    environment = json.loads((tmp_path / "environment.json").read_text())
    assert environment["author_evidence_access"]["inline_observations"] == []
    assert environment["author_evidence_access"]["inline_returns"] == []


def test_missing_session_object_is_not_treated_as_empty_fixture():
    with pytest.raises(session_source.ReconstructionSourceError, match="必须是对象"):
        session_source.indexed_session(None)


@pytest.mark.parametrize("bound_to_raw_return", [True, False])
def test_inline_search_return_needs_no_file_parse_or_duplicate_read(
    tmp_path, monkeypatch, bound_to_raw_return,
):
    network = SimpleNamespace(search=None, open=None, pages={}, calls=[], ready=lambda: False)
    monkeypatch.setattr(search_environment, "SearchTools", lambda _: network)
    calls = []
    source = {
        "raw_session": {"messages": [
            {"role": "tool", "tool_call_id": "search-1", "content": "真实搜索返回：题名和摘要"},
        ]},
        "tool_timeline": [{
            "name": "web_search", "call_id": "search-1",
            "result_text": "真实搜索返回：题名和摘要",
            "tool_message_index": 0 if bound_to_raw_return else 7,
            "session_parse": {"reason": "检索结果", "file_ops": []},
        }],
    }

    def run(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "READY", "requires_live_web": False,
            "retrieval_reason": "核对已捕获的搜索返回",
            "excluded_events": [], "context_references": [], "missing_inputs": [],
            "requirement_coverage": [{
                "obligation_id": "inspect", "evidence_ref_ids": ["captured:0"],
                "reason": "任务所需原始检索片段已经直接提供",
            }],
        })

    outcome = search_environment.run_search_task(
        source=source,
        task={"task_id": "search-inline", "task_instruction": "核对原始搜索返回",
              "acceptance_obligations": [{"id": "inspect"}]},
        agent=SimpleNamespace(run=run), output_root=tmp_path,
    )
    assert outcome["status"] == ("ENVIRONMENT_READY" if bound_to_raw_return else "BLOCKED")
    if bound_to_raw_return:
        assert len(calls) == 1
        access = json.loads((tmp_path / "environment.json").read_text())["author_evidence_access"]
        assert access["inline_returns"] == [{
            "evidence_ref_id": "captured:0", "tool_message_index": 0,
        }]
        assert access["read_calls"] == []
