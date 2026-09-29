"""真实公开检索与页面快照；失败保留为工具返回，不生成替代证据。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
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
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read(8_000_001)
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
        try:
            if url not in self.pages:
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
        return self._record("web_open", value)

    def ready(self) -> bool:
        return any(c.get("success") and c.get("results") for c in self.calls) and any(
            c.get("success") and c.get("text") for c in self.calls
        )
