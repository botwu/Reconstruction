"""静态 HTML 来源须保留原字节、链接和恢复校验，不依赖收费读取。"""

import hashlib
import io
import json
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
    assert first["extraction_scope"] == "static_html"
    assert first["title"] == "真实论文"
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
    assert [item["url"] for item in result["links"]] == ["https://example.org/valid"]


def test_direct_html_restore_uses_raw_encoding_and_links(tmp_path, monkeypatch):
    raw = HTML.decode().replace("utf-8", "gb18030").encode("gb18030")
    tools, requests = direct_tools(tmp_path / "original", monkeypatch, raw, "text/html")
    tools.open(URL, limit=3)
    resumed = SearchTools(tmp_path / "resumed")
    resumed.restore(tools.root, origin="actual-trial", tool_results=bound_events(tools))

    restored = resumed.open(URL, offset=3)
    assert restored["success"] and restored["cache_hit"]
    assert restored["encoding"] == "gb18030"
    assert restored["title"] == "真实论文"
    assert restored["links"] == tools.pages[URL]["links"]
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
    page = tools.open(URL)
    assert page["links"][0]["url"] == "https://example.org/%E8%AE%BA%E6%96%87.pdf"


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
