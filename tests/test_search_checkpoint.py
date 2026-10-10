"""研究员恢复须保留真实缓存与会话，不把历史成功当作当前在线能力。"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from traceforge.reconstruction.agents.session import AgentConversation, AgentSession, execute_tool
from traceforge.reconstruction.search_tools import SearchTools
from traceforge.reconstruction.session_source import indexed_session


def recorded_network(root, monkeypatch, *, markdown=None):
    tools = SearchTools(root)
    tools._fetch_provider = "serper"
    tools._serper_key = "test-only"
    raw = json.dumps({
        "text": "前段正文与未读尾部", "metadata": {"title": "真实资料"},
        "jsonld": {"author": [{"name": "甲"}, {"name": "乙"}]},
        **({"markdown": markdown} if markdown is not None else {}),
    }, ensure_ascii=False).encode()
    digest = hashlib.sha256(raw).hexdigest()

    def response(_request):
        (root / f"{digest}.raw").write_bytes(raw)
        return json.loads(raw), digest

    monkeypatch.setattr(tools, "_response", response)
    monkeypatch.setattr(tools, "_fetch_document", lambda _url: None)
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
def test_native_cli_stdout_and_collected_cache_are_bound_independently(
    tmp_path, monkeypatch, tampered,
):
    from types import SimpleNamespace

    from traceforge.reconstruction.search_environment import _review_search_rollouts

    original, trial = native_cache_trial(tmp_path, monkeypatch, "")
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
    trial["tool_events"] = [event]
    network = SearchTools(tmp_path / "researcher-web")

    def review(**kwargs):
        assert not tampered
        delivered = json.loads(kwargs["instruction"])["trials"][0]
        assert delivered["source_snapshots"]["pages"][0]["text"] == "前段正文与未读尾部"
        assert delivered["tool_events"] == [event]
        assert (delivered["source_snapshots"]["calls"][0]["raw_sha256"]
                == original.calls[0]["raw_sha256"])
        assert network.pages == {} and network.calls == []
        return SimpleNamespace(completed=True, errors=[], payload={
            "decision": "COMPLETE", "requirements": [],
        })

    output = _review_search_rollouts(
        task={"acceptance_obligations": []},
        environment={"evidence_handoff": {}, "captures": [], "requirement_coverage": [],
                     "context_messages": []},
        agent=SimpleNamespace(run=review), session=AgentSession(), output_root=tmp_path,
        native_trials=[trial], network=network,
    )
    assert output["decision"] == ("BLOCKED" if tampered else "COMPLETE")
    if tampered:
        assert output["failure_kind"] == "TRACE_UNAVAILABLE"


@pytest.mark.parametrize("decision", ["COMPLETE", "BLOCKED"])
def test_result_points_to_latest_review_checkpoint_including_new_sources(
    tmp_path, monkeypatch, decision,
):
    from types import SimpleNamespace

    from traceforge.reconstruction import search_environment

    network, _, _ = recorded_network(tmp_path / "active-web", monkeypatch)
    monkeypatch.setattr(search_environment, "SearchTools", lambda root: network)
    source = {"line_sha256": "fixed-source", "raw_session": {"messages": []}, "tool_timeline": []}
    task = {"task_id": "q1"}
    environment = {
        "task": task, "captures": [], "context_messages": [], "live_references": [],
        "tools": [], "limitations": [], "errors": [], "missing_inputs": [],
    }

    def complete(**kwargs):
        kwargs["session"].conversation.messages.append({
            "role": "user", "content": kwargs["instruction"],
        })
        return environment

    def review(**kwargs):
        kwargs["session"].conversation.messages.append({
            "role": "user", "content": "真实review阶段反馈",
        })
        kwargs["network"].open("https://example.org/new-review-paper")
        return {"decision": decision, "errors": ["明确反馈缺口"] if decision == "BLOCKED" else []}

    monkeypatch.setattr(search_environment, "_complete_search_environment", complete)
    monkeypatch.setattr(search_environment, "export_search_task", lambda *args, **kwargs: tmp_path / "harbor")
    monkeypatch.setattr(search_environment, "run_search_rollouts", lambda **kwargs: {
        "status": "ROLLOUT_COMPLETED", "errors": [], "rollouts": [],
    })
    monkeypatch.setattr(search_environment, "_review_search_rollouts", review)
    outcome = search_environment.run_search_task(
        source=source, task=task, agent=object(), output_root=tmp_path / "run",
        rollout_agent=SimpleNamespace(),
    )
    stored = json.loads((tmp_path / "run/result.json").read_text())
    assert stored["researcher_checkpoint"] == outcome["researcher_checkpoint"]
    restored = SearchTools(tmp_path / "restored")
    session = search_environment._restore_search_checkpoint(
        Path(stored["researcher_checkpoint"]), source=source, task=task, network=restored,
    )
    assert session.messages[-1]["content"] == "真实review阶段反馈"
    assert "https://example.org/new-review-paper" in restored.pages
    assert outcome["environment_review"] == decision


def test_markdown_restore_preserves_unread_links_and_jsonld(tmp_path, monkeypatch):
    markdown = "# 费率\n\n![未读取的费率图](https://example.org/rates.jpeg)\n尾部"
    original, events, _ = recorded_network(tmp_path / "old", monkeypatch, markdown=markdown)
    resumed = SearchTools(tmp_path / "new")
    resumed.restore(original.root, origin="solver:trial-01", tool_results=events)
    page = resumed.open("https://example.org/paper", offset=5)
    assert page["text"] == markdown[5:] and page["body_format"] == "markdown"
    assert page["jsonld"] == original.pages["https://example.org/paper"]["jsonld"]
    assert "image_sha256" not in page and resumed.ready() is False


@pytest.mark.parametrize("damage", ["page_format", "call_format", "unknown_format", "body"])
def test_markdown_restore_rejects_format_or_body_mismatch(tmp_path, monkeypatch, damage):
    original, events, _ = recorded_network(tmp_path / "old", monkeypatch, markdown="# 原始正文")
    page_path = next(original.root.glob("*.json"))
    page = json.loads(page_path.read_text())
    if damage == "call_format":
        call = json.loads(events[0]["result"])
        call["body_format"] = "text"
        result = json.dumps(call, ensure_ascii=False)
        events[0]["result"] = result
        events[0]["result_sha256"] = hashlib.sha256(result.encode()).hexdigest()
        (original.root / "calls.jsonl").write_text(result + "\n")
    elif damage == "page_format":
        page["body_format"] = "text"
    elif damage == "unknown_format":
        page["body_format"] = "html"
    else:
        page["text"] += "伪造"
    page_path.write_text(json.dumps(page, ensure_ascii=False))
    resumed = SearchTools(tmp_path / "new")
    with pytest.raises(ValueError):
        resumed.restore(original.root, origin="solver:trial-01", tool_results=events)
    assert resumed.pages == {} and resumed.calls == []


def test_legacy_text_snapshot_without_format_keeps_original_body(tmp_path, monkeypatch):
    original, events, digest = recorded_network(tmp_path / "old", monkeypatch)
    raw_path = original.root / f"{digest}.raw"
    data = json.loads(raw_path.read_bytes())
    data["markdown"] = "# 旧版未选用的 Markdown"
    raw = json.dumps(data, ensure_ascii=False).encode()
    new_digest = hashlib.sha256(raw).hexdigest()
    raw_path.unlink()
    (original.root / f"{new_digest}.raw").write_bytes(raw)
    page_path = next(original.root.glob("*.json"))
    page = json.loads(page_path.read_text())
    page.pop("body_format", None)
    page["raw_sha256"] = new_digest
    page_path.write_text(json.dumps(page, ensure_ascii=False))
    call = json.loads(events[0]["result"])
    call.pop("body_format", None)
    call["raw_sha256"] = new_digest
    result = json.dumps(call, ensure_ascii=False)
    events[0]["result"] = result
    events[0]["result_sha256"] = hashlib.sha256(result.encode()).hexdigest()
    (original.root / "calls.jsonl").write_text(result + "\n")

    resumed = SearchTools(tmp_path / "new")
    resumed.restore(original.root, origin="solver:legacy", tool_results=events)
    assert resumed.pages["https://example.org/paper"]["text"] == data["text"]
    assert resumed.open("https://example.org/paper")["text"] == data["text"]


def native_cache_trial(root, monkeypatch, visible_output):
    """实际回收的来源和模型看到的终端输出分别绑定。"""
    cache = root / "native-1/artifacts/logs/artifacts/search"
    original, _, _ = recorded_network(cache, monkeypatch)
    manifest = root / "native-1/artifacts/manifest.json"
    manifest.write_text('{"diagnostic_fixture": true}')
    terminal = json.dumps({"output": visible_output, "exit_code": 0}, ensure_ascii=False)
    value = [{"type": "text", "text": terminal}]
    trial = {
        "trial": "native-1", "completed": True, "answer": "实际回答",
        "web_cache_root": str(cache),
        "receipt": {
            "backend": "native_harbor",
            "evidence_files": {
                **{f"artifacts/logs/artifacts/search/{path.name}":
                   hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in cache.iterdir()},
                "artifacts/manifest.json": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            },
        },
        "tool_events": [{
            "tool_call_id": "native-open", "name": "terminal",
            "arguments": {"command": "traceforge-search open https://example.org/paper | head"},
            "result": value, "ok": True,
            "result_sha256": hashlib.sha256(
                json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        }],
    }
    return original, trial


@pytest.mark.parametrize("visible_output", [
    "段正文",
    '{"text":"段正文"}',
    '{"tool":"web_open","text":"截断',
    "json.decoder.JSONDecodeError: invalid JSON",
])
def test_native_filtered_stdout_does_not_erase_collected_sources(
    tmp_path, monkeypatch, visible_output,
):
    from types import SimpleNamespace

    from traceforge.reconstruction.search_environment import _review_search_rollouts

    original, trial = native_cache_trial(tmp_path, monkeypatch, visible_output)
    network = SearchTools(tmp_path / "restored")

    def review(**kwargs):
        request = json.loads(kwargs["instruction"])
        delivered = request["trials"][0]
        assert {key: delivered[key] for key in trial} == trial
        assert "不代表 solver 已读全文" in request["current_stage_instruction"]
        snapshots = delivered["source_snapshots"]
        assert snapshots["pages"][0]["text"] == "前段正文与未读尾部"
        assert network.pages == {} and network.calls == []
        assert len(snapshots["calls"]) == len(original.calls)
        for original_call, restored in zip(original.calls, snapshots["calls"], strict=True):
            assert {key: restored[key] for key in original_call} == original_call
            assert restored["restored_from"] == "native_solver:native-1"
            assert "native_result_sha256" not in restored
        return SimpleNamespace(completed=True, errors=[], payload={
            "decision": "COMPLETE", "requirements": [{
                "obligation_id": "inspect", "status": "SOLVER_ERROR",
                "reason": "来源已交付，模型所见仅是局部或错误输出",
            }],
        })

    result = _review_search_rollouts(
        task={"acceptance_obligations": [{"id": "inspect"}]},
        environment={"evidence_handoff": {}, "captures": [], "requirement_coverage": [],
                     "context_messages": []},
        agent=SimpleNamespace(run=review), session=AgentSession(), output_root=tmp_path / "review",
        native_trials=[trial], network=network,
    )
    assert result["decision"] == "COMPLETE"


@pytest.mark.parametrize("damage", ["unbound", "cache", "manifest", "tool_result"])
def test_native_cache_recovery_rejects_unbound_or_changed_evidence(
    tmp_path, monkeypatch, damage,
):
    from types import SimpleNamespace

    from traceforge.reconstruction.search_environment import _review_search_rollouts

    original, trial = native_cache_trial(tmp_path, monkeypatch, "段正文")
    if damage == "unbound":
        trial["receipt"]["evidence_files"] = {}
    elif damage == "cache":
        with (original.root / "calls.jsonl").open("a") as stream:
            stream.write("{}\n")
    elif damage == "manifest":
        (tmp_path / "native-1/artifacts/manifest.json").write_text("{}")
    else:
        trial["tool_events"][0]["result"] = "伪造工具返回"

    def review(**kwargs):
        pytest.fail("未绑定或已改变的证据不能发给复核模型")

    result = _review_search_rollouts(
        task={"acceptance_obligations": [{"id": "inspect"}]},
        environment={"evidence_handoff": {}, "captures": [], "requirement_coverage": [],
                     "context_messages": []},
        agent=SimpleNamespace(run=review), session=AgentSession(), output_root=tmp_path / "review",
        native_trials=[trial], network=SearchTools(tmp_path / "restored"),
    )
    assert result["failure_kind"] == "TRACE_UNAVAILABLE"
    assert result["decision"] == "BLOCKED"


def test_native_trials_keep_distinct_snapshots_of_same_url(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from traceforge.reconstruction.search_environment import _review_search_rollouts

    _, trial1 = native_cache_trial(tmp_path / "first", monkeypatch, "前段正文")
    second, trial2 = native_cache_trial(tmp_path / "second", monkeypatch, "段正文")
    trial2["trial"] = "native-2"
    old_raw = next(second.root.glob("*.raw"))
    raw = old_raw.read_bytes() + b" "
    digest = hashlib.sha256(raw).hexdigest()
    old_digest = old_raw.stem
    old_raw.unlink()
    (second.root / f"{digest}.raw").write_bytes(raw)
    for path in [*second.root.glob("*.json"), second.root / "calls.jsonl"]:
        path.write_text(path.read_text().replace(old_digest, digest))
    trial2["receipt"]["evidence_files"].update({
        f"artifacts/logs/artifacts/search/{path.name}":
        hashlib.sha256(path.read_bytes()).hexdigest() for path in second.root.iterdir()
    })
    del trial2["receipt"]["evidence_files"][f"artifacts/logs/artifacts/search/{old_digest}.raw"]
    original_trials = json.loads(json.dumps([trial1, trial2]))
    network = SearchTools(tmp_path / "author")

    def review(**kwargs):
        request = json.loads(kwargs["instruction"])
        snapshots = [t["source_snapshots"] for t in request["trials"]]
        assert [s["pages"][0]["raw_sha256"] for s in snapshots] == [old_digest, digest]
        assert all(s["pages"][0]["text"] == "前段正文与未读尾部" for s in snapshots)
        assert [t["tool_events"] for t in request["trials"]] == [
            trial1["tool_events"], trial2["tool_events"]]
        assert network.pages == {} and network.calls == []
        return SimpleNamespace(completed=True, errors=[], payload={
            "decision": "COMPLETE", "requirements": [{
                "obligation_id": "inspect", "status": "SUPPORTED",
                "reason": "分别保留真实抓取，正文相同不构成环境缺口",
            }],
        })

    result = _review_search_rollouts(
        task={"acceptance_obligations": [{"id": "inspect"}]},
        environment={"evidence_handoff": {}, "captures": [], "requirement_coverage": [],
                     "context_messages": []},
        agent=SimpleNamespace(run=review), session=AgentSession(), output_root=tmp_path / "review",
        native_trials=[trial1, trial2], network=network,
    )
    assert result["decision"] == "COMPLETE"
    assert [trial1, trial2] == original_trials
