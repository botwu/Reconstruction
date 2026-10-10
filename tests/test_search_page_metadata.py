"""页面返回保留提供方已有的结构化书目信息，不猜测作者。"""

import json

import pytest

from traceforge.reconstruction.search_tools import SearchTools


@pytest.fixture
def serper_tools(tmp_path, monkeypatch):
    tools = SearchTools(tmp_path)
    tools._serper_key = "test-only"
    tools._fetch_provider = "serper"
    monkeypatch.setattr(tools, "_fetch_document", lambda url: None)
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


def test_serper_markdown_keeps_real_rate_image_and_source_link(serper_tools, monkeypatch):
    # 真实 FBT 返回的最小等价片段：纯文本没有图片 URL，Markdown 保留来源。
    image = (
        '![](https://p16-oec-general-useast5.ttcdn-us.com/'
        'tos-useast5-i-omjb5zjo8w-tx/9e76a19c14b04385be1e17913340a4e0'
        '~tplv-fhlh96nyum-origin-jpeg.jpeg?'
        'dr=10761&'
        'amp;t=e19dd3fe&'
        'amp;ps=933b5bde&'
        'amp;shp=f36fc0ff&'
        'amp;shcp=9b759fb9&'
        'amp;idc=useast5&'
        'amp;from=4084187391)'
    )
    link = (
        '[Free Shipping Program](https://seller-us.tiktok.com/university/essay?'
        'course_type=1&'
        'from=search%7BcontentIdParams%7D&'
        'identity=1&'
        'knowledge_id=4442975095555886&'
        'role=1)'
    )
    markdown = "## FBT Fees\n\n" + image + "\n\n" + link
    response = {"text": "FBT Fees", "markdown": markdown,
                "metadata": {"title": "FBT Fees"}, "credits": 1}
    requests = []

    def respond(request):
        requests.append(json.loads(request.data))
        return response, "raw-hash"

    monkeypatch.setattr(serper_tools, "_response", respond)
    first = serper_tools.open("https://example.org/fbt", limit=20)
    second = serper_tools.open("https://example.org/fbt", offset=20)

    assert first["text"] + second["text"] == markdown
    assert first["body_format"] == second["body_format"] == "markdown"
    assert first["total_chars"] == len(markdown)
    assert requests == [{"url": "https://example.org/fbt", "includeMarkdown": True}]
    assert "ocr_page" not in first and "image_sha256" not in first


@pytest.mark.parametrize("markdown", [None, "", "   ", 17])
def test_serper_empty_or_invalid_markdown_uses_text(serper_tools, monkeypatch, markdown):
    response = {"text": "纯文本正文", "markdown": markdown}
    monkeypatch.setattr(serper_tools, "_response", lambda request: (response, "raw-hash"))
    page = serper_tools.open("https://example.org/paper")
    assert page["success"] and page["text"] == response["text"]
    assert page["body_format"] == "text"


def test_serper_markdown_only_preserves_table_and_jsonld(serper_tools, monkeypatch):
    markdown = "| 项目 | 金额 |\n| --- | --- |\n| 示例 | 1 |"
    jsonld = {"@type": "Article", "author": [{"name": "甲"}, {"name": "乙"}]}
    response = {"markdown": markdown, "metadata": {"title": "表格"}, "jsonld": jsonld}
    monkeypatch.setattr(serper_tools, "_response", lambda request: (response, "raw-hash"))
    page = serper_tools.open("https://example.org/table")
    assert page["success"] and page["text"] == markdown
    assert page["body_format"] == "markdown" and page["jsonld"] == jsonld
