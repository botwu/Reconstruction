"""真实公开检索与页面快照；失败保留为工具返回，不生成替代证据。"""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import shutil
import socket
import subprocess
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


def pdf_ocr_assets(page: dict[str, Any], root: Path) -> list[Path]:
    """核对 OCR 原始返回、PDF 和页图；同一契约用于检查点恢复与 Harbor 交付。"""
    assets: set[Path] = set()
    for number, item in (page.get("ocr_pages") or {}).items():
        if (not isinstance(item, dict) or item.get("schema_version") != "traceforge.pdf-ocr-page.v1"
                or str(item.get("page_number")) != number
                or item.get("source_pdf_sha256") != page.get("raw_sha256")
                or item.get("page_count") != page.get("page_count")):
            raise ValueError("OCR 页面与原 PDF 的绑定不匹配")
        paths = []
        for field, suffix in (("source_pdf_sha256", ".pdf"), ("image_sha256", ".png"),
                              ("ocr_raw_sha256", ".ocr.raw")):
            digest = item.get(field)
            if not isinstance(digest, str) or len(digest) != 64 or any(
                    char not in "0123456789abcdef" for char in digest):
                raise ValueError("OCR 来源哈希格式无效")
            path = root / (digest + suffix)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"OCR 来源缺失或哈希不匹配：{path.name}")
            paths.append(path)
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

    def _ocr(self, page: dict[str, Any], number: int) -> None:
        if type(number) is not int or not 1 <= number <= page.get("page_count", 0):
            raise ValueError("ocr_page 必须为 PDF 范围内从 1 开始的整数页号")
        if str(number) in (page.get("ocr_pages") or {}):
            pdf_ocr_assets(page, self.root)
            return
        if not self._ocr_python:
            raise ValueError("OCR 未配置：需要 TRACEFORGE_OCR_PYTHON 或 search.json 的 ocr_python")
        pdf = self.root / (page["raw_sha256"] + ".pdf")
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != page["raw_sha256"]:
            raise ValueError("OCR 原 PDF 哈希不匹配")
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
        pdf_ocr_assets(candidate, self.root)
        page["ocr_pages"] = candidate["ocr_pages"]

    def open(self, url: str, *, offset: int = 0, limit: int = 8000,
             ocr_page: int | None = None) -> dict[str, Any]:
        cache_hit = url in self.pages
        try:
            if not cache_hit:
                self.pages[url] = self._fetch(url)
            page = self.pages[url]
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
                     "error": f"{type(exc).__name__}: {exc}"}
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
                if page.get("content_kind") == "page_text":
                    data = json.loads(raw_path.read_bytes())
                    text = data.get("text") if page.get("provider") == "serper" else (
                        data.get("data") or {}).get("content")
                    if text != page["text"]:
                        raise ValueError(f"缓存正文与原始抓取不一致：{path}")
                    if page.get("provider") == "serper" and "jsonld" in data:
                        page["jsonld"] = data["jsonld"]
                pdf_ocr_assets(page, root)
                existing = self.pages.get(url)
                fields = ("raw_sha256", "text", "metadata", "jsonld", "content_kind", "source_ref")
                if existing and any(existing.get(key) != page.get(key) for key in fields):
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
