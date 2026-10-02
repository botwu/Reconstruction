"""研究员恢复须保留真实缓存与会话，不把历史成功当作当前在线能力。"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from traceforge.reconstruction.agents.session import AgentConversation, AgentSession, execute_tool
from traceforge.reconstruction.search_tools import SearchTools
from traceforge.reconstruction.session_source import indexed_session


def recorded_network(root, monkeypatch):
    tools = SearchTools(root)
    tools._fetch_provider = "serper"
    tools._serper_key = "test-only"
    raw = json.dumps({
        "text": "前段正文与未读尾部", "metadata": {"title": "真实资料"},
        "jsonld": {"author": [{"name": "甲"}, {"name": "乙"}]},
    }, ensure_ascii=False).encode()
    digest = hashlib.sha256(raw).hexdigest()

    def response(_request):
        (root / f"{digest}.raw").write_bytes(raw)
        return json.loads(raw), digest

    monkeypatch.setattr(tools, "_response", response)
    monkeypatch.setattr(tools, "_fetch_pdf", lambda _url: None)
    monkeypatch.setattr("traceforge.reconstruction.search_tools._public_url", lambda _url: None)
    session = AgentSession(web_open_handler=tools.open)
    arguments = {"url": "https://example.org/paper", "offset": 2, "limit": 3}
    result = execute_tool("web_open", arguments, session)
    events = [{
        "name": "web_open", "tool_call_id": "actual-open", "arguments": arguments,
        "ok": True, "result": result,
        "result_sha256": hashlib.sha256(result.encode()).hexdigest(),
    }]
    return tools, events, digest


def test_restored_cache_preserves_unread_body_jsonld_and_actual_range(tmp_path, monkeypatch):
    original, events, _ = recorded_network(tmp_path / "old", monkeypatch)
    resumed = SearchTools(tmp_path / "new")
    resumed.restore(original.root, origin="solver:trial-01", tool_results=events)

    assert resumed.pages["https://example.org/paper"]["text"] == "前段正文与未读尾部"
    result = resumed.open("https://example.org/paper", offset=5, limit=8)
    assert result["text"] == "未读尾部"
    assert result["jsonld"] == original.pages["https://example.org/paper"]["jsonld"]
    assert resumed.calls[0]["offset"] == 2 and resumed.calls[0]["next_offset"] == 5
    assert resumed.calls[0]["restored"] is True
    assert resumed.ready() is False


@pytest.mark.parametrize("damage", ["raw", "body", "unbound", "result_hash"])
def test_invalid_restored_source_is_not_silently_accepted(tmp_path, monkeypatch, damage):
    original, events, digest = recorded_network(tmp_path / "old", monkeypatch)
    if damage == "raw":
        (original.root / f"{digest}.raw").write_bytes(b"corrupt")
    elif damage == "body":
        page = next(original.root.glob("*.json"))
        data = json.loads(page.read_text())
        data["text"] += "伪造正文"
        page.write_text(json.dumps(data))
    elif damage == "unbound":
        events = []
    else:
        events[0]["result_sha256"] = "0" * 64
    resumed = SearchTools(tmp_path / "new")
    with pytest.raises(ValueError):
        resumed.restore(original.root, origin="solver:trial-01", tool_results=events)
    assert resumed.pages == {} and resumed.calls == []


def test_restored_success_cannot_mask_current_failure(tmp_path, monkeypatch):
    original, events, _ = recorded_network(tmp_path / "old", monkeypatch)
    resumed = SearchTools(tmp_path / "new")
    resumed.calls.extend([
        {"tool": "web_search", "success": True},
        {"tool": "web_open", "success": False, "error": "Not enough credits"},
    ])
    resumed.restore(original.root, origin="solver:trial-01", tool_results=events)
    assert resumed.ready() is False


def test_checkpoint_resume_is_portable_and_does_not_duplicate_source(tmp_path, monkeypatch):
    from traceforge.reconstruction import search_environment

    original, _, _ = recorded_network(tmp_path / "old-web", monkeypatch)
    source = {"line_sha256": "fixed-source", "raw_session": {"messages": []}, "tool_timeline": []}
    task = {"task_id": "q1", "task_instruction": "分析原资料"}
    session = AgentSession(conversation=AgentConversation(messages=[
        {"role": "user", "content": json.dumps({
            "SOURCE_SESSION": indexed_session(source["raw_session"]),
        })},
    ]))
    checkpoint = search_environment.save_search_checkpoint(
        source=source, task=task, session=session, network=original,
        output_root=tmp_path / "original",
    )
    shutil.copytree(checkpoint.parent, tmp_path / "moved")

    def complete(**kwargs):
        assert kwargs["session"].conversation.messages == session.conversation.messages
        instruction = json.loads(kwargs["instruction"])
        assert "SOURCE_SESSION" not in instruction
        assert instruction["restored_reference_catalog"] == [{
            "url": "https://example.org/paper", "title": "真实资料",
            "content_kind": "page_text", "retrieved_at": original.pages[
                "https://example.org/paper"]["retrieved_at"], "total_chars": 9,
        }]
        assert "text" not in instruction["restored_reference_catalog"][0]
        assert kwargs["network"].pages["https://example.org/paper"]["text"] == "前段正文与未读尾部"
        assert kwargs["network"].ready() is False
        return {"errors": ["明确停止"], "missing_inputs": []}

    monkeypatch.setattr(search_environment, "_complete_search_environment", complete)
    result = search_environment.run_search_task(
        source=source, task=task, agent=object(), output_root=tmp_path / "resumed",
        checkpoint_path=tmp_path / "moved" / checkpoint.name,
    )
    assert result["status"] == "BLOCKED"
    checkpoint = Path(result["researcher_checkpoint"])
    assert checkpoint.name == "checkpoint.json"
    assert checkpoint.parent.parent.name == "researcher-checkpoint"


@pytest.mark.parametrize("damage", ["source", "task", "conversation", "traversal", "source_absent"])
def test_checkpoint_identity_or_bytes_failure_never_restarts_empty(
    tmp_path, monkeypatch, damage,
):
    from traceforge.reconstruction import search_environment

    tools, _, _ = recorded_network(tmp_path / "old-web", monkeypatch)
    source = {"line_sha256": "fixed-source", "raw_session": {"messages": []}, "tool_timeline": []}
    task = {"task_id": "q1", "task_instruction": "分析原资料"}
    checkpoint = search_environment.save_search_checkpoint(
        source=source, task=task, session=AgentSession(conversation=AgentConversation(messages=[
            {"role": "user", "content": json.dumps({
                "SOURCE_SESSION": indexed_session(source["raw_session"]),
            })},
        ])), network=tools, output_root=tmp_path,
    )
    if damage == "source":
        source["line_sha256"] = "other-source"
    elif damage == "task":
        task["task_instruction"] = "不同的任务"
    elif damage == "conversation":
        (checkpoint.parent / "conversation.json").write_text("[] ")
    elif damage == "source_absent":
        conversation = checkpoint.parent / "conversation.json"
        conversation.write_text("[]")
        data = json.loads(checkpoint.read_text())
        data["files"]["conversation.json"] = hashlib.sha256(conversation.read_bytes()).hexdigest()
        checkpoint.write_text(json.dumps(data))
    else:
        data = json.loads(checkpoint.read_text())
        data["files"]["../outside"] = "0" * 64
        checkpoint.write_text(json.dumps(data))
    monkeypatch.setattr(search_environment, "_complete_search_environment",
                        lambda **kwargs: pytest.fail("恢复错误不得从空状态重跑"))
    with pytest.raises(ValueError):
        search_environment.run_search_task(
            source=source, task=task, agent=object(), output_root=tmp_path / "new",
            checkpoint_path=checkpoint,
        )


def test_restore_requires_provenance_not_just_a_url_cache(tmp_path, monkeypatch):
    original, _, _ = recorded_network(tmp_path / "old", monkeypatch)
    with pytest.raises(ValueError, match="完整工具回执"):
        SearchTools(tmp_path / "new").restore(original.root, origin="unverified")


def test_same_url_different_body_is_a_clear_conflict(tmp_path, monkeypatch):
    original, events, _ = recorded_network(tmp_path / "old", monkeypatch)
    resumed = SearchTools(tmp_path / "new")
    resumed.pages["https://example.org/paper"] = {
        **original.pages["https://example.org/paper"], "text": "另一个版本正文",
    }
    with pytest.raises(ValueError, match="同一来源快照内容冲突"):
        resumed.restore(original.root, origin="solver:trial-01", tool_results=events)
    assert resumed.pages["https://example.org/paper"]["text"] == "另一个版本正文"
    assert resumed.calls == []


def test_repeated_checkpoint_restore_keeps_original_page_and_call_provenance(tmp_path, monkeypatch):
    from traceforge.reconstruction.search_environment import (
        _restore_search_checkpoint,
        save_search_checkpoint,
    )

    original, events, _ = recorded_network(tmp_path / "old", monkeypatch)
    tools = SearchTools(tmp_path / "active")
    tools.restore(original.root, origin="solver:trial-01", tool_results=events)
    source = {"line_sha256": "fixed-source", "raw_session": {"messages": []}}
    task = {"task_id": "q1"}
    session = AgentSession(conversation=AgentConversation(messages=[
        {"role": "user", "content": json.dumps({
            "SOURCE_SESSION": indexed_session(source["raw_session"]),
        })},
    ]))
    checkpoint = save_search_checkpoint(
        source=source, task=task, session=session, network=tools, output_root=tmp_path / "first",
    )
    second = SearchTools(tmp_path / "second")
    session.conversation = _restore_search_checkpoint(
        checkpoint, source=source, task=task, network=second)
    checkpoint = save_search_checkpoint(
        source=source, task=task, session=session, network=second, output_root=tmp_path / "third",
    )
    final = SearchTools(tmp_path / "last")
    _restore_search_checkpoint(checkpoint, source=source, task=task, network=final)
    provenance = final.pages["https://example.org/paper"]["cache_provenance"]
    assert [item["origin"] for item in provenance] == ["solver:trial-01", "researcher_checkpoint"]
    assert final.calls[0]["restored_from"] == "solver:trial-01"
    assert final.calls[0]["restore_history"] == ["solver:trial-01", "researcher_checkpoint"]
    assert final.calls[0]["tool_call_id"] == "actual-open"
    assert final.calls[0]["tool_result_sha256"] == events[0]["result_sha256"]


def test_interrupted_checkpoint_save_preserves_previous_snapshot(tmp_path, monkeypatch):
    from traceforge.reconstruction import search_environment

    network, _, _ = recorded_network(tmp_path / "web", monkeypatch)
    source = {"line_sha256": "fixed-source", "raw_session": {"messages": []}}
    task = {"task_id": "q1"}
    session = AgentSession(conversation=AgentConversation(messages=[
        {"role": "user", "content": json.dumps({
            "SOURCE_SESSION": indexed_session(source["raw_session"]),
        })},
    ]))
    first = search_environment.save_search_checkpoint(
        source=source, task=task, session=session, network=network, output_root=tmp_path,
    )
    original_files = {str(path.relative_to(first.parent)): path.read_bytes()
                      for path in first.parent.rglob("*") if path.is_file()}
    save = search_environment._save

    def interrupted(path, value):
        if path.name == "checkpoint.json":
            raise OSError("保存中断")
        save(path, value)

    monkeypatch.setattr(search_environment, "_save", interrupted)
    with pytest.raises(OSError, match="保存中断"):
        search_environment.save_search_checkpoint(
            source=source, task=task, session=session, network=network, output_root=tmp_path,
        )
    assert original_files == {
        str(path.relative_to(first.parent)): path.read_bytes()
        for path in first.parent.rglob("*") if path.is_file()
    }
    resumed = SearchTools(tmp_path / "resumed")
    search_environment._restore_search_checkpoint(first, source=source, task=task, network=resumed)
    assert resumed.pages["https://example.org/paper"]["text"] == "前段正文与未读尾部"


@pytest.mark.parametrize("tampered", [False, True])
def test_native_cli_cache_is_bound_to_complete_terminal_stdout(
    tmp_path, monkeypatch, tampered,
):
    from types import SimpleNamespace

    from traceforge.reconstruction.search_environment import _review_search_rollouts

    original, _, _ = recorded_network(tmp_path / "native-cache", monkeypatch)
    stdout = (original.root / "calls.jsonl").read_text()
    result = json.dumps({"output": stdout, "exit_code": 0}, ensure_ascii=False)
    event = {
        "name": "terminal", "tool_call_id": "native-terminal",
        "arguments": {"command": "traceforge-search open https://example.org/paper --limit 3"},
        "result": result, "ok": True,
        "result_sha256": hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
    }
    if tampered:
        event["result"] = result + "伪造"
    network = SearchTools(tmp_path / "researcher-web")

    def review(**kwargs):
        assert not tampered
        assert network.pages["https://example.org/paper"]["text"] == "前段正文与未读尾部"
        assert network.calls[0]["native_result_sha256"] == event["result_sha256"]
        assert network.calls[0]["native_tool_name"] == "terminal"
        return SimpleNamespace(completed=True, errors=[], payload={
            "decision": "COMPLETE", "requirements": [],
        })

    output = _review_search_rollouts(
        task={"acceptance_obligations": []},
        environment={"evidence_handoff": {}, "captures": [], "requirement_coverage": [],
                     "context_messages": []},
        agent=SimpleNamespace(run=review), session=AgentSession(), output_root=tmp_path,
        native_trials=[{
            "trial": "native-01", "tool_events": [event], "web_cache_root": str(original.root),
        }], network=network,
    )
    assert output["decision"] == ("BLOCKED" if tampered else "COMPLETE")
    if tampered:
        assert output["failure_kind"] == "TRACE_UNAVAILABLE"
