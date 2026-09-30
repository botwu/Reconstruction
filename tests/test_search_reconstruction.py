"""检索环境保留原始返回，并隔离原轨迹答案。"""

import ast
import base64
import hashlib
import json
import os
import subprocess
import sys

import pytest

from traceforge.reconstruction.search_environment import captured_evidence
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


def test_search_failure_is_recorded_and_not_marked_ready(tmp_path, monkeypatch):
    def fail(_query):
        raise RuntimeError("网络不可用")

    tools = SearchTools(tmp_path)
    monkeypatch.setattr(tools, "_search", fail)
    result = tools.search("轨道交通")
    assert result["success"] is False
    assert tools.ready() is False
    assert json.loads((tmp_path / "calls.jsonl").read_text())["success"] is False



@pytest.mark.parametrize("query_success", [True, False])
def test_empty_search_result_is_not_a_transport_failure(tmp_path, monkeypatch, query_success):
    tools = SearchTools(tmp_path)

    def search(_query):
        if not query_success:
            raise RuntimeError("检索请求失败")
        return [], "empty-result-hash"

    monkeypatch.setattr(tools, "_search", search)
    monkeypatch.setattr(tools, "_fetch", lambda url: {
        "success": True, "url": url, "text": "已知地址的真实正文",
    })
    result = tools.search("精确符号")
    tools.open("https://example.org/source")
    assert result["success"] is query_success
    assert tools.ready() is query_success


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


def test_cli_pagination_reads_saved_page_in_a_new_process(tmp_path, monkeypatch):
    from traceforge.reconstruction import search_tools

    tools = SearchTools(tmp_path)
    monkeypatch.setattr(tools, "_fetch", lambda url: {
        "url": url, "text": "abcdef", "success": True, "raw_sha256": "original-hash",
    })
    tools.open("https://example.invalid/page", limit=3)
    result = subprocess.run([
        sys.executable, search_tools.__file__, "--output-root", str(tmp_path),
        "open", "https://example.invalid/page", "--offset", "3", "--limit", "3",
    ], capture_output=True, text=True, check=True)
    page = json.loads(result.stdout)
    assert page["text"] == "def"
    assert page["raw_sha256"] == "original-hash"
    assert page["next_offset"] is None
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 2


def test_cli_returns_failure_when_search_credentials_are_missing(tmp_path):
    from traceforge.reconstruction import search_tools

    result = subprocess.run([
        sys.executable, search_tools.__file__, "--output-root", str(tmp_path),
        "search", "原任务关键词",
    ], capture_output=True, text=True, env={
        **os.environ, "SERPER_API_KEY": "", "JINA_API_KEY": "",
        "TRACEFORGE_SEARCH_CONFIG": str(tmp_path / "missing-config.json"),
    })
    assert result.returncode == 1
    assert json.loads(result.stdout)["success"] is False
    assert json.loads((tmp_path / "calls.jsonl").read_text())["success"] is False


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
    from traceforge.reconstruction.pipeline import run_reconstruction
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
    result = run_reconstruction(
        agent=agent,
        output_root=tmp_path,
        source={"domain_route": "retrieval", "raw_session": {"messages": []}},
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
        "schema_version": "traceforge.search-environment.v4",
        "status": "READY", "task": {"task_id": "q1", "task_instruction": "梳理文献"},
        "captures": [{"evidence_ref_id": "captured:0", "result_text": "原始返回"}],
        "live_references": [{"source_mode": "live_page", "text": "完整页面", "raw_sha256": "h",
                             "url": "https://example.org/source.py", "title": "来源正文",
                             "source_ref": "v1", "content_kind": "source_file"}],
        "context_note": "历史内容是线索", "limitations": [],
    }
    result = run_search_rollouts(
        environment=environment, rollout_agent=SimpleNamespace(run=solve, model_name="fixture"),
        output_root=tmp_path, rollout_trials=1,
    )
    evidence = calls[0]["session"].evidence
    assert evidence[0] == environment["captures"][0]
    assert json.loads(evidence[1]["result_text"])["text"] == "完整页面"
    from traceforge.reconstruction.agents.session import execute_tool

    index = json.loads(execute_tool("list_evidence", {}, calls[0]["session"]))
    assert index[0]["evidence_ref_id"] == "captured:0"
    assert "url" not in index[0]
    assert index[1]["url"] == "https://example.org/source.py"
    assert index[1]["source_ref"] == "v1"
    assert index[1]["content_kind"] == "source_file"
    assert index[1]["title"] == "来源正文"
    assert "完整页面" not in json.dumps(index, ensure_ascii=False)
    assert len(environment["captures"]) == 1
    assert result["status"] == "ROLLOUT_COMPLETED"
    assert result["rollouts"][0]["acceptance"] == "NOT_ASSESSED"


@pytest.mark.parametrize("missing,expected", [([], "ENVIRONMENT_READY"), (["必需的私有原文"], "BLOCKED")])
def test_optional_preferences_do_not_replace_required_input_gate(tmp_path, monkeypatch, missing, expected):
    from types import SimpleNamespace

    from traceforge.reconstruction import search_environment

    network = SimpleNamespace(ready=lambda: True, search=None, open=None, calls=[], pages={})
    monkeypatch.setattr(search_environment, "SearchTools", lambda root: network)
    feedback = {"missing_inputs": ["上一轮缺少实现正文"]}
    def complete(**kwargs):
        assert json.loads(kwargs["instruction"])["reconstruction_feedback"] == feedback
        return SimpleNamespace(
            payload={"status": "READY", "requires_live_web": True, "retrieval_reason": "任务需要公开文献",
                     "reference_event_indices": [], "context_note": "已恢复任务",
                     "limitations": ["未指定篇幅，按中文综述处理"], "missing_inputs": missing},
            errors=[], completed=True,
        )
    agent = SimpleNamespace(run=complete)
    result = search_environment.run_search_task(
        source={"raw_session": {"messages": []}},
        task={"task_id": "q1", "task_instruction": "查找公开来源并概述。"},
        agent=agent, output_root=tmp_path, initial_feedback=feedback,
    )
    assert result["status"] == expected
    environment = json.loads((tmp_path / "environment.json").read_text())
    assert environment["missing_inputs"] == missing
    assert environment["limitations"] == ["未指定篇幅，按中文综述处理"]


@pytest.mark.parametrize("repair", [True, False])
def test_unknown_reference_feedback_keeps_context_and_stops_without_progress(tmp_path, monkeypatch, repair):
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
            "requires_live_web": True, "retrieval_reason": "查找公开来源",
            "status": "READY", "excluded_events": [] if repair and len(sessions) > 1 else [
                {"event_index": 99, "reason": "原始事件中不存在的索引"}],
            "context_references": [{"message_index": 0, "used_by_user_message_index": 1}],
            "missing_inputs": [],
        })

    outcome = search_environment.run_search_task(
        source={"raw_session": {"messages": [
            {"role": "assistant", "content": "选题1：原题目"},
            {"role": "user", "content": "选题1投哪个口"},
            {"role": "assistant", "content": "不能交给 solver 的原答案"},
            {"role": "user", "content": "另一个独立任务"},
        ]}, "tool_timeline": [
            {"name": "search", "result_text": "原始来源"}, {"name": "search", "pending": True},
        ]}, task={"task_id": "q1", "task_instruction": "选题1投哪个口？",
                  "source_task": {"message_indices": [1]}},
        agent=SimpleNamespace(run=run), output_root=tmp_path,
    )
    assert len(sessions) == 2
    assert outcome["status"] == ("ENVIRONMENT_READY" if repair else "BLOCKED")
    assert ("SEARCH_COMPLETION_NO_PROGRESS" in outcome["errors"]) is not repair
    environment = json.loads((tmp_path / "environment.json").read_text())
    assert environment["context_messages"] == ([{
        "message_index": 0, "used_by_user_message_index": 1,
        "role": "assistant", "content": "选题1：原题目",
    }] if repair else [])
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
            "requires_live_web": True, "retrieval_reason": "查找公开来源",
            "status": "READY", "reference_event_indices": [], "missing_inputs": [],
        })

    result = search_environment.run_search_task(
        source={"raw_session": {"messages": []}},
        task={"task_id": "q", "task_instruction": "查找公开来源并概述。"},
        agent=SimpleNamespace(run=run), output_root=tmp_path,
    )
    assert len(attempts) == 2
    assert result["status"] == ("ENVIRONMENT_READY" if perform_check else "BLOCKED")


def test_local_evidence_search_can_resume_and_read_historical_file_coordinates():
    from traceforge.reconstruction.agents.session import AgentSession, execute_tool

    session = AgentSession(evidence=[{
        "evidence_ref_id": "captured:0", "result_text": "needle " * 12,
        "session_parse": {"reference_file_ops": [
            {"kind": "read", "path": None, "source_path": "HEAD^:removed.py",
             "content": "class Reference:\r\n    pass\r\n", "line_numbers": [4, 5]},
        ]},
    }])
    first = json.loads(execute_tool("search_evidence", {"query": "needle"}, session))
    assert len(first["matches"]) == 10 and first["next_offset"] == 10
    second = json.loads(execute_tool("search_evidence", {"query": "needle", "offset": 10}, session))
    assert len(second["matches"]) == 2 and second["next_offset"] is None
    read = json.loads(execute_tool("read_evidence", {"id": "captured:0", "path": "HEAD^:removed.py"}, session))
    assert read["observations"][0]["content"] == "class Reference:\r\n    pass\r\n"
    assert read["observations"][0]["line_numbers"] == [4, 5]
    assert execute_tool("search_evidence", {"query": ""}, session).startswith("error:")


@pytest.mark.parametrize("read_source", [True, False])
def test_local_search_requires_actual_corpus_reading_without_public_web(tmp_path, monkeypatch, read_source):
    from types import SimpleNamespace

    from traceforge.reconstruction import search_environment
    from traceforge.reconstruction.agents.session import execute_tool

    def no_network(*args, **kwargs):
        pytest.fail("本地代码检索不需要网络验证")

    network = SimpleNamespace(ready=no_network, search=no_network, open=no_network, calls=[], pages={})
    monkeypatch.setattr(search_environment, "SearchTools", lambda root: network)

    def run(**kwargs):
        if read_source:
            session = kwargs["session"]
            args = {"id": "captured:0"}
            value = execute_tool("read_evidence", args, session)
            session.tool_events.append({"name": "read_evidence", "arguments": args,
                                        "ok": not value.startswith("error:")})
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "READY", "requires_live_web": False, "retrieval_reason": "只读比较已捕获源码",
            "reference_event_indices": [0], "missing_inputs": [],
        })

    result = search_environment.run_search_task(
        source={"domain_route": "retrieval", "raw_session": {"messages": []},
                "tool_timeline": [{"name": "Read", "result_text": "class Reference: pass"}]},
        task={"task_id": "code-search", "task_instruction": "比较源码，引用路径和行号。"},
        agent=SimpleNamespace(run=run), output_root=tmp_path,
    )
    assert result["status"] == ("ENVIRONMENT_READY" if read_source else "BLOCKED")
    environment = json.loads((tmp_path / "environment.json").read_text())
    assert environment["source_mode"] == "captured_references"
    assert "web_search" not in environment["tools"]


@pytest.mark.parametrize("url", [
    "https://raw.githubusercontent.com/example/project/abc123/src/module.py",
    "https://github.com/example/project/blob/abc123/src/module.py",
])
@pytest.mark.parametrize("valid_hash", [True, False])
def test_github_source_preserves_bytes_and_rejects_corruption(tmp_path, monkeypatch, url, valid_hash):
    original = b"# header\r\nclass Example:\r\n    value = 3\r\n"
    blob = hashlib.sha1(b"blob " + str(len(original)).encode() + b"\0" + original).hexdigest()
    api = {"type": "file", "encoding": "base64", "size": len(original),
           "sha": blob if valid_hash else "0" * 40,
           "content": base64.encodebytes(original).decode()}
    tools = SearchTools(tmp_path)
    tools._fetch_provider = "serper"
    tools._serper_key = "test-only"
    requests = []

    def respond(request):
        requests.append(json.loads(request.data)["url"])
        return {"text": json.dumps(api)}, "provider-response-sha"

    monkeypatch.setattr(tools, "_response", respond)
    monkeypatch.setattr("traceforge.reconstruction.search_tools._public_url", lambda value: None)
    result = tools.open(url)
    assert requests == ["https://api.github.com/repos/example/project/contents/src/module.py?ref=abc123"]
    assert result["success"] is valid_hash
    if valid_hash:
        assert result["text"].encode() == original
        assert result["content_sha256"] == hashlib.sha256(original).hexdigest()
        assert result["git_blob_sha1"] == blob
        assert isinstance(ast.parse(result["text"]).body[0], ast.ClassDef)
        assert result["url"] == url
    else:
        assert url not in tools.pages
        assert "哈希" in result["error"]
