"""重复链接表只在后审请求中转为同版来源引用，实际读取正文保持原样。"""

import copy
from types import SimpleNamespace

import pytest

from traceforge.reconstruction import search_environment as module
from traceforge.reconstruction.agents import AgentSession
from traceforge.reconstruction.search_tools import SearchTools
from traceforge.reconstruction.session_source import indexed_session


def capture_request(tmp_path, monkeypatch, *, completion=False, mismatch=None):
    links = [{"text": "章节" + str(index), "url": "https://example.org/" + str(index)}
             for index in range(50)]
    page = {"tool": "web_open", "url": "https://example.org/guide", "raw_sha256": "version-a",
            "text": "完整正文", "links": links}
    calls = [{"tool": "web_open", "url": page["url"], "raw_sha256": page["raw_sha256"],
              "success": True, "text": "正文", "offset": 2, "limit": 2, "total_chars": 4,
              "links": copy.deepcopy(links), "retrieved_at": "原抓取时间"}]
    references = [copy.deepcopy(page)]
    if mismatch == "url":
        calls[0]["url"] += "/other"
    elif mismatch == "version":
        calls[0]["raw_sha256"] = "version-b"
    elif mismatch == "links":
        calls[0]["links"][-1]["text"] = "另一版本标题"
    elif mismatch == "missing_hash":
        calls[0].pop("raw_sha256")
    elif mismatch == "ambiguous":
        references.append(copy.deepcopy(page))
    elif mismatch == "existing_ref":
        calls[0]["links_ref"] = {"path": "已有字段必须保留"}
    source = {"raw_session": {"tools": [{"name": "original"}], "messages": [
        {"role": "system", "content": "全部原始上下文"},
        {"role": "user", "content": "解释该指南"},
    ]}}
    task = {"task_id": "guide", "acceptance_obligations": [{"id": "explain"}]}
    environment = {"task": task, "captures": [], "context_messages": [],
                   "live_references": references}
    native = [{"trial": "trial-one", "answer": "原答案", "tool_events": [
        {"result": {"text": "求解者实际所见正文", "links": copy.deepcopy(links)}}],
        "trajectory": {"messages": [{"role": "tool", "content": "原有工具消息"}]}}]
    untouched = copy.deepcopy((source, environment, calls, native))
    network = SearchTools(tmp_path / "author-web")
    network.calls = calls
    captured = {}

    def record(**kwargs):
        captured.update(kwargs["request"])
        return {"decision": "BLOCKED", "requirements": []}

    monkeypatch.setattr(module, "_run_search_review", record)
    kwargs = dict(source=source, task=task, environment=environment,
                  agent=SimpleNamespace(), session=AgentSession(), network=network,
                  output_root=tmp_path / "review")
    if completion:
        module._review_search_completion(**kwargs)
    else:
        module._review_search_rollouts(**kwargs, native_trials=native)
    assert (source, environment, calls, native) == untouched
    assert captured["SOURCE_SESSION"] == indexed_session(source["raw_session"])
    assert captured["environment"] == environment
    if not completion:
        assert captured["trials"] == native
    return captured, calls


@pytest.mark.parametrize("completion", [False, True])
def test_repeated_links_expand_to_exact_original_calls(tmp_path, monkeypatch, completion):
    request, calls = capture_request(tmp_path, monkeypatch, completion=completion)
    projected = request["actual_web_calls"][0]
    assert "links" not in projected
    assert projected["links_ref"] == {
        "path": "/environment/live_references/0/links", "raw_sha256": "version-a",
    }
    restored = copy.deepcopy(projected)
    reference = restored.pop("links_ref")
    target = request
    for part in reference["path"].strip("/").split("/"):
        target = target[int(part)] if isinstance(target, list) else target[part]
    restored["links"] = copy.deepcopy(target)
    assert restored == calls[0]


@pytest.mark.parametrize("mismatch", [
    "url", "version", "links", "missing_hash", "ambiguous", "existing_ref",
])
def test_different_or_ambiguous_source_keeps_complete_call(tmp_path, monkeypatch, mismatch):
    request, calls = capture_request(tmp_path, monkeypatch, mismatch=mismatch)
    assert request["actual_web_calls"] == calls
