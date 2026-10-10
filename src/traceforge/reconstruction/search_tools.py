"""真实公开检索与页面快照；失败保留为工具返回，不生成替代证据。"""

from __future__ import annotations

import base64
import codecs
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


def _public_url(
    url: str, resolve: Callable[[str], list[str]] | None = None,
) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None):
        raise ValueError("来源必须为公开 HTTP(S) 地址")
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValueError("来源不能指向本机或私有网络")
        return
    try:
        addresses = [item[4][0] for item in socket.getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM,
        )]
    except OSError:
        if resolve is None:
            raise
        addresses = []
    if addresses and all(ipaddress.ip_address(value).is_global for value in addresses):
        return
    if resolve is not None:
        addresses = resolve(parsed.hostname.encode("idna").decode("ascii"))
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("来源不能指向本机或私有网络")


def _wire_url(url: str) -> str:
    """只编码传输地址，保留分隔符和已有转义；原调用与缓存仍按原 URL 记账。"""
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname or parsed.username is not None:
        raise ValueError("来源地址必须包含主机且不能含用户信息")
    host = parsed.hostname.encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None:
        host += f":{parsed.port}"
    return urllib.parse.quote(
        urllib.parse.urlunsplit(parsed._replace(netloc=host)), safe=":/?#[]@!$&'()*+,;=%",
    )


class PublicSourceRedirect(urllib.request.HTTPRedirectHandler):
    """逐跳检查公开来源，避免重定向绕过原地址检查。"""

    def __init__(self, validate: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self._validate = validate

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int,
        msg: str, headers: Any, newurl: str,
    ) -> urllib.request.Request | None:
        newurl = _wire_url(newurl)
        (self._validate or _public_url)(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _StaticHTML(HTMLParser):
    """仅提取原 HTML 中已有的文字和链接，不执行脚本或猜测下载地址。"""

    def __init__(self, url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = url
        self.has_base = False
        self.skipped = 0
        self.in_title = False
        self.parts: list[str] = []
        self.title: list[str] = []
        self.links: list[dict[str, str]] = []
        self.anchor: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "style", "noscript", "template"}:
            self.skipped += 1
        if self.skipped:
            return
        if tag == "title":
            self.in_title = True
        href = values.get("href")
        if tag == "base" and href and not self.has_base:
            self.base_url = urllib.parse.urljoin(self.base_url, href)
            self.has_base = True
        source = tag
        if tag == "meta" and (values.get("name") or "").lower() == "citation_pdf_url":
            href, source = values.get("content"), "citation_pdf_url"
        if href and source in {"a", "link", "citation_pdf_url"}:
            url = urllib.parse.urljoin(self.base_url, href)
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username:
                try:
                    link_url = _wire_url(url)
                except (ValueError, UnicodeError):
                    return
                link = {"url": link_url, "text": "", "source": source}
                self.links.append(link)
                if tag == "a":
                    self.anchor = link

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self.skipped = max(0, self.skipped - 1)
        if tag == "title":
            self.in_title = False
        if tag == "a":
            self.anchor = None

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if self.skipped or not text:
            return
        (self.title if self.in_title else self.parts).append(text)
        if self.anchor is not None:
            self.anchor["text"] = " ".join(filter(None, [self.anchor["text"], text]))


def _extract_static_html(raw: bytes, *, encoding: str, url: str) -> dict[str, Any]:
    encoding = codecs.lookup(encoding).name
    parser = _StaticHTML(url)
    parser.feed(raw.decode(encoding))
    parser.close()
    return {
        "title": " ".join(parser.title), "text": "\n".join(parser.parts), "links": parser.links,
        "encoding": encoding, "resolved_url": url, "provider": "direct_html",
        "content_kind": "page_text", "extractor": "stdlib.html.parser/static-v1",
        "extraction_scope": "static_html",
        "limitations": ["仅提取原始 HTML 的静态文字与链接，未执行 JavaScript 或验证链接内容；"
                        "可能含导航文字，不保证页面包含论文全文。"],
    }


def _validate_html_page(page: dict[str, Any], root: Path) -> None:
    digest = page.get("raw_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("HTML 原始抓取哈希无效")
    raw = (root / f"{digest}.raw").read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("HTML 原始抓取哈希不匹配")
    if not isinstance(page.get("encoding"), str) or not isinstance(page.get("resolved_url"), str):
        raise ValueError("HTML 缓存缺少原始编码或最终网址")
    actual = _extract_static_html(raw, encoding=page["encoding"], url=page["resolved_url"])
    if any(page.get(key) != value for key, value in actual.items()):
        raise ValueError("HTML 缓存正文、链接或提取信息与原始抓取不一致")


def _verified_pdf_asset(root: Path, digest: Any, suffix: str) -> Path:
    """原 PDF 与 OCR 派生物使用同一哈希校验，不能按文件名冒认原件。"""
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("PDF/OCR 来源哈希格式无效")
    path = root / (digest + suffix)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"PDF/OCR 来源缺失或哈希不匹配：{path.name}")
    return path


def pdf_assets(page: dict[str, Any], root: Path | None) -> list[Path]:
    """核对直接抓取的 PDF 与已有 OCR；检查点恢复与 Harbor 交付共用此契约。"""
    if page.get("provider") != "direct_pdf" and not page.get("ocr_pages"):
        return []
    if root is None:
        raise ValueError("PDF/OCR 交付缺少原 PDF、页图和识别原始返回所在目录")
    pdf = _verified_pdf_asset(root, page.get("raw_sha256"), ".pdf")
    assets = {pdf}
    for number, item in (page.get("ocr_pages") or {}).items():
        if (not isinstance(item, dict) or item.get("schema_version") != "traceforge.pdf-ocr-page.v1"
                or str(item.get("page_number")) != number
                or item.get("source_pdf_sha256") != page.get("raw_sha256")
                or item.get("page_count") != page.get("page_count")):
            raise ValueError("OCR 页面与原 PDF 的绑定不匹配")
        paths = [pdf]
        for field, suffix in (("image_sha256", ".png"), ("ocr_raw_sha256", ".ocr.raw")):
            paths.append(_verified_pdf_asset(root, item.get(field), suffix))
        if json.loads(paths[-1].read_bytes()) != {
                key: value for key, value in item.items() if key != "ocr_raw_sha256"}:
            raise ValueError("OCR 文本块与实际识别原始返回不一致")
        assets.update(paths)
    return sorted(assets)


def _page_view(page: dict[str, Any], ocr_page: int | None) -> dict[str, Any]:
    value = {key: val for key, val in page.items() if key != "ocr_pages"}
    if ocr_page is not None:
        item = (page.get("ocr_pages") or {}).get(str(ocr_page))
        if not isinstance(item, dict):
            raise ValueError("实际 OCR 分页对应的缓存缺失")
        value.update(
            text=json.dumps(item["blocks"], ensure_ascii=False), ocr_page=ocr_page,
            content_kind="pdf_ocr", extraction_scope="positioned_ocr_blocks",
            **{key: item[key] for key in ("image_sha256", "ocr_raw_sha256", "limitations",
                                          "versions", "model_sha256", "image_size")},
        )
    return value


class SearchTools:
    """同一次运行的分页固定使用首次抓取的页面，记录真实来源和抓取时间。"""

    def __init__(self, output_root: Path) -> None:
        self.root = output_root
        self.root.mkdir(parents=True, exist_ok=True)
        self.pages: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        config_path = Path(os.environ.get(
            "TRACEFORGE_SEARCH_CONFIG", str(Path.home() / ".config/traceforge/search.json"),
        ))
        config = json.loads(config_path.read_text()) if config_path.is_file() else {}
        self._ocr_python = os.environ.get("TRACEFORGE_OCR_PYTHON") or config.get("ocr_python")
        self._serper_key = os.environ.get("SERPER_API_KEY") or config.get("serper_api_key")
        self._jina_key = os.environ.get("JINA_API_KEY") or config.get("jina_api_key")
        self._fetch_provider = os.environ.get("TRACEFORGE_FETCH_PROVIDER") or config.get(
            "fetch_provider", "jina",
        )
        self._proxy = os.environ.get("TRACEFORGE_SEARCH_PROXY") or config.get("proxy")
        self._proxy_secrets: set[str] = set()
        self._proxy_opener = None
        if self._proxy:
            try:
                if not isinstance(self._proxy, str):
                    raise ValueError
                parsed = urllib.parse.urlsplit(self._proxy)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                    raise ValueError
                _ = parsed.port
            except (TypeError, ValueError):
                raise ValueError("检索代理必须为有效 HTTP(S) 代理地址") from None
            self._proxy_secrets.add(self._proxy)
            for value in (parsed.username, parsed.password):
                if value:
                    self._proxy_secrets.update((value, urllib.parse.unquote(value)))
            if parsed.username is not None and parsed.password is not None:
                credentials = urllib.parse.unquote(parsed.username + ":" + parsed.password)
                self._proxy_secrets.add(base64.b64encode(credentials.encode()).decode())
            self._proxy_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self._proxy, "https": self._proxy}),
                PublicSourceRedirect(),
            )

    def _check_public_url(self, url: str) -> None:
        if self._proxy and not urllib.request.proxy_bypass(urllib.parse.urlsplit(url).netloc):
            _public_url(url, resolve=self._resolve_public_dns)
        else:
            _public_url(url)

    def _resolve_public_dns(self, hostname: str) -> list[str]:
        """宿主解析异常时，经既有代理核对两类地址；失败不放行。"""
        if urllib.request.proxy_bypass("dns.google"):
            raise ValueError("公网 DNS 验证须经已配置代理，dns.google 不能被 NO_PROXY 绕过")
        addresses = []
        for kind, record_type in (("A", 1), ("AAAA", 28)):
            query = urllib.parse.urlencode({"name": hostname, "type": kind})
            data, digest = self._response(urllib.request.Request(
                "https://dns.google/resolve?" + query,
                headers={"Accept": "application/dns-json"},
            ))
            context = f"公网 DNS {hostname} {kind}，回执 {digest}.raw"
            if (not isinstance(data, dict) or type(data.get("Status")) is not int
                    or data["Status"] != 0 or data.get("TC") is not False):
                raise ValueError(f"{context}：查询失败或响应不完整")
            questions = data.get("Question")
            if (not isinstance(questions, list) or len(questions) != 1
                    or not isinstance(questions[0], dict)
                    or str(questions[0].get("name", "")).rstrip(".").lower()
                    != hostname.rstrip(".").lower()
                    or questions[0].get("type") != record_type):
                raise ValueError(f"{context}：响应不属于当前查询")
            answers = data.get("Answer", [])
            if not isinstance(answers, list):
                raise ValueError(f"{context}：地址记录格式无效")
            for answer in answers:
                if not isinstance(answer, dict) or type(answer.get("type")) is not int:
                    raise ValueError(f"{context}：地址记录格式无效")
                if answer["type"] not in {1, 28}:
                    continue
                try:
                    if not isinstance(answer.get("data"), str):
                        raise ValueError
                    address = ipaddress.ip_address(answer["data"])
                except ValueError:
                    raise ValueError(f"{context}：地址记录不是有效 IP") from None
                if address.version != (4 if answer["type"] == 1 else 6) or not address.is_global:
                    raise ValueError(f"{context}：包含非公网或类型不符的地址")
                addresses.append(str(address))
        if not addresses:
            raise ValueError(f"公网 DNS {hostname} 未返回任何公网地址")
        return addresses

    def _error_detail(self, value: str) -> str:
        """只清除错误诊断中的部署凭据；原始来源内容保持不变。"""
        secrets = self._proxy_secrets | {self._serper_key, self._jina_key}
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            value = value.replace(secret, "[credential]")
        return value

    def _record(self, kind: str, value: dict[str, Any]) -> dict[str, Any]:
        entry = {"tool": kind, "retrieved_at": datetime.now(UTC).isoformat(), **value}
        self.calls.append(entry)
        with (self.root / "calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def _response(self, request: urllib.request.Request) -> tuple[dict[str, Any], str]:
        try:
            open_request = self._proxy_opener.open if self._proxy_opener else urllib.request.urlopen
            with open_request(request, timeout=90) as response:
                raw = response.read(8_000_001)
        except urllib.error.HTTPError as exc:
            raw = exc.read(8_000_001)
            digest = hashlib.sha256(raw).hexdigest()
            (self.root / f"{digest}.error.raw").write_bytes(raw)
            detail = self._error_detail(raw.decode("utf-8", errors="replace"))
            suffix = "（错误正文展示已截断）" if len(detail) > 2000 else ""
            raise ValueError(
                f"检索服务 HTTP {exc.code}: {detail[:2000]}{suffix}; "
                f"错误回执 {digest}.error.raw"
            ) from exc
        if len(raw) > 8_000_000:
            raise ValueError("返回超过 8 MB，未将不完整下载冒充完整来源")
        digest = hashlib.sha256(raw).hexdigest()
        (self.root / f"{digest}.raw").write_bytes(raw)
        return json.loads(raw), digest

    def _search(self, query: str) -> tuple[list[dict[str, Any]], str]:
        if not self._serper_key:
            raise ValueError("未配置 Serper 凭据")
        request = urllib.request.Request(
            "https://google.serper.dev/search",
            data=json.dumps({"q": query, "num": 8}).encode(),
            headers={"X-API-KEY": self._serper_key, "Content-Type": "application/json"},
        )
        response, digest = self._response(request)
        if not isinstance(response.get("organic"), list):
            raise ValueError("Serper 返回缺少 organic 检索结果")
        return response["organic"], digest

    def search(self, query: str) -> dict[str, Any]:
        try:
            results, digest = self._search(query)
            value = {"success": True, "query": query, "results": results,
                     "source_mode": "live_search", "provider": "serper",
                     "content_kind": "search_snippets", "raw_sha256": digest}
        except Exception as exc:
            value = {"success": False, "query": query,
                     "error": f"{type(exc).__name__}: {self._error_detail(str(exc))}"}
        return self._record("web_search", value)

    def _fetch(self, url: str) -> dict[str, Any]:
        self._check_public_url(url)
        document = self._fetch_document(url)
        if document is not None:
            return document
        parsed = urllib.parse.urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname == "raw.githubusercontent.com" and len(parts) >= 4:
            owner, repo, ref, *file_parts = parts
        elif parsed.hostname == "github.com" and len(parts) >= 5 and parts[2] == "blob":
            owner, repo, _, ref, *file_parts = parts
        else:
            return self._fetch_page(url)
        # 网页文本提取会压平源码；内容 API 的编码可保留原字节并校验 Git 对象。
        api_url = (f"https://api.github.com/repos/{owner}/{repo}/contents/"
                   + "/".join(file_parts) + "?ref=" + urllib.parse.quote(urllib.parse.unquote(ref), safe=""))
        self._check_public_url(api_url)
        page = self._fetch_page(api_url)
        data = json.loads(page["text"])
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise ValueError("GitHub 内容 API 未返回完整文件，不能使用网页抽取文本替代源码")
        raw = base64.b64decode("".join(data["content"].split()), validate=True)
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if len(raw) != data.get("size") or blob != data.get("sha"):
            raise ValueError("GitHub 文件大小或 Git blob 哈希不一致")
        return {**page, "url": url, "resolved_url": api_url, "text": raw.decode("utf-8"),
                "content_kind": "source_file", "content_sha256": hashlib.sha256(raw).hexdigest(),
                "git_blob_sha1": blob, "source_ref": urllib.parse.unquote(ref)}

    def _fetch_document(self, url: str) -> dict[str, Any] | None:
        # 单次原站请求按实际响应区分 PDF 与静态 HTML；其他内容仍由既定读取服务处理。
        pdf_expected = urllib.parse.urlsplit(url).path.lower().endswith(".pdf")
        request = urllib.request.Request(_wire_url(url), headers={"User-Agent": "TraceForge/0.3"})
        handlers: list[urllib.request.BaseHandler] = [PublicSourceRedirect(self._check_public_url)]
        if self._proxy:
            handlers.append(urllib.request.ProxyHandler(
                {"http": self._proxy, "https": self._proxy},
            ))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(request, timeout=90 if pdf_expected else 20) as response:
                prefix = response.read(5)
                content_type = response.headers.get("Content-Type", "")
                pdf_expected = pdf_expected or "application/pdf" in content_type.lower()
                if prefix != b"%PDF-":
                    if pdf_expected:
                        raise ValueError("来源没有返回 PDF 原文件，不能把登录页或错误页面当正文")
                    if content_type.split(";", 1)[0].strip().lower() != "text/html":
                        return None
                    if urllib.parse.urlsplit(url).hostname in {
                            "github.com", "raw.githubusercontent.com"}:
                        return None
                    resolved = urllib.parse.urlsplit(response.geturl())
                    if resolved.hostname in {"github.com", "raw.githubusercontent.com"}:
                        raise ValueError("GitHub 来源须通过内容 API 校验，不能采用 HTML 静态正文")
                    raw = prefix + response.read(8_000_001 - len(prefix))
                    if len(raw) > 8_000_000:
                        raise ValueError("HTML 超过 8 MB，未采用不完整下载")
                    digest = hashlib.sha256(raw).hexdigest()
                    (self.root / f"{digest}.raw").write_bytes(raw)
                    charset = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, re.I)
                    if charset is None:
                        charset = re.search(
                            r"<meta\b[^>]*\bcharset\s*=\s*['\"]?([\w.-]+)",
                            raw[:8192].decode("ascii", errors="ignore"), re.I,
                        )
                    page = _extract_static_html(
                        raw, encoding=charset.group(1) if charset else "utf-8",
                        url=response.geturl(),
                    )
                    if not page["text"]:
                        return None
                    return {**page, "success": True, "url": url, "raw_sha256": digest,
                            "source_mode": "live_page",
                            "retrieved_at": datetime.now(UTC).isoformat()}
                pdf_expected = True
                raw = prefix + response.read(32_000_001 - len(prefix))
                resolved_url = response.geturl()
        except OSError:
            if pdf_expected:
                raise
            return None
        if len(raw) > 32_000_000:
            raise ValueError("PDF 超过 32 MB，未采用不完整下载")
        from pypdf import PdfReader, __version__

        digest = hashlib.sha256(raw).hexdigest()
        (self.root / f"{digest}.pdf").write_bytes(raw)
        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
        return {
            "success": True, "url": url, "resolved_url": resolved_url,
            "title": str((reader.metadata or {}).get("/Title") or ""),
            "text": "\n\n".join(f"[PDF 第 {i + 1} 页]\n{text}" for i, text in enumerate(pages)),
            "page_count": len(pages),
            "empty_text_pages": [i + 1 for i, text in enumerate(pages) if not text.strip()],
            "extraction_scope": "text_layer",
            "limitations": ["仅提取 PDF 文本层；图片、图表布局与公式未通过视觉核对，空文本页未做 OCR。"],
            "raw_sha256": digest, "provider": "direct_pdf", "extractor": f"pypdf/{__version__}",
            "source_mode": "live_page", "content_kind": "pdf_text",
            "retrieved_at": datetime.now(UTC).isoformat(),
        }

    def _fetch_page(self, url: str) -> dict[str, Any]:
        if self._fetch_provider == "serper":
            if not self._serper_key:
                raise ValueError("未配置 Serper 凭据")
            request = urllib.request.Request(
                "https://scrape.serper.dev/",
                data=json.dumps({"url": url, "includeMarkdown": True}).encode(),
                headers={"X-API-KEY": self._serper_key, "Content-Type": "application/json"},
            )
            data, digest = self._response(request)
            body_format = ("markdown" if isinstance(data.get("markdown"), str)
                           and data["markdown"].strip() else "text")
            text = data.get(body_format)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Serper 未返回可读正文")
            return {"success": True, "url": url, "text": text, "body_format": body_format,
                    "title": (data.get("metadata") or {}).get("title", ""),
                    "metadata": data.get("metadata", {}), "raw_sha256": digest,
                    **({"jsonld": data["jsonld"]} if "jsonld" in data else {}),
                    "provider": "serper", "source_mode": "live_page", "content_kind": "page_text",
                    "retrieved_at": datetime.now(UTC).isoformat()}
        if self._fetch_provider != "jina":
            raise ValueError("fetch_provider 仅支持 jina 或 serper")
        if not self._jina_key:
            raise ValueError("未配置 Jina 凭据")
        request = urllib.request.Request(
            "https://r.jina.ai/" + urllib.parse.quote(url, safe=":/?=&%#"),
            headers={"Authorization": "Bearer " + self._jina_key,
                     "Accept": "application/json", "X-Timeout": "60"},
        )
        response, digest = self._response(request)
        data = response.get("data") or {}
        text = str(data.get("content") or "")
        if not text.strip():
            raise ValueError("Jina 未返回可读正文")
        return {"success": True, "url": url, "resolved_url": data.get("url", url),
                "title": data.get("title", ""), "text": text, "raw_sha256": digest,
                "provider": "jina", "published_time": data.get("publishedTime"),
                "source_mode": "live_page", "content_kind": "page_text",
                "retrieved_at": datetime.now(UTC).isoformat()}

    def _ocr(self, page: dict[str, Any], number: int) -> None:
        if type(number) is not int or not 1 <= number <= page.get("page_count", 0):
            raise ValueError("ocr_page 必须为 PDF 范围内从 1 开始的整数页号")
        if str(number) in (page.get("ocr_pages") or {}):
            pdf_assets(page, self.root)
            return
        if not self._ocr_python:
            raise ValueError("OCR 未配置：需要 TRACEFORGE_OCR_PYTHON 或 search.json 的 ocr_python")
        pdf = _verified_pdf_asset(self.root, page.get("raw_sha256"), ".pdf")
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [self._ocr_python, str(Path(__file__).with_name("pdf_ocr.py")), str(pdf),
             str(number), str(self.root)],
            capture_output=True, text=True, env=env, timeout=180,
        )
        if result.returncode:
            raise ValueError(f"OCR 工作进程不可用：{result.stderr[-2000:]}")
        raw = result.stdout.encode()
        digest = hashlib.sha256(raw).hexdigest()
        (self.root / (digest + ".ocr.raw")).write_bytes(raw)
        item = {**json.loads(raw), "ocr_raw_sha256": digest}
        candidate = {**page, "ocr_pages": {**page.get("ocr_pages", {}), str(number): item}}
        pdf_assets(candidate, self.root)
        page["ocr_pages"] = candidate["ocr_pages"]

    def open(self, url: str, *, offset: int = 0, limit: int = 8000,
             ocr_page: int | None = None) -> dict[str, Any]:
        cache_hit = url in self.pages
        try:
            if not cache_hit:
                self.pages[url] = self._fetch(url)
            page = self.pages[url]
            if cache_hit and page.get("provider") == "direct_html":
                _validate_html_page(page, self.root)
            if ocr_page is not None:
                self._ocr(page, ocr_page)
            name = hashlib.sha256(url.encode()).hexdigest() + ".json"
            (self.root / name).write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")
            if (ocr_page is None and page.get("page_count")
                    and len(page.get("empty_text_pages", [])) == page["page_count"]):
                raise ValueError("PDF 没有可读取的文本层，需要显式 ocr_page 或其他真实来源；未生成正文")
            page = _page_view(page, ocr_page)
            text = page["text"]
            offset, limit = max(0, int(offset)), max(1, min(8000, int(limit)))
            value = {**page, "text": text[offset:offset + limit], "total_chars": len(text),
                     "offset": offset, "next_offset": offset + limit
                     if offset + limit < len(text) else None}
        except Exception as exc:
            value = {"success": False, "url": url, "offset": offset, "limit": limit,
                     **({"ocr_page": ocr_page} if ocr_page is not None else {}),
                     "error": f"{type(exc).__name__}: {self._error_detail(str(exc))}"}
        return self._record("web_open", {**value, "cache_hit": cache_hit})

    def restore(
        self, root: Path, *, origin: str,
        tool_results: list[dict[str, Any]] | None = None,
        checkpoint_files: dict[str, str] | None = None,
    ) -> None:
        """恢复真实快照及调用；导入 solver 时必须绑定完整工具回执。"""
        try:
            if tool_results is None:
                if checkpoint_files is None:
                    raise ValueError("恢复须提供完整工具回执或已绑定的检查点文件清单")
                actual_files = {path.name for path in root.iterdir() if path.is_file()}
                if actual_files != set(checkpoint_files):
                    raise ValueError(f"检查点检索文件清单不匹配：{root}")
                for name, digest in checkpoint_files.items():
                    if Path(name).name != name:
                        raise ValueError(f"检查点检索文件不是内部文件名：{name}")
                    if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                        raise ValueError(f"检查点检索文件哈希不匹配：{name}")
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            pages = {}
            raw_files = [*root.glob("*.raw"), *root.glob("*.pdf"), *root.glob("*.png")]
            for path in raw_files:
                if hashlib.sha256(path.read_bytes()).hexdigest() != path.name.split(".")[0]:
                    raise ValueError(f"原始抓取哈希不匹配：{path}")
            bindings = []
            if tool_results is not None:
                returned = []
                for event in tool_results:
                    if event.get("name") not in {"web_open", "web_search"}:
                        continue
                    result = event.get("result")
                    if (not isinstance(result, str)
                            or hashlib.sha256(result.encode()).hexdigest()
                            != event.get("result_sha256")):
                        raise ValueError(f"检索完整工具回执哈希不匹配：{origin}")
                    returned.append(json.loads(result))
                    bindings.append({
                        "tool_call_id": event["tool_call_id"],
                        "tool_result_sha256": event["result_sha256"],
                        **{key: event[key] for key in ("native_result_sha256", "native_tool_name")
                           if key in event},
                    })
                if calls != returned:
                    raise ValueError(f"检索缓存调用与完整工具回执不匹配：{origin}")
            for path in root.glob("*.json"):
                page = json.loads(path.read_text())
                url = page.get("url")
                if (not isinstance(url, str) or not isinstance(page.get("text"), str)
                        or not page.get("success")
                        or path.name != hashlib.sha256(url.encode()).hexdigest() + ".json"):
                    raise ValueError(f"检索页面缓存无效：{path}")
                digest = page.get("raw_sha256")
                raw_path = root / f"{digest}.raw"
                if page.get("provider") == "direct_pdf":
                    raw_path = root / f"{digest}.pdf"
                if not raw_path.is_file():
                    raise ValueError(f"检索原始抓取缺失：{raw_path}")
                if page.get("provider") == "direct_html":
                    _validate_html_page(page, root)
                elif page.get("content_kind") == "page_text":
                    data = json.loads(raw_path.read_bytes())
                    if page.get("provider") == "serper":
                        body_format = page.get("body_format", "text")
                        if body_format not in {"text", "markdown"}:
                            raise ValueError(f"Serper 缓存正文格式无效：{path}")
                        text = data.get(body_format)
                    else:
                        text = (data.get("data") or {}).get("content")
                    if text != page["text"]:
                        raise ValueError(f"缓存正文与原始抓取不一致：{path}")
                    if page.get("provider") == "serper" and "jsonld" in data:
                        page["jsonld"] = data["jsonld"]
                pdf_assets(page, root)
                existing = self.pages.get(url)
                fields = ("raw_sha256", "text", "metadata", "jsonld", "content_kind", "source_ref")
                if page.get("provider") == "direct_html":
                    fields += ("title", "links", "encoding", "resolved_url")
                if existing and (
                    any(existing.get(key) != page.get(key) for key in fields)
                    or existing.get("body_format", "text") != page.get("body_format", "text")
                ):
                    raise ValueError(f"同一来源快照内容冲突：{url}（{origin}）")
                for number, item in (existing or {}).get("ocr_pages", {}).items():
                    if number in page.get("ocr_pages", {}) and page["ocr_pages"][number] != item:
                        raise ValueError(f"同一 PDF 的 OCR 页面冲突：{url}，{number}")
                pages[url] = page
            for call in calls:
                if not isinstance(call, dict) or call.get("tool") not in {"web_open", "web_search"}:
                    raise ValueError(f"检索调用缓存无效：{origin}")
                if not call.get("success"):
                    continue
                digest = call.get("raw_sha256")
                if not any(path.name == f"{digest}.raw" or path.name == f"{digest}.pdf"
                           for path in raw_files):
                    raise ValueError(f"检索调用原始抓取缺失：{origin}，{digest}")
                if call["tool"] == "web_search":
                    raw_search = json.loads((root / f"{digest}.raw").read_bytes())
                    if raw_search.get("organic") != call.get("results"):
                        raise ValueError(f"检索摘要与原始抓取不一致：{origin}")
                if call["tool"] == "web_open":
                    page = pages.get(call.get("url"))
                    if page is not None:
                        page = _page_view(page, call.get("ocr_page"))
                    if page is not None and page.get("provider") == "direct_html":
                        fields = ("raw_sha256", "resolved_url", "encoding", "title", "links",
                                  "provider", "content_kind", "extractor",
                                  "extraction_scope", "limitations")
                        if any(call.get(key) != page.get(key) for key in fields):
                            raise ValueError(f"HTML 工具回执与原始页面来源不一致：{origin}")
                    if (page is not None and page.get("provider") == "serper"
                            and call.get("body_format", "text") != page.get("body_format", "text")):
                        raise ValueError(f"Serper 工具回执与缓存正文格式不一致：{origin}")
                    if call.get("ocr_page") is not None and page is not None:
                        fields = ("content_kind", "extraction_scope", "image_sha256", "ocr_raw_sha256",
                                  "limitations", "versions", "model_sha256", "image_size")
                        if any(call.get(key) != page.get(key) for key in fields):
                            raise ValueError(f"OCR 工具回执与原识别来源不一致：{origin}")
                    offset = call.get("offset")
                    if (page is None or type(offset) is not int or offset < 0
                            or not isinstance(call.get("text"), str)
                            or call.get("total_chars") != len(page["text"])
                            or call.get("text") != page["text"][offset:offset + len(call["text"])]):
                        raise ValueError(f"检索实际分页与缓存正文不一致：{origin}")
            for url, page in pages.items():
                restored = dict(page)
                provenance = [
                    *(self.pages.get(url, {}).get("cache_provenance") or []),
                    *(page.get("cache_provenance") or []),
                    {"origin": origin, "raw_sha256": page["raw_sha256"],
                     "retrieved_at": page.get("retrieved_at")},
                ]
                restored["cache_provenance"] = []
                for item in provenance:
                    if item not in restored["cache_provenance"]:
                        restored["cache_provenance"].append(item)
                if url in self.pages:
                    restored = {**self.pages[url], "cache_provenance": restored["cache_provenance"],
                                **({"ocr_pages": {**self.pages[url].get("ocr_pages", {}),
                                                 **page.get("ocr_pages", {})}}
                                   if page.get("ocr_pages") else {})}
                self.pages[url] = restored
                filename = hashlib.sha256(url.encode()).hexdigest() + ".json"
                (self.root / filename).write_text(
                    json.dumps(restored, ensure_ascii=False, indent=2), encoding="utf-8",
                )
            for path in raw_files:
                if path.resolve() != (self.root / path.name).resolve():
                    shutil.copyfile(path, self.root / path.name)
            for index, call in enumerate(calls):
                history = list(call.get("restore_history") or [])
                if origin not in history:
                    history.append(origin)
                self.calls.append({
                    **call, **(bindings[index] if bindings else {}),
                    "restored": True, "restored_from": call.get("restored_from", origin),
                    "restore_history": history,
                })
            (self.root / "calls.jsonl").write_text(
                "".join(json.dumps(call, ensure_ascii=False) + "\n" for call in self.calls),
                encoding="utf-8",
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"检索状态恢复失败：{root}（{exc}）") from exc

    def ready(self) -> bool:
        """分别核对最近实际查询和抓取；缓存命中不能证明当前服务可用。"""
        latest = {
            kind: next((call for call in reversed(self.calls)
                        if call.get("tool") == kind and not call.get("cache_hit")
                        and not call.get("restored")), {})
            for kind in ("web_search", "web_open")
        }
        return bool(latest["web_search"].get("success")
                    and latest["web_open"].get("success") and latest["web_open"].get("text"))


def main() -> int:
    """同一检索实现既供补全 agent 调用，也可在 Harbor 任务容器内执行。"""
    import argparse

    parser = argparse.ArgumentParser(description="检索公开资料并保存原始返回")
    parser.add_argument("--output-root", type=Path, default=Path("/logs/artifacts/search"))
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("query")
    page = commands.add_parser("open")
    page.add_argument("url")
    page.add_argument("--offset", type=int, default=0)
    page.add_argument("--limit", type=int, default=8000)
    args = parser.parse_args()
    tools = SearchTools(args.output_root)
    if args.command == "search":
        result = tools.search(args.query)
    else:
        # 终端每次启动新进程；翻页仍须使用第一次实际抓取的正文。
        cache = args.output_root / (hashlib.sha256(args.url.encode()).hexdigest() + ".json")
        if cache.is_file():
            tools.pages[args.url] = json.loads(cache.read_text(encoding="utf-8"))
        result = tools.open(args.url, offset=args.offset, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
