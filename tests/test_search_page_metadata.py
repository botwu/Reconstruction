"""页面返回保留提供方已有的结构化书目信息，不猜测作者。"""

import json

import pytest

from traceforge.reconstruction.search_tools import SearchTools


@pytest.fixture
def serper_tools(tmp_path, monkeypatch):
    tools = SearchTools(tmp_path)
    tools._serper_key = "test-only"
    tools._fetch_provider = "serper"
    monkeypatch.setattr(tools, "_fetch_pdf", lambda url: None)
    monkeypatch.setattr("traceforge.reconstruction.search_tools._public_url", lambda url: None)
    return tools


def test_page_without_jsonld_keeps_existing_metadata(serper_tools, monkeypatch):
    metadata = {"title": "论文题名", "citation_author": "作者甲"}
    response = {"text": "真实正文", "metadata": metadata}
    monkeypatch.setattr(serper_tools, "_response", lambda request: (response, "raw-hash"))

    page = serper_tools.open("https://example.org/paper")

    assert page["metadata"] == metadata
    assert page["title"] == "论文题名"
    assert page["raw_sha256"] == "raw-hash"
    assert "jsonld" not in page


@pytest.mark.parametrize("jsonld", [
    {"@type": "ScholarlyArticle", "author": {"@type": "Person", "name": "作者甲"}},
    {"@graph": [
        {"@type": "ScholarlyArticle", "author": [{"@id": "person1"}, {"@id": "person2"}]},
        {"@id": "person1", "@type": "Person", "name": "作者甲"},
        {"@id": "person2", "@type": "Person", "name": "作者乙"},
    ]},
])
def test_jsonld_keeps_author_order_and_reference_nodes(serper_tools, monkeypatch, jsonld):
    metadata = {"title": "论文题名", "citation_author": "作者乙"}
    response = {"text": "连续正文", "metadata": metadata, "jsonld": jsonld}
    monkeypatch.setattr(serper_tools, "_response", lambda request: (response, "raw-hash"))

    first = serper_tools.open("https://example.org/paper", limit=2)
    second = serper_tools.open("https://example.org/paper", offset=2)

    for page in [first, second]:
        assert page["jsonld"] == jsonld
        assert page["metadata"] == metadata
        assert page["raw_sha256"] == "raw-hash"
    cached = next(serper_tools.root.glob("*.json"))
    assert json.loads(cached.read_text())["jsonld"] == jsonld
    events = [
        json.loads(line) for line in (serper_tools.root / "calls.jsonl").read_text().splitlines()
    ]
    assert all(event["jsonld"] == jsonld for event in events)


def test_jsonld_does_not_replace_missing_page_body(serper_tools, monkeypatch):
    response = {"text": "", "jsonld": {"@type": "Person", "name": "作者甲"}}
    monkeypatch.setattr(serper_tools, "_response", lambda request: (response, "raw-hash"))

    page = serper_tools.open("https://example.org/paper")

    assert page["success"] is False
    assert "未返回可读正文" in page["error"]
