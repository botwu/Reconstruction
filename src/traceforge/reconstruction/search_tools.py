"""真实公开检索与页面快照；失败保留为工具返回，不生成替代证据。"""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _public_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("来源必须为公开 HTTP(S) 地址")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("来源不能指向本机或私有网络")


class PublicSourceRedirect(urllib.request.HTTPRedirectHandler):
    """逐跳检查公开来源，避免重定向绕过原地址检查。"""

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int,
        msg: str, headers: Any, newurl: str,
    ) -> urllib.request.Request | None:
        _public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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
        self._serper_key = os.environ.get("SERPER_API_KEY") or config.get("serper_api_key")
        self._jina_key = os.environ.get("JINA_API_KEY") or config.get("jina_api_key")
        self._fetch_provider = os.environ.get("TRACEFORGE_FETCH_PROVIDER") or config.get(
            "fetch_provider", "jina",
        )

    def _record(self, kind: str, value: dict[str, Any]) -> dict[str, Any]:
        entry = {"tool": kind, "retrieved_at": datetime.now(UTC).isoformat(), **value}
        self.calls.append(entry)
        with (self.root / "calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def _response(self, request: urllib.request.Request) -> tuple[dict[str, Any], str]:
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw = response.read(8_000_001)
        except urllib.error.HTTPError as exc:
            raw = exc.read(8_000_001)
            digest = hashlib.sha256(raw).hexdigest()
            (self.root / f"{digest}.error.raw").write_bytes(raw)
            detail = raw.decode("utf-8", errors="replace")
            for key in (self._serper_key, self._jina_key):
                if key:
                    detail = detail.replace(key, "[credential]")
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
            value = {"success": False, "query": query, "error": f"{type(exc).__name__}: {exc}"}
        return self._record("web_search", value)

    def _fetch(self, url: str) -> dict[str, Any]:
        _public_url(url)
        pdf = self._fetch_pdf(url)
        if pdf is not None:
            return pdf
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
        _public_url(api_url)
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

    def _fetch_pdf(self, url: str) -> dict[str, Any] | None:
        # 文献链接可能无后缀或经重定向；依据实际响应识别，普通网页仍交给既定读取服务。
        pdf_expected = urllib.parse.urlsplit(url).path.lower().endswith(".pdf")
        request = urllib.request.Request(url, headers={"User-Agent": "TraceForge/0.3"})
        opener = urllib.request.build_opener(PublicSourceRedirect())
        try:
            with opener.open(request, timeout=90 if pdf_expected else 20) as response:
                prefix = response.read(5)
                pdf_expected = pdf_expected or "application/pdf" in response.headers.get("Content-Type", "").lower()
                if prefix != b"%PDF-":
                    if pdf_expected:
                        raise ValueError("来源没有返回 PDF 原文件，不能把登录页或错误页面当正文")
                    return None
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
        if not any(text.strip() for text in pages):
            raise ValueError("PDF 没有可读取的文本层，需要 OCR 或其他真实来源；未生成正文")
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
                "https://scrape.serper.dev/", data=json.dumps({"url": url}).encode(),
                headers={"X-API-KEY": self._serper_key, "Content-Type": "application/json"},
            )
            data, digest = self._response(request)
            if not isinstance(data.get("text"), str) or not data["text"].strip():
                raise ValueError("Serper 未返回可读正文")
            return {"success": True, "url": url, "text": data["text"],
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

    def open(self, url: str, *, offset: int = 0, limit: int = 8000) -> dict[str, Any]:
        cache_hit = url in self.pages
        try:
            if not cache_hit:
                self.pages[url] = self._fetch(url)
                name = hashlib.sha256(url.encode()).hexdigest() + ".json"
                (self.root / name).write_text(
                    json.dumps(self.pages[url], ensure_ascii=False, indent=2), encoding="utf-8",
                )
            page = self.pages[url]
            text = page["text"]
            offset, limit = max(0, int(offset)), max(1, min(8000, int(limit)))
            value = {**page, "text": text[offset:offset + limit], "total_chars": len(text),
                     "offset": offset, "next_offset": offset + limit
                     if offset + limit < len(text) else None}
        except Exception as exc:
            value = {"success": False, "url": url, "error": f"{type(exc).__name__}: {exc}"}
        return self._record("web_open", {**value, "cache_hit": cache_hit})

    def ready(self) -> bool:
        """分别核对最近实际查询和抓取；缓存命中不能证明当前服务可用。"""
        latest = {
            kind: next((call for call in reversed(self.calls)
                        if call.get("tool") == kind and not call.get("cache_hit")), {})
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
