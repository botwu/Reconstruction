"""Anthropic Messages 透明采集代理。

该模块只负责一次转发与证据采集，不重试、不改写请求体，也不将任何
认证头写入磁盘。它既可以被 :class:`AnthropicCaptureProxy` 嵌入调用，也可以
在 AGS 沙盒中直接执行 ``python -m harbor_ags.capture``。
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as _dt
import hashlib
import http.server
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .exceptions import CaptureInfrastructureError

_AUTHORIZATION_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
    }
)
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
_PASSTHROUGH_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "anthropic-beta",
        "anthropic-version",
        "content-type",
        "user-agent",
        "x-request-id",
    }
)
_PASSTHROUGH_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-encoding",
        "content-type",
        "request-id",
        "x-request-id",
        "anthropic-request-id",
    }
)
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_or_none(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _json_safe_body(data: bytes) -> dict[str, Any]:
    """保留语义与字节摘要，非 JSON 响应使用 base64 保真。"""

    parsed = _json_or_none(data)
    result: dict[str, Any] = {
        "sha256": _sha256(data),
        "size_bytes": len(data),
        # 即使是 JSON 也保留原始字节，避免重序列化导致证据漂移。
        "raw_base64": base64.b64encode(data).decode("ascii"),
    }
    if parsed is not None:
        result["json"] = parsed
    return result


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """返回可落盘的头部副本。

    不保留认证头的名称或值，避免消费者误以为 ``<redacted>`` 可重放。
    """

    return {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).lower() not in _AUTHORIZATION_HEADERS
    }


@dataclass(frozen=True)
class CaptureConfig:
    upstream_base_url: str
    evidence_dir: Path
    upstream_api_key: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 0
    timeout_seconds: float = 300.0
    max_request_bytes: int = 32 * 1024 * 1024
    read_chunk_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.upstream_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("upstream_base_url 必须是 http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("upstream_base_url 不得包含 URL 凭据")
        if not self.upstream_api_key:
            raise ValueError("upstream_api_key 不能为空")
        if not (0 <= self.listen_port <= 65535):
            raise ValueError("listen_port 必须在 0..65535 之间")
        if self.timeout_seconds <= 0 or self.max_request_bytes <= 0:
            raise ValueError("超时和请求大小限制必须大于 0")

    @property
    def messages_url(self) -> str:
        base = self.upstream_base_url.rstrip("/")
        if base.endswith("/v1/messages"):
            return base
        if base.endswith("/v1"):
            return base + "/messages"
        return base + "/v1/messages"


@dataclass
class CapturedExchange:
    exchange_id: str
    started_at: str
    finished_at: str
    duration_ms: int
    method: str
    path: str
    upstream_url: str
    request_headers: dict[str, str]
    request: dict[str, Any]
    response_status: int
    response_headers: dict[str, str]
    response: dict[str, Any]
    streaming: bool
    complete: bool
    request_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    sse_event_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CaptureError(CaptureInfrastructureError):
    """采集代理自身故障。"""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """不跟随重定向，防止上游 key 被带到未锁定的 host。"""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class _SSEParser:
    def __init__(self) -> None:
        self._buffer = b""
        self.events: list[dict[str, Any]] = []
        self.message_stop_seen = False

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self._buffer += chunk
        emitted: list[dict[str, Any]] = []
        while True:
            match = re.search(rb"\r?\n\r?\n", self._buffer)
            if match is None:
                break
            raw = self._buffer[: match.start()]
            self._buffer = self._buffer[match.end() :]
            event = self._parse_event(raw)
            if event is not None:
                self.events.append(event)
                emitted.append(event)
                if event.get("event") == "message_stop" or (
                    isinstance(event.get("data"), dict)
                    and event["data"].get("type") == "message_stop"
                ):
                    self.message_stop_seen = True
        return emitted

    def finish(self) -> dict[str, Any] | None:
        if not self._buffer:
            return None
        raw = self._buffer
        self._buffer = b""
        event = self._parse_event(raw)
        if event is not None:
            event["unterminated"] = True
            self.events.append(event)
        return event

    @staticmethod
    def _parse_event(raw: bytes) -> dict[str, Any] | None:
        if not raw:
            return None
        event_name: str | None = None
        data_lines: list[bytes] = []
        event_id: str | None = None
        for line in raw.splitlines():
            if not line or line.startswith(b":"):
                continue
            field_name, separator, value = line.partition(b":")
            if separator and value.startswith(b" "):
                value = value[1:]
            if field_name == b"event":
                event_name = value.decode("utf-8", "replace")
            elif field_name == b"data":
                data_lines.append(value)
            elif field_name == b"id":
                event_id = value.decode("utf-8", "replace")
        data_bytes = b"\n".join(data_lines)
        data: Any = None
        if data_bytes:
            data = _json_or_none(data_bytes)
            if data is None:
                data = data_bytes.decode("utf-8", "replace")
        return {
            "event": event_name,
            "id": event_id,
            "data": data,
            "raw_base64": base64.b64encode(raw).decode("ascii"),
            "raw_sha256": _sha256(raw),
            "raw_size_bytes": len(raw),
        }


def assemble_anthropic_sse(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """将 Anthropic SSE 事件重组为不丢失模型字段的消息视图。"""

    message: dict[str, Any] = {"content": [], "usage": {key: None for key in _USAGE_FIELDS}}
    blocks: dict[int, dict[str, Any]] = {}
    complete = False
    for envelope in events:
        payload = envelope.get("data")
        if not isinstance(payload, Mapping):
            continue
        event_type = payload.get("type") or envelope.get("event")
        if event_type == "message_start":
            start_message = payload.get("message")
            if isinstance(start_message, Mapping):
                for key, value in start_message.items():
                    if key == "content":
                        for index, block in enumerate(value or []):
                            if isinstance(block, Mapping):
                                blocks[index] = copy.deepcopy(dict(block))
                    elif key == "usage" and isinstance(value, Mapping):
                        for usage_key in _USAGE_FIELDS:
                            if usage_key in value:
                                message["usage"][usage_key] = value.get(usage_key)
                    else:
                        message[key] = copy.deepcopy(value)
        elif event_type == "content_block_start":
            index = int(payload.get("index", len(blocks)))
            block = payload.get("content_block")
            if isinstance(block, Mapping):
                blocks[index] = copy.deepcopy(dict(block))
        elif event_type == "content_block_delta":
            index = int(payload.get("index", 0))
            block = blocks.setdefault(index, {})
            delta = payload.get("delta")
            if not isinstance(delta, Mapping):
                continue
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                block["type"] = block.get("type", "text")
                block["text"] = str(block.get("text", "")) + str(delta.get("text", ""))
            elif delta_type == "thinking_delta":
                block["type"] = block.get("type", "thinking")
                block["thinking"] = str(block.get("thinking", "")) + str(delta.get("thinking", ""))
            elif delta_type == "signature_delta":
                block["signature"] = str(block.get("signature", "")) + str(
                    delta.get("signature", "")
                )
            elif delta_type == "input_json_delta":
                partial = str(delta.get("partial_json", ""))
                block["input_json"] = str(block.get("input_json", "")) + partial
            else:
                block.setdefault("deltas", []).append(copy.deepcopy(dict(delta)))
        elif event_type == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, Mapping):
                message.update(copy.deepcopy(dict(delta)))
            usage = payload.get("usage")
            if isinstance(usage, Mapping):
                for usage_key in _USAGE_FIELDS:
                    if usage_key in usage:
                        message["usage"][usage_key] = usage.get(usage_key)
        elif event_type == "message_stop":
            complete = True

    normalized_blocks: list[dict[str, Any]] = []
    for index in sorted(blocks):
        block = blocks[index]
        if block.get("type") == "tool_use" and "input_json" in block:
            raw_input = block.pop("input_json")
            try:
                block["input"] = json.loads(raw_input)
            except json.JSONDecodeError:
                block["input"] = None
                block["input_json_incomplete"] = raw_input
        normalized_blocks.append(block)
    message["content"] = normalized_blocks
    message["complete"] = complete
    return message


class _EvidenceWriter:
    def __init__(self, evidence_dir: Path) -> None:
        self.evidence_dir = evidence_dir
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.exchange_path = evidence_dir / "anthropic-exchanges.jsonl"
        self.sse_path = evidence_dir / "anthropic-sse.jsonl"
        self._lock = threading.Lock()
        # 即使只有非流式调用，完整证据契约中的两个 JSONL 也必须存在。
        self.exchange_path.touch(exist_ok=True)
        self.sse_path.touch(exist_ok=True)

    def append_exchange(self, exchange: CapturedExchange) -> None:
        self._append_json(self.exchange_path, exchange.to_dict())

    def append_sse(self, exchange_id: str, sequence: int, event: Mapping[str, Any]) -> None:
        record = {"exchange_id": exchange_id, "sequence": sequence, **copy.deepcopy(dict(event))}
        self._append_json(self.sse_path, record)

    def _append_json(self, path: Path, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())


class _CaptureServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: CaptureConfig) -> None:
        self.capture_config = config
        self.evidence_writer = _EvidenceWriter(config.evidence_dir)
        super().__init__(address, _CaptureHandler)


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AnthropicCaptureProxy/1"

    @property
    def capture_server(self) -> _CaptureServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        # 请求路径可能包含业务信息，默认不写 stderr access log。
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if urllib.parse.urlsplit(self.path).path != "/healthz":
            self._send_json(404, {"status": "not_found"})
            return
        self._send_json(200, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        config = self.capture_server.capture_config
        if urllib.parse.urlsplit(self.path).path != "/v1/messages":
            self._send_json(404, {"type": "error", "error": {"type": "not_found_error"}})
            return
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        if transfer_encoding and transfer_encoding != "identity":
            self._send_json(
                400,
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "chunked request bodies are unsupported",
                    },
                },
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error"}})
            return
        if content_length < 0 or content_length > config.max_request_bytes:
            self._send_json(413, {"type": "error", "error": {"type": "request_too_large"}})
            return
        request_bytes = self.rfile.read(content_length)
        if len(request_bytes) != content_length:
            self._send_json(400, {"type": "error", "error": {"type": "incomplete_request"}})
            return
        self._proxy_messages(request_bytes)

    def _proxy_messages(self, request_bytes: bytes) -> None:
        config = self.capture_server.capture_config
        writer = self.capture_server.evidence_writer
        exchange_id = str(uuid.uuid4())
        started_at = _utc_now()
        started_monotonic = time.monotonic()
        request_payload = _json_or_none(request_bytes)
        requested_streaming = bool(
            isinstance(request_payload, Mapping) and request_payload.get("stream") is True
        )
        outgoing_headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() in _PASSTHROUGH_REQUEST_HEADERS
            and name.lower() not in _HOP_BY_HOP_HEADERS
        }
        outgoing_headers["x-api-key"] = config.upstream_api_key
        outgoing_headers.setdefault("content-type", "application/json")
        request = urllib.request.Request(
            config.messages_url,
            data=request_bytes,
            headers=outgoing_headers,
            method="POST",
        )

        status = 502
        response_headers: dict[str, str] = {}
        response_bytes = bytearray()
        request_id: str | None = None
        sse_parser = _SSEParser()
        error_code: str | None = None
        error_detail: str | None = None
        streaming = requested_streaming
        downstream_started = False
        try:
            try:
                opener = urllib.request.build_opener(_NoRedirectHandler())
                upstream = opener.open(request, timeout=config.timeout_seconds)
            except urllib.error.HTTPError as exc:
                # HTTPError 仍是一个可读 response；原样转发上游状态和 body。
                upstream = exc
            with upstream:
                status = int(upstream.status)
                upstream_headers = {str(k): str(v) for k, v in upstream.headers.items()}
                response_headers = {
                    name: value
                    for name, value in redact_headers(upstream_headers).items()
                    if name.lower() in _PASSTHROUGH_RESPONSE_HEADERS
                }
                content_type = upstream.headers.get("Content-Type", "")
                streaming = requested_streaming or content_type.lower().startswith(
                    "text/event-stream"
                )
                request_id = (
                    upstream.headers.get("request-id")
                    or upstream.headers.get("x-request-id")
                    or upstream.headers.get("anthropic-request-id")
                )
                self.send_response(status)
                for name, value in upstream.headers.items():
                    lower = name.lower()
                    if lower in _PASSTHROUGH_RESPONSE_HEADERS and lower not in _HOP_BY_HOP_HEADERS:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                downstream_started = True
                sequence = 0
                while True:
                    # read(n) may wait for a full buffer and hide live SSE frames.
                    chunk = upstream.read1(config.read_chunk_bytes)
                    if not chunk:
                        break
                    response_bytes.extend(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if streaming:
                        for event in sse_parser.feed(chunk):
                            writer.append_sse(exchange_id, sequence, event)
                            sequence += 1
                if streaming:
                    tail = sse_parser.finish()
                    if tail is not None:
                        writer.append_sse(exchange_id, sequence, tail)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error_code = "UPSTREAM_TRANSPORT_ERROR"
            error_detail = f"{type(exc).__name__}: {exc}"
            if not downstream_started:
                self._send_json(
                    502,
                    {"type": "error", "error": {"type": "api_connection_error"}},
                )
        finally:
            self.close_connection = True

        parsed_response = _json_or_none(bytes(response_bytes)) if not streaming else None
        if streaming:
            normalized_response = assemble_anthropic_sse(sse_parser.events)
            complete = status != 200 or sse_parser.message_stop_seen
            if status == 200 and not complete and error_code is None:
                error_code = "INCOMPLETE_STREAM"
                error_detail = "HTTP 200 SSE stream ended without message_stop"
            response_evidence = {
                "body": _json_safe_body(bytes(response_bytes)),
                "message": normalized_response,
            }
        else:
            complete = status != 200 or isinstance(parsed_response, Mapping)
            if status == 200 and not complete and error_code is None:
                error_code = "INCOMPLETE_RESPONSE"
                error_detail = "HTTP 200 response was not a complete JSON object"
            response_evidence = _json_safe_body(bytes(response_bytes))

        exchange = CapturedExchange(
            exchange_id=exchange_id,
            started_at=started_at,
            finished_at=_utc_now(),
            duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
            method="POST",
            path="/v1/messages",
            upstream_url=config.messages_url,
            request_headers={
                name: value
                for name, value in redact_headers(dict(self.headers.items())).items()
                if name.lower() in _PASSTHROUGH_REQUEST_HEADERS
            },
            request=_json_safe_body(request_bytes),
            response_status=status,
            response_headers=response_headers,
            response=response_evidence,
            streaming=streaming,
            complete=complete,
            request_id=request_id,
            error_code=error_code,
            error_detail=error_detail,
            sse_event_count=len(sse_parser.events),
        )
        try:
            writer.append_exchange(exchange)
        except OSError as exc:
            # 代理已无法提供可认证证据；连接会被关闭，Agent 侧应将其归类为 INFRA_CAPTURE。
            raise CaptureError(f"写入采集证据失败: {exc}") from exc

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


class AnthropicCaptureProxy:
    """可嵌入的 sandbox CaptureProxy。"""

    def __init__(self, config: CaptureConfig) -> None:
        self.config = config
        self._server: _CaptureServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("CaptureProxy 尚未启动")
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        host = self.config.listen_host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{self.port}"

    def start(self) -> AnthropicCaptureProxy:
        if self._server is not None:
            return self
        self._server = _CaptureServer(
            (self.config.listen_host, self.config.listen_port), self.config
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="anthropic-capture-proxy",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def serve_forever(self) -> None:
        if self._server is not None:
            raise RuntimeError("CaptureProxy 已在运行")
        self._server = _CaptureServer(
            (self.config.listen_host, self.config.listen_port), self.config
        )
        try:
            print(json.dumps({"base_url": self.base_url, "status": "ready"}), flush=True)
            self._server.serve_forever()
        finally:
            self._server.server_close()
            self._server = None

    def __enter__(self) -> AnthropicCaptureProxy:
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Anthropic Messages 无损采集代理")
    parser.add_argument(
        "--upstream-base-url",
        default=os.environ.get("CAPTURE_UPSTREAM_BASE_URL")
        or os.environ.get("ANTHROPIC_UPSTREAM_BASE_URL")
        or "https://tokenhub.sensetime.com",
    )
    parser.add_argument(
        "--upstream-api-key",
        default=os.environ.get("CAPTURE_UPSTREAM_API_KEY")
        or os.environ.get("TOKENHUB_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY"),
    )
    parser.add_argument("--listen-host", default=os.environ.get("CAPTURE_LISTEN_HOST", "127.0.0.1"))
    parser.add_argument(
        "--listen-port",
        type=int,
        default=int(os.environ.get("CAPTURE_LISTEN_PORT", "8081")),
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path(os.environ.get("CAPTURE_EVIDENCE_DIR", "/tmp/harbor-ags-capture")),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.environ.get("CAPTURE_TIMEOUT_SECONDS", "300")),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.upstream_api_key:
        raise SystemExit("必须通过 --upstream-api-key 或环境变量提供上游 key")
    proxy = AnthropicCaptureProxy(
        CaptureConfig(
            upstream_base_url=args.upstream_base_url,
            upstream_api_key=args.upstream_api_key,
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            evidence_dir=args.evidence_dir,
            timeout_seconds=args.timeout_seconds,
        )
    )
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
