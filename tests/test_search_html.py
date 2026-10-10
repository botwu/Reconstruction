"""静态 HTML 来源须保留原字节、链接和恢复校验，不依赖收费读取。"""

import hashlib
import io
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from traceforge.reconstruction import search_tools
from traceforge.reconstruction.search_tools import SearchTools

URL = "https://example.org/article"
RESOLVED = "https://example.org/papers/one/"
HTML = """<html><head><meta charset="utf-8"><title>真实论文</title>
<meta name="citation_pdf_url" content="../paper.pdf"></head><body>
<h1>论文标题</h1><p>摘要正文与未读尾部</p>
<a href="../paper.pdf">PDF 下载</a><a href="javascript:alert(1)">无效链接</a>
<script>伪正文</script><style>伪样式</style></body></html>""".encode()


def direct_tools(tmp_path, monkeypatch, raw=HTML, content_type="text/html; charset=utf-8",
                 resolved=RESOLVED):
    requests = []
    monkeypatch.setattr(search_tools, "_public_url", lambda _url: None)

    def open_response(request, **kwargs):
        requests.append(request.full_url)
        response = io.BytesIO(raw)
        response.headers = {"Content-Type": content_type}
        response.geturl = lambda: resolved
        return response

    monkeypatch.setattr(search_tools.urllib.request, "build_opener",
                        lambda *args: SimpleNamespace(open=open_response))
    tools = SearchTools(tmp_path)
    monkeypatch.setattr(tools, "_fetch_page", lambda _url: pytest.fail("不应调用收费读取"))
    return tools, requests


def bound_events(tools):
    return [{
        "name": call["tool"], "tool_call_id": f"actual-{index}",
        "result": text, "result_sha256": hashlib.sha256(text.encode()).hexdigest(),
    } for index, call in enumerate(tools.calls)
        for text in [json.dumps(call, ensure_ascii=False)]]


def test_direct_html_preserves_source_links_and_pagination(tmp_path, monkeypatch):
    tools, requests = direct_tools(tmp_path, monkeypatch)
    first = tools.open(URL, limit=3)
    second = tools.open(URL, offset=3)
    page = tools.pages[URL]

    assert first["success"] and second["cache_hit"]
    assert first["provider"] == "direct_html"
    metadata = json.loads(tools.open(URL, section="metadata")["text"])
    assert metadata["extraction_scope"] == "static_html"
    assert metadata["title"] == "真实论文"
    assert first["text"] + second["text"] == page["text"]
    assert "摘要正文与未读尾部" in page["text"]
    assert "伪正文" not in page["text"] and "伪样式" not in page["text"]
    assert {link["url"] for link in page["links"]} == {"https://example.org/papers/paper.pdf"}
    assert any(link["text"] == "PDF 下载" for link in page["links"])
    assert any(link["source"] == "citation_pdf_url" for link in page["links"])
    assert page["resolved_url"] == RESOLVED
    assert page["encoding"] == "utf-8"
    assert page["limitations"]
    assert (tmp_path / (page["raw_sha256"] + ".raw")).read_bytes() == HTML
    assert requests == [URL]


@pytest.mark.parametrize("url,wire", [
    ("https://www.bing.com/search?q=铁路通信信号工程技术+张辰东+2024",
     "https://www.bing.com/search?q=%E9%93%81%E8%B7%AF%E9%80%9A%E4%BF%A1%E4%BF%A1%E5%8F%B7"
     "%E5%B7%A5%E7%A8%8B%E6%8A%80%E6%9C%AF+%E5%BC%A0%E8%BE%B0%E4%B8%9C+2024"),
    ("https://example.org/中文%2F.html?q=a%26b+c&x=带 空格",
     "https://example.org/%E4%B8%AD%E6%96%87%2F.html?q=a%26b+c&x=%E5%B8%A6%20%E7%A9%BA%E6%A0%BC"),
    ("https://例子.测试:8443/正文",
     "https://xn--fsqu00a.xn--0zwm56d:8443/%E6%AD%A3%E6%96%87"),
    ("https://[2606:4700:4700::1111]:443/正文",
     "https://[2606:4700:4700::1111]:443/%E6%AD%A3%E6%96%87"),
])
def test_unicode_url_is_encoded_for_transport_without_rewriting_source(
    tmp_path, monkeypatch, url, wire,
):
    tools, requests = direct_tools(tmp_path, monkeypatch)
    result = tools.open(url)
    assert result["success"] and result["url"] == url
    assert requests == [wire] and requests[0].isascii()
    assert tools.calls[0]["url"] == url and url in tools.pages


@pytest.mark.parametrize("bad_url", [
    "https://example.org:bad/path", "https://example.org:99999/path", "https://@example.org/path",
])
def test_invalid_navigation_link_does_not_discard_valid_page_text(
    tmp_path, monkeypatch, bad_url,
):
    raw = (
        f'<p>前段正文</p><a href="{bad_url}">无效导航</a>'
        '<a href="/valid">有效导航</a><p>后段正文</p>'
    ).encode()
    tools, _ = direct_tools(tmp_path, monkeypatch, raw=raw)
    result = tools.open(URL)
    assert result["success"] and "前段正文" in result["text"] and "后段正文" in result["text"]
    links = json.loads(tools.open(URL, section="links")["text"])
    assert [item["url"] for item in links] == ["https://example.org/valid"]


def test_direct_html_restore_uses_raw_encoding_and_links(tmp_path, monkeypatch):
    raw = HTML.decode().replace("utf-8", "gb18030").encode("gb18030")
    tools, requests = direct_tools(tmp_path / "original", monkeypatch, raw, "text/html")
    tools.open(URL, limit=3)
    resumed = SearchTools(tmp_path / "resumed")
    resumed.restore(tools.root, origin="actual-trial", tool_results=bound_events(tools))

    restored = resumed.open(URL, offset=3)
    assert restored["success"] and restored["cache_hit"]
    metadata = json.loads(resumed.open(URL, section="metadata")["text"])
    assert metadata["encoding"] == "gb18030"
    assert metadata["title"] == "真实论文"
    assert json.loads(resumed.open(URL, section="links")["text"]) == tools.pages[URL]["links"]
    assert requests == [URL]


@pytest.mark.parametrize("field,value", [
    ("text", "伪造正文"), ("title", "伪造题名"),
    ("links", [{"url": "https://example.org/forged.pdf", "text": "伪造", "source": "a"}]),
    ("resolved_url", "https://other.example.org/"),
    ("encoding", "latin-1"),
])
def test_direct_html_rejects_changed_derived_cache(tmp_path, monkeypatch, field, value):
    tools, _ = direct_tools(tmp_path / "original", monkeypatch)
    tools.open(URL, limit=3)
    events = bound_events(tools)
    path = tools.root / (hashlib.sha256(URL.encode()).hexdigest() + ".json")
    page = json.loads(path.read_text())
    page[field] = value
    path.write_text(json.dumps(page))

    resumed = SearchTools(tmp_path / "resumed")
    with pytest.raises(ValueError):
        resumed.restore(tools.root, origin="actual-trial", tool_results=events)
    assert not resumed.pages


def test_direct_html_rejects_forged_link_even_outside_read_text(tmp_path, monkeypatch):
    tools, _ = direct_tools(tmp_path / "original", monkeypatch)
    tools.open(URL, limit=3)
    call = tools.calls[0]
    call["links"] = [{"url": "https://example.org/forged.pdf", "text": "伪造", "source": "a"}]
    (tools.root / "calls.jsonl").write_text(json.dumps(call, ensure_ascii=False) + "\n")
    with pytest.raises(ValueError):
        SearchTools(tmp_path / "resumed").restore(
            tools.root, origin="actual-trial", tool_results=bound_events(tools))


def test_direct_html_cached_open_rechecks_raw_bytes(tmp_path, monkeypatch):
    tools, _ = direct_tools(tmp_path, monkeypatch)
    first = tools.open(URL)
    (tmp_path / (first["raw_sha256"] + ".raw")).write_bytes(b"forged")
    result = tools.open(URL)
    assert not result["success"]
    assert "哈希" in result["error"]


@pytest.mark.parametrize("raw", [b"<html><script>dynamic()</script></html>", b"x" * 8_000_001])
def test_empty_or_oversized_html_is_not_evidence(tmp_path, monkeypatch, raw):
    tools, _ = direct_tools(tmp_path, monkeypatch, raw)
    fallback = []
    monkeypatch.setattr(tools, "_fetch_page", lambda url: fallback.append(url) or {
        "success": True, "url": url, "text": "实际服务返回", "provider": "configured_reader",
    })
    result = tools.open(URL)
    if len(raw) > 8_000_000:
        assert not result["success"] and not fallback
        assert "8 MB" in result["error"]
    else:
        assert result["provider"] == "configured_reader" and fallback == [URL]


def test_github_blob_never_uses_html_as_source(tmp_path, monkeypatch):
    tools, requests = direct_tools(tmp_path, monkeypatch)
    monkeypatch.setattr(tools, "_fetch_page", lambda url: {
        "success": True, "url": url, "text": "网页不能冒充源码",
    })
    result = tools.open("https://github.com/owner/repo/blob/main/src.py")
    assert not result["success"]
    assert requests == ["https://github.com/owner/repo/blob/main/src.py"]


def test_html_links_quote_non_ascii_without_inventing_a_target(tmp_path, monkeypatch):
    raw = '<p>正文</p><a href="/论文.pdf">下载</a>'.encode()
    tools, _ = direct_tools(tmp_path, monkeypatch, raw)
    links = json.loads(tools.open(URL, section="links")["text"])
    assert links[0]["url"] == "https://example.org/%E8%AE%BA%E6%96%87.pdf"


def test_redirected_github_html_cannot_bypass_blob_validation(tmp_path, monkeypatch):
    tools, _ = direct_tools(
        tmp_path, monkeypatch, resolved="https://github.com/owner/repo/blob/main/src.py")
    result = tools.open(URL)
    assert not result["success"]
    assert "内容 API" in result["error"]


@pytest.mark.parametrize("url", [
    "https://raw.githubusercontent.com/owner/repo/main/paper.pdf",
    "https://github.com/owner/repo/blob/main/paper.pdf",
])
def test_github_pdf_uses_original_document_route(tmp_path, monkeypatch, url):
    from test_search_pdf import pdf_bytes

    raw = pdf_bytes(["Original GitHub PDF"])
    tools, requests = direct_tools(tmp_path, monkeypatch, raw, "application/pdf", resolved=url)
    result = tools.open(url)

    assert result["success"]
    assert result["provider"] == "direct_pdf"
    assert "Original GitHub PDF" in result["text"]
    assert result["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert requests == [url]


def test_large_navigation_and_metadata_do_not_hide_body_or_pagination(tmp_path, monkeypatch):
    raw = (
        "<html><head><title>" + "完整标题" * 5000 + "</title></head><body>"
        + "<p>" + "正文" * 6000 + "</p>"
        + "".join(f'<a href="/chapter/{i}#section">章节{i}导航</a>' for i in range(1200))
        + "</body></html>"
    ).encode()
    tools, requests = direct_tools(tmp_path, monkeypatch, raw)
    first = tools.open(URL, limit=8000)
    page = tools.pages[URL]

    assert len(json.dumps(first, ensure_ascii=False)) < 20000
    assert len(first["text"]) == 8000
    assert first["next_offset"] == 8000
    assert first["total_chars"] == len(page["text"])
    assert first["section"] == "text"
    assert not {"links", "title", "metadata", "jsonld", "limitations"} & first.keys()
    for section, expected in (
        ("links", page["links"]),
        ("metadata", {key: value for key, value in page.items() if key not in {"text", "links"}}),
    ):
        chunks = []
        offset = 0
        while offset is not None:
            result = tools.open(URL, section=section, offset=offset, limit=8000)
            assert result["success"] and result["cache_hit"]
            assert len(json.dumps(result, ensure_ascii=False)) < 20000
            chunks.append(result["text"])
            offset = result["next_offset"]
        assert json.loads("".join(chunks)) == expected

    assert requests == [URL]
    assert (tmp_path / (page["raw_sha256"] + ".raw")).read_bytes() == raw
    cache = tmp_path / (hashlib.sha256(URL.encode()).hexdigest() + ".json")
    saved = json.loads(cache.read_text())
    assert saved == page


@pytest.mark.parametrize("section", ["text", "links", "metadata"])
@pytest.mark.parametrize("field,value", [
    ("text", "伪造内容"), ("next_offset", 999999), ("total_chars", 1),
    ("links", [{"url": "https://example.org/forged", "text": "伪造", "source": "a"}]),
])
def test_section_restore_rejects_forged_content_and_cursor(
    tmp_path, monkeypatch, section, field, value,
):
    tools, _ = direct_tools(tmp_path / "original", monkeypatch)
    tools.open(URL, section=section, limit=3)
    tools.calls[0][field] = value
    (tools.root / "calls.jsonl").write_text(json.dumps(tools.calls[0], ensure_ascii=False) + "\n")
    with pytest.raises(ValueError):
        SearchTools(tmp_path / "resumed").restore(
            tools.root, origin="trial", tool_results=bound_events(tools),
        )


def test_sections_survive_repeated_restore_without_changing_source(tmp_path, monkeypatch):
    tools, requests = direct_tools(tmp_path / "original", monkeypatch)
    for section in ("text", "links", "metadata"):
        tools.open(URL, section=section, limit=3)
    first = SearchTools(tmp_path / "first")
    first.restore(tools.root, origin="original", tool_results=bound_events(tools))
    for section in ("text", "links", "metadata"):
        assert first.open(URL, section=section, offset=3)["cache_hit"]
    second = SearchTools(tmp_path / "second")
    second.restore(first.root, origin="first", tool_results=bound_events(first))
    assert second.pages[URL]["text"] == tools.pages[URL]["text"]
    assert second.pages[URL]["links"] == tools.pages[URL]["links"]
    assert (second.root / (tools.pages[URL]["raw_sha256"] + ".raw")).read_bytes() == HTML
    assert requests == [URL]


@pytest.mark.parametrize("forged", [False, True])
def test_legacy_unsectioned_links_receipt_keeps_strict_restore(tmp_path, monkeypatch, forged):
    tools, _ = direct_tools(tmp_path / "original", monkeypatch)
    tools.open(URL, limit=3)
    page = tools.pages[URL]
    legacy = {
        "tool": "web_open", **page, "text": page["text"][:3],
        "total_chars": len(page["text"]), "offset": 0, "next_offset": 3, "cache_hit": False,
    }
    if forged:
        legacy["links"] = [{"url": "https://example.org/forged", "text": "伪造", "source": "a"}]
    tools.calls[:] = [legacy]
    (tools.root / "calls.jsonl").write_text(json.dumps(legacy, ensure_ascii=False) + "\n")
    restored = SearchTools(tmp_path / "restored")
    if forged:
        with pytest.raises(ValueError, match="HTML 工具回执"):
            restored.restore(tools.root, origin="legacy", tool_results=bound_events(tools))
    else:
        restored.restore(tools.root, origin="legacy", tool_results=bound_events(tools))
        assert restored.open(URL, offset=3)["text"] == page["text"][3:]


def test_invalid_section_fails_before_fetch(tmp_path, monkeypatch):
    tools = SearchTools(tmp_path)
    monkeypatch.setattr(tools, "_fetch", lambda _: pytest.fail("无效分区不得联网"))
    result = tools.open(URL, section="unknown")
    assert not result["success"] and "section" in result["error"]
    assert not tools.pages


def test_jsonld_and_metadata_are_paged_without_silent_truncation(tmp_path, monkeypatch):
    tools = SearchTools(tmp_path)
    page = {
        "success": True, "url": URL, "text": "正文", "provider": "serper",
        "metadata": {"authors": ["真实作者"] * 8000},
        "jsonld": [{"description": "完整描述" * 10000}],
    }
    monkeypatch.setattr(tools, "_fetch", lambda _: page)
    first = tools.open(URL)
    assert len(json.dumps(first, ensure_ascii=False)) < 1000
    chunks = []
    offset = 0
    while offset is not None:
        result = tools.open(URL, section="metadata", offset=offset)
        assert len(json.dumps(result, ensure_ascii=False)) < 20000
        chunks.append(result["text"])
        offset = result["next_offset"]
    restored = json.loads("".join(chunks))
    assert restored["metadata"] == page["metadata"]
    assert restored["jsonld"] == page["jsonld"]


@pytest.mark.parametrize("body,expected", [("", False), ("   ", False), ("实际正文", True)])
def test_ready_uses_fetched_body_not_link_json_or_pagination(tmp_path, monkeypatch, body, expected):
    tools = SearchTools(tmp_path)
    requests = []
    monkeypatch.setattr(tools, "_search", lambda _: ([], "search-sha"))

    def fetch(url):
        requests.append(url)
        return {"success": True, "url": url, "text": body,
                "links": [{"url": "https://example.org/next"}]}

    monkeypatch.setattr(tools, "_fetch", fetch)
    tools.search("检索")
    assert tools.open(URL, section="links")["text"]
    assert tools.ready() is expected
    tools.open(URL, offset=99999)
    assert tools.ready() is expected
    assert requests == [URL]


@pytest.mark.parametrize("section", ["links", "metadata"])
def test_cli_sections_continue_same_snapshot_across_processes(tmp_path, monkeypatch, section):
    tools, requests = direct_tools(tmp_path, monkeypatch)
    first = tools.open(URL, section=section, limit=3)
    result = subprocess.run([
        sys.executable, search_tools.__file__, "--output-root", str(tmp_path),
        "open", URL, "--section", section, "--offset", "3", "--limit", "8000",
    ], capture_output=True, text=True, check=True)
    second = json.loads(result.stdout)
    expected = (tools.pages[URL]["links"] if section == "links" else {
        key: value for key, value in tools.pages[URL].items() if key not in {"text", "links"}
    })
    assert json.loads(first["text"] + second["text"]) == expected
    assert second["cache_hit"] and second["next_offset"] is None
    assert second["raw_sha256"] == first["raw_sha256"]
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 2
    assert requests == [URL]


def test_agent_web_open_schema_and_dispatch_keep_section_cursor(tmp_path, monkeypatch):
    from traceforge.reconstruction.agents.session import AgentSession, execute_tool, tool_schemas

    tools, _ = direct_tools(tmp_path, monkeypatch)
    schema = tool_schemas(("web_open",))[0]["function"]["parameters"]["properties"]
    assert schema["section"]["enum"] == ["text", "links", "metadata"]
    assert schema["section"]["default"] == "text"
    session = AgentSession(web_open_handler=tools.open)
    result = json.loads(execute_tool(
        "web_open", {"url": URL, "section": "links", "offset": 3, "limit": 7}, session,
    ))
    assert result["section"] == "links" and result["offset"] == 3
    assert result["limit"] == 7 and result["next_offset"] == 10
    assert result["text"] == json.dumps(tools.pages[URL]["links"], ensure_ascii=False)[3:10]
