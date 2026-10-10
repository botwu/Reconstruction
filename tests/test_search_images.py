"""原图按真实字节保存和恢复；查看图片不创建文本识别替身。"""

import base64
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from traceforge.reconstruction import search_tools
from traceforge.reconstruction.search_tools import SearchTools

URL = "https://example.org/assets/rate-card"
RESOLVED = "https://cdn.example.org/current-rates"
IMAGES = {
    "image/jpeg": (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAMEBgUGBgYF"
        "BgYGBwkIBgcJBwYGCAsICQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUDAwUKBwYHCgoKCgoK"
        "CgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgr/wAARCAACAAIDASIA"
        "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
        "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
        "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
        "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
        "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
        "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
        "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
        "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD+f+ii"
        "igD/2Q=="
    ),
    "image/png": (
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAD0lEQVQIHWNkAANGBjAAAAAjAAMz"
        "85CnAAAAAElFTkSuQmCC"
    ),
}


def image_tools(root, monkeypatch, mime="image/jpeg", raw=None, content_length=None):
    raw = base64.b64decode(IMAGES[mime]) if raw is None else raw
    requests = []
    monkeypatch.setattr(search_tools, "_public_url", lambda _url, **kwargs: None)

    def response(request, **kwargs):
        requests.append(request.full_url)
        stream = io.BytesIO(raw)
        stream.headers = {
            "Content-Type": mime,
            "Content-Length": str(len(raw) if content_length is None else content_length),
        }
        stream.geturl = lambda: RESOLVED
        return stream

    monkeypatch.setattr(search_tools.urllib.request, "build_opener",
                        lambda *args: SimpleNamespace(open=response))
    tools = SearchTools(root)
    monkeypatch.setattr(tools, "_fetch_page", lambda _url: {
        "success": True, "text": "图片不能由乱码正文替代", "provider": "text_reader",
    })
    return tools, requests, raw


def bound_events(tools):
    return [{
        "name": call["tool"], "tool_call_id": f"actual-{i}",
        "result": text, "result_sha256": hashlib.sha256(text.encode()).hexdigest(),
    } for i, call in enumerate(tools.calls)
        for text in [json.dumps(call, ensure_ascii=False)]]


@pytest.mark.parametrize("mime,suffix", [("image/jpeg", ".jpeg"), ("image/png", ".png")])
def test_open_preserves_original_image_and_view_sends_exact_pixels(
    tmp_path, monkeypatch, mime, suffix,
):
    tools, requests, raw = image_tools(tmp_path, monkeypatch, mime)
    result = tools.open(URL)
    assert result["success"] and result["provider"] == "direct_image"
    assert result["content_kind"] == "source_image"
    assert result["mime_type"] == mime and result["source_bytes"] == len(raw)
    assert result["url"] == URL and result["resolved_url"] == RESOLVED
    assert result["extraction_scope"] == "image_bytes"
    assert "未" in result["text"] and result["limitations"]
    digest = hashlib.sha256(raw).hexdigest()
    assert result["raw_sha256"] == digest
    assert search_tools.image_assets(tools.pages[URL], tools.root) == [tmp_path / (digest + suffix)]
    assert (tmp_path / (digest + suffix)).read_bytes() == raw
    before = list(tools.calls)
    view = tools.view_image(URL)
    assert view["_multimodal"] is True
    data_url = view["content"][1]["image_url"]["url"]
    assert data_url.startswith(f"data:{mime};base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == raw
    assert view["meta"]["raw_sha256"] == digest
    assert json.loads(view["text_summary"])["url"] == URL
    assert tools.calls == before and requests == [URL]
    assert tools.open(URL)["cache_hit"] and requests == [URL]


@pytest.mark.parametrize("mime,raw", [
    ("image/jpeg", b"<html>login</html>"),
    ("image/png", b"\xff\xd8\xffinvalid"),
    ("image/jpeg", b"\xff\xd8\xff" + b"x" * 32_000_000),
], ids=["html-login", "mime-mismatch", "oversized"])
def test_bad_image_response_is_not_passed_to_text_fallback(tmp_path, monkeypatch, mime, raw):
    tools, _, _ = image_tools(tmp_path, monkeypatch, mime, raw)
    result = tools.open(URL)
    assert not result["success"] and "error" in result
    assert result.get("provider") != "text_reader"
    assert not list(tmp_path.glob("*.jpeg")) and not list(tmp_path.glob("*.png"))


@pytest.mark.parametrize("damage", ["missing", "bytes", "mime", "size", "text"])
def test_cached_image_and_view_reject_corruption(tmp_path, monkeypatch, damage):
    tools, _, _ = image_tools(tmp_path, monkeypatch)
    first = tools.open(URL)
    assert first["provider"] == "direct_image"
    path = tmp_path / (first["raw_sha256"] + ".jpeg")
    if damage == "missing":
        path.unlink()
    elif damage == "bytes":
        path.write_bytes(b"corrupt")
    else:
        key, value = {"mime": ("mime_type", "image/png"),
                      "size": ("source_bytes", 1), "text": ("text", "虚构识别结果")}[damage]
        tools.pages[URL][key] = value
    assert tools.view_image(URL).startswith("error:")
    assert not tools.open(URL)["success"]


@pytest.mark.parametrize("mime", ["image/jpeg", "image/png"])
def test_restore_copies_image_and_keeps_view_bound_to_original(tmp_path, monkeypatch, mime):
    tools, requests, raw = image_tools(tmp_path / "old", monkeypatch, mime)
    tools.open(URL, limit=5)
    resumed = SearchTools(tmp_path / "new")
    resumed.restore(tools.root, origin="actual-trial", tool_results=bound_events(tools))
    view = resumed.view_image(URL)
    assert base64.b64decode(view["content"][1]["image_url"]["url"].split(",", 1)[1]) == raw
    assert resumed.open(URL)["cache_hit"] and requests == [URL]
    assert resumed.ready() is False


@pytest.mark.parametrize("side,field,value", [
    ("page", "mime_type", "image/png"), ("page", "source_bytes", 1),
    ("page", "resolved_url", "https://other.example.org/image"),
    ("call", "mime_type", "image/png"), ("call", "raw_sha256", "0" * 64),
    ("call", "source_bytes", 1), ("call", "resolved_url", "https://other.example.org/image"),
])
def test_restore_rejects_image_source_metadata_mismatch(
    tmp_path, monkeypatch, side, field, value,
):
    tools, _, _ = image_tools(tmp_path / "old", monkeypatch)
    tools.open(URL)
    events = bound_events(tools)
    if side == "page":
        path = tools.root / (hashlib.sha256(URL.encode()).hexdigest() + ".json")
        page = json.loads(path.read_text())
        page[field] = value
        path.write_text(json.dumps(page))
    else:
        tools.calls[0][field] = value
        (tools.root / "calls.jsonl").write_text(
            json.dumps(tools.calls[0], ensure_ascii=False) + "\n",
        )
        events = bound_events(tools)
    resumed = SearchTools(tmp_path / "new")
    with pytest.raises(ValueError):
        resumed.restore(tools.root, origin="actual-trial", tool_results=events)
    assert resumed.pages == {} and resumed.calls == []


def test_view_requires_opened_image_without_network(tmp_path, monkeypatch):
    tools = SearchTools(tmp_path)
    monkeypatch.setattr(tools, "_fetch", lambda _url: pytest.fail("查看不能发起下载"))
    assert tools.view_image(URL).startswith("error:")
    tools.pages[URL] = {"success": True, "url": URL, "text": "网页", "provider": "serper"}
    assert tools.view_image(URL).startswith("error:")
    assert not tools.calls


def test_large_image_is_preserved_but_not_embedded_or_resized(tmp_path, monkeypatch):
    raw = base64.b64decode(IMAGES["image/jpeg"])
    raw = raw[:-2] + b"x" * 3_200_000 + raw[-2:]
    tools, _, _ = image_tools(tmp_path, monkeypatch, raw=raw)
    opened = tools.open(URL)
    assert opened["success"] and opened["source_bytes"] == len(raw)
    assert "4 MiB" in tools.view_image(URL)
    assert (tmp_path / (opened["raw_sha256"] + ".jpeg")).read_bytes() == raw


def test_same_url_image_metadata_conflict_is_not_silently_replaced(tmp_path, monkeypatch):
    original, _, _ = image_tools(tmp_path / "old", monkeypatch)
    original.open(URL)
    resumed = SearchTools(tmp_path / "new")
    resumed.pages[URL] = {**original.pages[URL], "resolved_url": "https://other.example.org/image"}
    with pytest.raises(ValueError, match="同一来源快照内容冲突"):
        resumed.restore(original.root, origin="actual-trial", tool_results=bound_events(original))
    assert resumed.pages[URL]["resolved_url"] == "https://other.example.org/image"
    assert not resumed.calls


def test_image_magic_with_wrong_content_type_is_not_text(tmp_path, monkeypatch):
    raw = base64.b64decode(IMAGES["image/jpeg"])
    tools, _, _ = image_tools(tmp_path, monkeypatch, mime="text/html", raw=raw)
    result = tools.open(URL)
    assert not result["success"] and "MIME" in result["error"]
    assert result.get("provider") != "text_reader"


def test_image_content_length_mismatch_is_not_saved(tmp_path, monkeypatch):
    tools, _, _ = image_tools(tmp_path, monkeypatch, content_length=1)
    result = tools.open(URL)
    assert not result["success"] and "Content-Length" in result["error"]
    assert not list(tmp_path.glob("*.jpeg"))
