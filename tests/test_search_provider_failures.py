"""真实服务错误应进入 agent 返回，不能退化成无法定位的 HTTP 400。"""

import io
import urllib.error

import pytest

from traceforge.reconstruction.search_tools import SearchTools


@pytest.mark.parametrize("operation", ["search", "open"])
def test_provider_credit_error_is_explained_and_retained(tmp_path, monkeypatch, operation):
    tools = SearchTools(tmp_path)
    tools._serper_key = "test-only"
    tools._fetch_provider = "serper"
    monkeypatch.setattr(tools, "_fetch_pdf", lambda url: None)
    monkeypatch.setattr("traceforge.reconstruction.search_tools._public_url", lambda url: None)
    raw = b'{"message":"Not enough credits","statusCode":400}'

    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(raw))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = tools.search("public package") if operation == "search" else tools.open(
        "https://example.org/paper",
    )

    assert result["success"] is False
    assert "HTTP 400" in result["error"]
    assert "Not enough credits" in result["error"]
    assert next(tmp_path.glob("*.error.raw")).read_bytes() == raw


def test_provider_error_does_not_echo_configured_credential(tmp_path, monkeypatch):
    tools = SearchTools(tmp_path)
    tools._serper_key = "private-test-token"
    raw = b"rejected private-test-token"

    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(raw))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = tools.search("public package")
    assert result["success"] is False
    assert tools._serper_key not in result["error"]
    assert "[credential]" in result["error"]


def test_cached_page_does_not_claim_live_fetch_available(tmp_path):
    tools = SearchTools(tmp_path)
    tools.calls.append({"tool": "web_search", "success": True, "results": []})
    tools.pages["https://example.org/paper"] = {"success": True, "text": "原已抓取正文"}

    result = tools.open("https://example.org/paper")

    assert result["success"] is True and result["cache_hit"] is True
    assert tools.ready() is False


@pytest.mark.parametrize("failed_kind", ["web_search", "web_open"])
def test_latest_service_failure_is_not_masked_by_prior_success_or_cache(
    tmp_path, failed_kind,
):
    tools = SearchTools(tmp_path)
    tools.calls.extend([
        {"tool": "web_search", "success": True, "results": []},
        {"tool": "web_open", "success": True, "text": "真实网页"},
    ])
    assert tools.ready() is True
    tools.calls.append({"tool": failed_kind, "success": False, "error": "Not enough credits"})
    tools.pages["https://example.org/paper"] = {"success": True, "text": "旧正文"}
    tools.open("https://example.org/paper")
    assert tools.ready() is False


def test_live_fetch_then_pagination_keeps_actual_fetch_evidence(tmp_path, monkeypatch):
    tools = SearchTools(tmp_path)
    tools.calls.append({"tool": "web_search", "success": True, "results": []})
    monkeypatch.setattr(tools, "_fetch", lambda url: {"success": True, "text": "实际连续正文"})

    assert tools.open("https://example.org/paper", limit=2)["cache_hit"] is False
    assert tools.open("https://example.org/paper", offset=2)["cache_hit"] is True
    assert tools.ready() is True
