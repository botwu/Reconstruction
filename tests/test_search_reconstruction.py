"""检索环境保留原始返回，并隔离原轨迹答案。"""

import json

import pytest

from traceforge.reconstruction.search_environment import captured_evidence, select_captures
from traceforge.reconstruction.search_tools import SearchTools


def test_captures_preserve_results_without_original_answer():
    source = {
        "raw_session": {"messages": [{"role": "assistant", "content": "原答案"}]},
        "tool_timeline": [
            {"name": "arbitrary_harness", "arguments": {"q": "查询"},
             "result_blocks": [{"index": 0, "text": "完整证据\r\n"}],
             "assistant_message_index": 1, "tool_message_index": 2},
            {"name": "pending_tool", "pending": True},
        ],
    }
    records = captured_evidence(source)
    assert len(records) == 1
    assert records[0]["result_blocks"] == source["tool_timeline"][0]["result_blocks"]
    assert "原答案" not in json.dumps(records, ensure_ascii=False)
    assert select_captures(records, [0]) == records


@pytest.mark.parametrize("indices", [[1], [0, 0], [True], "0"])
def test_selection_rejects_missing_or_ambiguous_source(indices):
    with pytest.raises(ValueError):
        select_captures([{"event_index": 0}], indices)


def test_search_failure_is_recorded_and_not_marked_ready(tmp_path, monkeypatch):
    def fail(_query):
        raise RuntimeError("网络不可用")

    tools = SearchTools(tmp_path)
    monkeypatch.setattr(tools, "_search", fail)
    result = tools.search("轨道交通")
    assert result["success"] is False
    assert tools.ready() is False
    assert json.loads((tmp_path / "calls.jsonl").read_text())["success"] is False


@pytest.mark.parametrize("provider", ["jina", "serper"])
def test_provider_results_keep_raw_snapshot_reference(tmp_path, monkeypatch, provider):
    tools = SearchTools(tmp_path)
    tools._serper_key = "test-only"
    tools._jina_key = "test-only"
    tools._fetch_provider = provider
    body = ({"data": {"url": "https://example.org", "title": "来源", "content": "实际正文"}}
            if provider == "jina" else {"text": "实际正文", "metadata": {"title": "来源"}})
    responses = iter([
        ({"organic": [{"title": "来源", "link": "https://example.org", "snippet": "摘要"}]},
         "search-hash"),
        (body, "page-hash"),
    ])
    monkeypatch.setattr(tools, "_response", lambda request: next(responses))
    monkeypatch.setattr("traceforge.reconstruction.search_tools._public_url", lambda url: None)
    search = tools.search("原始任务关键词")
    page = tools.open(search["results"][0]["link"])
    assert search["raw_sha256"] == "search-hash"
    assert page["raw_sha256"] == "page-hash"
    assert page["provider"] == provider
    assert page["text"] == "实际正文"
    assert tools.ready()
    assert "test-only" not in (tmp_path / "calls.jsonl").read_text()


def test_page_pagination_reuses_same_snapshot(tmp_path, monkeypatch):
    tools = SearchTools(tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        return {"url": url, "text": "abcdef", "success": True}

    monkeypatch.setattr(tools, "_fetch", fetch)
    assert tools.open("https://example.org", offset=0, limit=3)["text"] == "abc"
    second = tools.open("https://example.org", offset=3, limit=3)
    assert second["text"] == "def"
    assert second["next_offset"] is None
    assert calls == ["https://example.org"]


def test_private_urls_are_not_public_sources(tmp_path):
    tools = SearchTools(tmp_path)
    result = tools.open("http://127.0.0.1/internal")
    assert result["success"] is False
    assert tools.ready() is False


@pytest.mark.parametrize("completed", [True, False])
def test_solver_plaintext_is_not_retried_as_invalid_json(tmp_path, completed):
    from hermes_fakes import FakeHermesAgent

    from traceforge.reconstruction.agents import AgentSession, build_hermes_runtime
    from traceforge.reconstruction.search_environment import SEARCH_SOLVER_ROLE

    class Solver(FakeHermesAgent):
        def run_conversation(self, instruction, **kwargs):
            return {"completed": completed, "final_response": "有来源的最终综述。", "api_calls": 1}

    runtime = build_hermes_runtime(
        model_name="test", factory=Solver, provider="gpt",
        base_url="https://model.example", api_key="test",
    )
    result = runtime.run(role=SEARCH_SOLVER_ROLE, instruction="按段落分析",
                         session=AgentSession(), output_root=tmp_path)
    assert result.completed is completed
    assert result.final_text == "有来源的最终综述。"
    assert len(result.turns) == 1


@pytest.mark.parametrize("execute_rollout", [False, True])
def test_search_entry_does_not_enter_file_replay_or_sandbox(tmp_path, monkeypatch, execute_rollout):
    from traceforge.reconstruction import agents, search_environment
    from traceforge.reconstruction.eligible_reconstruction import run_eligible_reconstruction
    from traceforge.reconstruction.verification import VerificationConfig

    agent = object()
    calls = []
    runtime_calls = []
    solver = object()

    def build_solver(**kwargs):
        runtime_calls.append(kwargs)
        return solver

    def run_search(**kwargs):
        calls.append(kwargs)
        return tmp_path / "manifest.json"

    monkeypatch.setattr(search_environment, "run_search_reconstruction", run_search)
    monkeypatch.setattr(agents, "build_hermes_runtime", build_solver)
    result = run_eligible_reconstruction(
        raw_line="{}", record=None, agent=agent, output_root=tmp_path,
        source_override={"domain_route": "retrieval", "raw_session": {"messages": []}},
        container_runtime_factory=lambda: pytest.fail("search 不应启动文件沙箱"),
        verification_config=VerificationConfig(
            harbor_root=tmp_path, model_name="author", rollout_model="claude/solver",
            execute_rollout=execute_rollout, hermes_home=tmp_path / "hermes-source",
        ),
    )
    assert result == tmp_path / "manifest.json"
    assert calls[0]["agent"] is agent
    assert calls[0]["rollout_agent"] is (solver if execute_rollout else None)
    assert len(runtime_calls) == int(execute_rollout)
    if execute_rollout:
        assert runtime_calls[0]["hermes_home"] == tmp_path / "hermes-source"


def test_ready_search_environment_can_resume_with_captured_and_live_evidence(tmp_path):
    from types import SimpleNamespace

    from traceforge.reconstruction.search_environment import run_search_rollouts

    calls = []

    def solve(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completed=True, errors=[], turns=[], final_text="实际回答")

    environment = {
        "status": "READY", "task": {"task_id": "q1", "task_instruction": "梳理文献"},
        "captures": [{"evidence_ref_id": "captured:0", "result_text": "原始返回"}],
        "live_references": [{"source_mode": "live_page", "text": "完整页面", "raw_sha256": "h"}],
        "context_note": "历史内容是线索", "limitations": [],
    }
    result = run_search_rollouts(
        environment=environment, rollout_agent=SimpleNamespace(run=solve, model_name="fixture"),
        output_root=tmp_path, rollout_trials=1,
    )
    evidence = calls[0]["session"].evidence
    assert evidence[0] == environment["captures"][0]
    assert json.loads(evidence[1]["result_text"])["text"] == "完整页面"
    assert len(environment["captures"]) == 1
    assert result["status"] == "ROLLOUT_COMPLETED"
    assert result["rollouts"][0]["acceptance"] == "NOT_ASSESSED"


@pytest.mark.parametrize("missing,expected", [([], "ENVIRONMENT_READY"), (["必需的私有原文"], "BLOCKED")])
def test_optional_preferences_do_not_replace_required_input_gate(tmp_path, monkeypatch, missing, expected):
    from types import SimpleNamespace

    from traceforge.reconstruction import search_environment

    network = SimpleNamespace(ready=lambda: True, search=None, open=None, calls=[], pages={})
    monkeypatch.setattr(search_environment, "SearchTools", lambda root: network)
    agent = SimpleNamespace(run=lambda **kwargs: SimpleNamespace(
        payload={"status": "READY", "reference_event_indices": [], "context_note": "已恢复任务",
                 "limitations": ["未指定篇幅，按中文综述处理"], "missing_inputs": missing},
        errors=[], completed=True,
    ))
    result = search_environment.run_search_task(
        source={"raw_session": {"messages": []}}, task={"task_id": "q1"},
        agent=agent, output_root=tmp_path,
    )
    assert result["status"] == expected
    environment = json.loads((tmp_path / "environment.json").read_text())
    assert environment["missing_inputs"] == missing
    assert environment["limitations"] == ["未指定篇幅，按中文综述处理"]


@pytest.mark.parametrize("repair", [True, False])
def test_pending_reference_feedback_keeps_context_and_stops_without_progress(tmp_path, monkeypatch, repair):
    from types import SimpleNamespace

    from traceforge.reconstruction import search_environment

    network = SimpleNamespace(ready=lambda: True, search=None, open=None, calls=[], pages={})
    monkeypatch.setattr(search_environment, "SearchTools", lambda root: network)
    sessions = []

    def run(**kwargs):
        sessions.append(kwargs["session"])
        prompt = json.loads(kwargs["instruction"])
        assert prompt["available_reference_event_indices"] == [0]
        if len(sessions) == 1:
            assert prompt["conversation_messages"] == [
                {"message_index": 0, "role": "assistant", "content": "选题1：原题目"},
                {"message_index": 1, "role": "user", "content": "选题1投哪个口"},
            ]
            assert prompt["task_last_user_message_index"] == 1
        if len(sessions) == 2:
            assert "validation_feedback" in prompt
            assert sessions[0] is sessions[1]
            assert sessions[1].conversation.messages == [{"role": "assistant", "content": "已查来源"}]
        sessions[-1].conversation.messages = [{"role": "assistant", "content": "已查来源"}]
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "READY", "reference_event_indices": [0] if repair and len(sessions) > 1 else [1],
            "context_note": "原 message 3 中的选题1是原题目", "missing_inputs": [],
        })

    outcome = search_environment.run_search_task(
        source={"raw_session": {"messages": [
            {"role": "assistant", "content": "选题1：原题目"},
            {"role": "user", "content": "选题1投哪个口"},
            {"role": "assistant", "content": "不能交给 solver 的原答案"},
            {"role": "user", "content": "另一个独立任务"},
        ]}, "tool_timeline": [
            {"name": "search", "result_text": "原始来源"}, {"name": "search", "pending": True},
        ]}, task={"task_id": "q1", "source_task": {"message_indices": [1]}},
        agent=SimpleNamespace(run=run), output_root=tmp_path,
    )
    assert len(sessions) == 2
    assert outcome["status"] == ("ENVIRONMENT_READY" if repair else "BLOCKED")
    assert ("SEARCH_COMPLETION_NO_PROGRESS" in outcome["errors"]) is not repair
    environment = json.loads((tmp_path / "environment.json").read_text())
    assert environment["context_note"] == "原 message 3 中的选题1是原题目"
    assert len(environment["captures"]) == int(repair)


@pytest.mark.parametrize("perform_check", [True, False])
def test_missing_live_query_returns_to_author_without_faking_readiness(tmp_path, monkeypatch, perform_check):
    from types import SimpleNamespace

    from traceforge.reconstruction import search_environment

    queries = []
    network = SimpleNamespace(ready=lambda: bool(queries), search=queries.append,
                              open=None, calls=[], pages={})
    monkeypatch.setattr(search_environment, "SearchTools", lambda root: network)
    attempts = []

    def run(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 2:
            feedback = json.loads(kwargs["instruction"])["validation_feedback"]
            assert any("web_search" in error for error in feedback)
            assert kwargs["session"] is attempts[0]["session"]
            if perform_check:
                kwargs["session"].web_search_handler("任务的公开来源")
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "READY", "reference_event_indices": [], "missing_inputs": [],
        })

    result = search_environment.run_search_task(
        source={"raw_session": {"messages": []}}, task={"task_id": "q"},
        agent=SimpleNamespace(run=run), output_root=tmp_path,
    )
    assert len(attempts) == 2
    assert result["status"] == ("ENVIRONMENT_READY" if perform_check else "BLOCKED")
