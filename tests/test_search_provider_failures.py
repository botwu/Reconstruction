"""真实服务错误应进入 agent 返回，不能退化成无法定位的 HTTP 400。"""

import io
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

from traceforge.reconstruction.search_tools import SearchTools


@pytest.mark.parametrize("operation", ["search", "open"])
def test_provider_credit_error_is_explained_and_retained(tmp_path, monkeypatch, operation):
    tools = SearchTools(tmp_path)
    tools._serper_key = "test-only"
    tools._fetch_provider = "serper"
    monkeypatch.setattr(tools, "_fetch_document", lambda url: None)
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


@pytest.mark.parametrize("provider,environment_override", [("serper", False), ("jina", True)])
def test_private_search_proxy_routes_only_its_own_requests(
    tmp_path, monkeypatch, provider, environment_override,
):
    from traceforge.reconstruction import search_tools

    proxy = "http://proxy-user:proxy-password@proxy.example:3128"
    config = tmp_path / "search.json"
    config.write_text(json.dumps({
        "proxy": proxy if not environment_override else "http://unused.example:3128",
        "serper_api_key": "test-serper", "jina_api_key": "test-jina",
        "fetch_provider": provider,
    }))
    monkeypatch.setenv("TRACEFORGE_SEARCH_CONFIG", str(config))
    monkeypatch.delenv("TRACEFORGE_SEARCH_PROXY", raising=False)
    for name in ("SERPER_API_KEY", "JINA_API_KEY", "TRACEFORGE_FETCH_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    if environment_override:
        monkeypatch.setenv("TRACEFORGE_SEARCH_PROXY", proxy)
    before_env = dict(os.environ)
    before_opener = urllib.request._opener
    requested, checked, redirects = [], [], []

    def respond(request, **kwargs):
        requested.append(request.full_url)
        if request.full_url == "https://example.org/static":
            raw = b"<html><p>Actual source text</p></html>"
            content_type = "text/html"
        else:
            bodies = {
                "https://google.serper.dev/search": {"organic": []},
                "https://scrape.serper.dev/": {"text": "Actual fetched text"},
                "https://r.jina.ai/https://example.org/dynamic": {
                    "data": {"content": "Actual fetched text"}},
            }
            raw = json.dumps(bodies.get(request.full_url, {})).encode()
            content_type = "application/json"
        response = io.BytesIO(raw)
        response.headers = {"Content-Type": content_type}
        response.geturl = lambda: request.full_url
        return response

    def build(*handlers):
        handler = next(h for h in handlers if isinstance(h, urllib.request.ProxyHandler))
        assert handler.proxies == {"http": proxy, "https": proxy}
        redirects.extend(h for h in handlers if isinstance(h, search_tools.PublicSourceRedirect))
        return SimpleNamespace(open=respond)

    monkeypatch.setattr(urllib.request, "build_opener", build)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: pytest.fail("不能全局路由"))
    monkeypatch.setattr(search_tools, "_public_url", lambda url, **kwargs: checked.append(url))
    tools = SearchTools(tmp_path / "outputs")

    assert tools.search("source query")["success"]
    assert tools.open("https://example.org/dynamic")["text"] == "Actual fetched text"
    assert tools.open("https://example.org/static")["provider"] == "direct_html"
    assert requested[0] == "https://google.serper.dev/search"
    assert checked == ["https://example.org/dynamic", "https://example.org/static"]
    assert len(redirects) == 3
    assert os.environ == before_env and urllib.request._opener is before_opener
    assert proxy not in (tools.root / "calls.jsonl").read_text()


@pytest.mark.parametrize("operation,http_error", [
    ("search", False), ("search", True), ("pdf", False),
])
def test_proxy_failures_do_not_disclose_proxy_credentials(
    tmp_path, monkeypatch, operation, http_error,
):
    from traceforge.reconstruction import search_tools

    proxy = "http://private-user:p%40ssword@proxy.example:3128"
    monkeypatch.setenv("TRACEFORGE_SEARCH_PROXY", proxy)
    monkeypatch.setenv("SERPER_API_KEY", "test-serper")
    monkeypatch.setenv("TRACEFORGE_SEARCH_CONFIG", str(tmp_path / "absent.json"))
    detail = f"Proxy failed: {proxy}; private-user; p%40ssword; p@ssword"

    def fail(request, **kwargs):
        if http_error:
            raise urllib.error.HTTPError(request.full_url, 407, "Proxy Authentication Required",
                                         {}, io.BytesIO(detail.encode()))
        raise urllib.error.URLError(detail)

    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *handlers: SimpleNamespace(open=fail))
    monkeypatch.setattr(search_tools, "_public_url", lambda url, **kwargs: None)
    tools = SearchTools(tmp_path / "outputs")
    result = (tools.search("source query") if operation == "search"
              else tools.open("https://example.org/source.pdf"))

    assert not result["success"] and not tools.ready()
    recorded = json.dumps(result) + (tools.root / "calls.jsonl").read_text()
    for secret in (proxy, "private-user", "p%40ssword", "p@ssword"):
        assert secret not in recorded
    assert "Proxy failed" in recorded and "[credential]" in recorded


@pytest.mark.parametrize("proxy", [
    "socks5://private-user:password@proxy.example:1080",
    "http://private-user:password@proxy.example:bad", {"unexpected": "value"},
])
def test_invalid_search_proxy_fails_without_echoing_configuration(tmp_path, monkeypatch, proxy):
    config = tmp_path / "search.json"
    config.write_text(json.dumps({"proxy": proxy}))
    monkeypatch.setenv("TRACEFORGE_SEARCH_CONFIG", str(config))
    monkeypatch.delenv("TRACEFORGE_SEARCH_PROXY", raising=False)
    with pytest.raises(ValueError, match="检索代理必须为有效 HTTP") as error:
        SearchTools(tmp_path / "outputs")
    assert "private-user" not in str(error.value) and "password" not in str(error.value)


def _mock_dns_and_pages(tmp_path, monkeypatch, *, local_address="2001::1", proxy=True,
                        overrides=None, redirect=None):
    """复现宿主受污染、代理侧公网解析正常；所有网络均为确定性替身。"""
    from traceforge.reconstruction import search_tools

    monkeypatch.setenv("TRACEFORGE_SEARCH_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.delenv("TRACEFORGE_SEARCH_PROXY", raising=False)
    if proxy:
        monkeypatch.setenv("TRACEFORGE_SEARCH_PROXY", "http://proxy.example:3128")
    requests = []

    def lookup(*args, **kwargs):
        if isinstance(local_address, Exception):
            raise local_address
        values = local_address if isinstance(local_address, list) else [local_address]
        return [(0, 0, 0, "", (address, 443)) for address in values]

    def build(*handlers):
        def respond(request, **kwargs):
            url = urllib.parse.urlsplit(request.full_url)
            requests.append(request.full_url)
            if url.hostname == "dns.google":
                assert any(isinstance(h, urllib.request.ProxyHandler) for h in handlers)
                query = urllib.parse.parse_qs(url.query)
                kind = query["type"][0]
                name = query["name"][0]
                answer = {"name": name + ".", "type": 1, "data": "23.62.212.194"}
                if kind == "AAAA":
                    answer = {"name": name + ".", "type": 5,
                              "data": "e35058.a.akamaiedge.net."}
                body = {"Status": 0, "TC": False,
                        "Question": [{"name": name + ".", "type": 1 if kind == "A" else 28}],
                        "Answer": [answer]}
                replacement = (overrides or {}).get((name, kind), (overrides or {}).get(kind))
                if isinstance(replacement, Exception):
                    raise replacement
                if replacement is not None:
                    body = (replacement if not isinstance(replacement, dict)
                            else {**body, **replacement})
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                content_type = "application/json"
            else:
                if redirect and url.hostname == "start.example":
                    handler = next(h for h in handlers
                                   if isinstance(h, search_tools.PublicSourceRedirect))
                    follow = handler.redirect_request(
                        request, None, 302, "Found", {}, redirect,
                    )
                    return respond(follow, **kwargs)
                raw = b"<html><p>Actual public fee page</p></html>"
                content_type = "text/html"
            response = io.BytesIO(raw)
            response.headers = {"Content-Type": content_type}
            response.geturl = lambda: request.full_url
            return response
        return SimpleNamespace(open=respond)

    monkeypatch.setattr(search_tools.socket, "getaddrinfo", lookup)
    monkeypatch.setattr(urllib.request, "build_opener", build)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **kw: pytest.fail("不得绕开实例代理"))
    return SearchTools(tmp_path / "outputs"), requests


@pytest.mark.parametrize("local_address", ["2001::1", ["23.62.212.194", "2001::1"]])
def test_proxy_recovers_real_tiktok_dns_pollution(tmp_path, monkeypatch, local_address):
    tools, requests = _mock_dns_and_pages(tmp_path, monkeypatch, local_address=local_address)
    result = tools.open("https://scm-us.tiktok.com/merchant-university/course-details/26930688006")
    assert result["success"] and result["text"] == "Actual public fee page"
    assert [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["type"][0]
            for u in requests if urllib.parse.urlsplit(u).hostname == "dns.google"] == ["A", "AAAA"]
    assert [call["tool"] for call in tools.calls] == ["web_open"]
    assert len(list(tools.root.glob("*.raw"))) == 3


@pytest.mark.parametrize("local_address", [
    "23.62.212.194", ["23.62.212.194", "2606:4700:4700::1111"],
])
def test_public_local_dns_keeps_existing_fetch_path(tmp_path, monkeypatch, local_address):
    tools, requests = _mock_dns_and_pages(tmp_path, monkeypatch, local_address=local_address)
    assert tools.open("https://scm-us.tiktok.com/page")["success"]
    assert not any("dns.google" in url for url in requests)


def test_proxy_public_dns_recovers_local_lookup_failure(tmp_path, monkeypatch):
    tools, _ = _mock_dns_and_pages(
        tmp_path, monkeypatch, local_address=socket.gaierror("DNS unavailable"),
    )
    assert tools.open("https://scm-us.tiktok.com/page")["success"]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/private", "http://10.0.0.1/private",
    "http://[::1]/private", "http://[2001::1]/private",
])
def test_private_ip_literals_never_use_dns_fallback(tmp_path, monkeypatch, url):
    tools, requests = _mock_dns_and_pages(tmp_path, monkeypatch)
    assert not tools.open(url)["success"]
    assert requests == []


@pytest.mark.parametrize("local_address", ["2001::1", socket.gaierror("DNS unavailable")])
def test_nonpublic_local_dns_without_proxy_stays_rejected(tmp_path, monkeypatch, local_address):
    tools, requests = _mock_dns_and_pages(
        tmp_path, monkeypatch, proxy=False, local_address=local_address,
    )
    assert not tools.open("https://scm-us.tiktok.com/page")["success"]
    assert requests == []


@pytest.mark.parametrize("bad_aaaa", [
    {"Status": 2},
    {"TC": True},
    {"Question": [{"name": "other.example.", "type": 28}]},
    {"Answer": [{"name": "scm-us.tiktok.com.", "type": 28, "data": "::1"}]},
    {"Answer": [{"name": "scm-us.tiktok.com.", "type": 28, "data": "not-an-address"}]},
    {"Answer": "not-a-record-list"},
    b"not json",
    urllib.error.URLError("DNS service unavailable"),
])
def test_dns_fallback_never_ignores_bad_aaaa(tmp_path, monkeypatch, bad_aaaa):
    tools, requests = _mock_dns_and_pages(
        tmp_path, monkeypatch, overrides={"AAAA": bad_aaaa},
    )
    result = tools.open("https://scm-us.tiktok.com/page")
    assert not result["success"] and not tools.ready()
    assert all(urllib.parse.urlsplit(u).hostname == "dns.google" for u in requests)


def test_dns_fallback_requires_at_least_one_global_address(tmp_path, monkeypatch):
    tools, requests = _mock_dns_and_pages(
        tmp_path, monkeypatch, overrides={"A": {"Answer": []}, "AAAA": {"Answer": []}},
    )
    assert not tools.open("https://scm-us.tiktok.com/page")["success"]
    assert all(urllib.parse.urlsplit(u).hostname == "dns.google" for u in requests)


@pytest.mark.parametrize("redirect,success", [
    ("https://seller-us.tiktok.com/page", True),
    ("http://127.0.0.1/private", False),
])
def test_redirect_reuses_public_dns_policy(tmp_path, monkeypatch, redirect, success):
    tools, requests = _mock_dns_and_pages(
        tmp_path, monkeypatch, redirect=redirect,
    )
    result = tools.open("https://start.example/page")
    assert result["success"] is success
    if success:
        assert result["resolved_url"] == redirect
    else:
        assert redirect not in requests


def test_public_ip_literal_does_not_need_dns(tmp_path, monkeypatch):
    tools, requests = _mock_dns_and_pages(tmp_path, monkeypatch)
    assert tools.open("https://23.62.212.194/page")["success"]
    assert requests == ["https://23.62.212.194/page"]


@pytest.mark.parametrize("bypass_host", ["scm-us.tiktok.com", "dns.google"])
def test_dns_fallback_cannot_bypass_configured_proxy(tmp_path, monkeypatch, bypass_host):
    tools, requests = _mock_dns_and_pages(tmp_path, monkeypatch)
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: host == bypass_host)
    assert not tools.open("https://scm-us.tiktok.com/page")["success"]
    assert requests == []


def test_redirect_rejects_nonpublic_dns_answer(tmp_path, monkeypatch):
    target = "https://blocked.example/private"
    tools, requests = _mock_dns_and_pages(
        tmp_path, monkeypatch, redirect=target,
        overrides={("blocked.example", "AAAA"): {
            "Answer": [{"name": "blocked.example.", "type": 28, "data": "::1"}],
        }},
    )
    assert not tools.open("https://start.example/page")["success"]
    assert target not in requests
