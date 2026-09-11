"""语义重建模型的窄调用边界。

模块不持久化 API key、prompt 或响应正文。调用方可以注入 transport 做离线测试；
生产环境只从环境变量读取密钥。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class ModelGatewayError(RuntimeError):
    """模型请求失败，或返回内容不满足调用契约。"""

    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """一次可审计的模型请求；不包含密钥。"""

    request_id: str
    model: str
    system: str
    prompt: str
    response_schema: str
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_seconds: int = 120


@dataclass(frozen=True, slots=True)
class ModelResponse:
    request_id: str
    model: str
    provider: str
    text: str
    attempts: int
    latency_seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ModelCallReceipt:
    """可持久化的调用摘要，禁止保存 prompt、响应正文和密钥。"""

    request_id: str
    provider: str
    model: str
    status: str
    attempts: int
    latency_seconds: float
    response_sha256: str | None
    error_code: str | None


class ChatModel(Protocol):
    """语义重建只依赖这一个接口。"""

    def complete(self, request: ModelRequest) -> ModelResponse: ...


Transport = Callable[[str, Mapping[str, str], bytes, int], tuple[int, bytes]]


def _config_channels(path: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """读取 ``newapi_channel_conn`` 配置而不将密钥写入日志或 artifact。

    当前 TokenHub 配置是顶层 channel 名加一行 JSON 对象的 YAML 子集；这里
    不依赖 PyYAML，并且只把解析结果保存在进程内存中。对普通 YAML 映射也
    做了最小兼容，便于测试配置迁移。
    """

    try:
        with open(path, encoding="utf-8") as config_file:
            raw_lines = config_file.read().splitlines()
    except OSError as exc:
        raise ModelGatewayError("无法读取模型配置", code="CONFIG_READ_ERROR") from exc
    channels: dict[str, dict[str, Any]] = {}
    current: str | None = None
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t")) and stripped.endswith(":"):
            current = stripped[:-1].strip()
            continue
        if current is None:
            continue
        # 配置格式中的值是一行 JSON；拒绝任意代码或复杂 YAML。
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            channels[current] = value
    return channels


class NewAPIClient:
    """TokenHub/NewAPI OpenAI-compatible chat-completions 客户端。

    ``api_key`` 仅保存在客户端对象内存中，既不会进入 ``ModelResponse``，也
    不会进入 receipt、异常文本或持久化 artifact。该客户端用于 Gemini 等
    NewAPI channel；Claude 原生接口继续使用 :class:`OpusClient`。
    """

    provider = "newapi"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        channel: str = "gemini",
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        transport: Transport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ModelGatewayError("模型配置缺少密钥", code="API_KEY_MISSING")
        if max_retries < 0 or retry_backoff_seconds < 0:
            raise ValueError("重试参数必须非负")
        self._api_key = api_key
        self.channel = channel
        configured_url = base_url.rstrip("/")
        if configured_url.endswith("/chat/completions"):
            endpoint = configured_url
        elif configured_url.endswith("/v1"):
            endpoint = configured_url + "/chat/completions"
        else:
            endpoint = configured_url + "/v1/chat/completions"
        self.base_url = endpoint
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._transport = transport or _default_transport

    @classmethod
    def from_config(
        cls,
        path: str | os.PathLike[str],
        *,
        channel: str = "gemini",
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        transport: Transport | None = None,
    ) -> NewAPIClient:
        channels = _config_channels(path)
        entry = channels.get(channel)
        if not isinstance(entry, dict):
            raise ModelGatewayError("模型配置未找到指定 channel", code="CONFIG_CHANNEL_MISSING")
        api_key = entry.get("key") or entry.get("api_key")
        base_url = entry.get("url") or entry.get("base_url")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ModelGatewayError("模型配置缺少密钥", code="API_KEY_MISSING")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ModelGatewayError("模型配置缺少 endpoint", code="CONFIG_ENDPOINT_MISSING")
        return cls(
            api_key=api_key,
            base_url=base_url,
            channel=channel,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            transport=transport,
        )

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.model.strip() or not request.prompt.strip():
            raise ModelGatewayError("模型请求缺少 model 或 prompt", code="INVALID_REQUEST")
        if not request.response_schema.strip():
            raise ModelGatewayError("必须声明响应 schema", code="SCHEMA_REQUIRED")
        body = json.dumps(
            {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.prompt},
                ],
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self._api_key}",
        }
        started = time.monotonic()
        for attempt in range(self.max_retries + 1):
            try:
                status, raw = self._transport(self.base_url, headers, body, request.timeout_seconds)
            except TimeoutError as exc:
                error = ModelGatewayError(
                    "模型网络请求超时", code="NETWORK_TIMEOUT", retryable=True
                )
                if attempt >= self.max_retries:
                    raise error from exc
                time.sleep(self.retry_backoff_seconds * (2**attempt))
                continue
            except ModelGatewayError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                time.sleep(self.retry_backoff_seconds * (2**attempt))
                continue
            if status in {408, 429} or status >= 500:
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_seconds * (2**attempt))
                    continue
                raise ModelGatewayError("模型服务暂时不可用", code=f"HTTP_{status}", retryable=True)
            if status < 200 or status >= 300:
                raise ModelGatewayError("模型请求被拒绝", code=f"HTTP_{status}")
            try:
                payload = json.loads(raw)
                choices = payload.get("choices", [])
                message = choices[0].get("message", {}) if choices else {}
                content = message.get("content", "") if isinstance(message, dict) else ""
                if isinstance(content, list):
                    text = "".join(
                        str(part.get("text", "")) for part in content if isinstance(part, dict)
                    )
                else:
                    text = content if isinstance(content, str) else ""
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                AttributeError,
                TypeError,
                IndexError,
            ) as exc:
                raise ModelGatewayError("模型响应不是合法 JSON", code="INVALID_RESPONSE") from exc
            if not text.strip():
                raise ModelGatewayError("模型响应没有文本内容", code="EMPTY_RESPONSE")
            usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
            return ModelResponse(
                request_id=request.request_id,
                model=request.model,
                provider=f"{self.provider}:{self.channel}",
                text=text,
                attempts=attempt + 1,
                latency_seconds=time.monotonic() - started,
                input_tokens=usage.get("prompt_tokens") if isinstance(usage, dict) else None,
                output_tokens=usage.get("completion_tokens") if isinstance(usage, dict) else None,
            )
        raise AssertionError("unreachable")


def _default_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: int
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise ModelGatewayError("模型网络请求失败", code="NETWORK_ERROR", retryable=True) from exc
    except TimeoutError as exc:
        raise ModelGatewayError("模型网络请求超时", code="NETWORK_TIMEOUT", retryable=True) from exc


class OpusClient:
    """Claude Opus 原生 Messages API 客户端。"""

    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key_env: str = "TOKENHUB_KEY",
        base_url: str | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        transport: Transport | None = None,
    ) -> None:
        if max_retries < 0 or retry_backoff_seconds < 0:
            raise ValueError("重试参数必须非负")
        self.api_key_env = api_key_env
        configured_url = base_url or os.environ.get(
            "TOKENHUB_BASE_URL", "https://tokenhub.sensetime.com"
        )
        self.base_url = configured_url.rstrip("/")
        if not self.base_url.endswith("/messages"):
            self.base_url += "/messages" if self.base_url.endswith("/v1") else "/v1/messages"
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._transport = transport or _default_transport

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.model.strip() or not request.prompt.strip():
            raise ModelGatewayError("模型请求缺少 model 或 prompt", code="INVALID_REQUEST")
        if not request.response_schema.strip():
            raise ModelGatewayError("必须声明响应 schema", code="SCHEMA_REQUIRED")
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ModelGatewayError(
                f"未设置模型密钥环境变量：{self.api_key_env}", code="API_KEY_MISSING"
            )
        body = json.dumps(
            {
                "model": request.model,
                "system": request.system,
                "messages": [{"role": "user", "content": request.prompt}],
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        started = time.monotonic()
        for attempt in range(self.max_retries + 1):
            try:
                status, raw = self._transport(self.base_url, headers, body, request.timeout_seconds)
            except TimeoutError as exc:
                error = ModelGatewayError(
                    "模型网络请求超时", code="NETWORK_TIMEOUT", retryable=True
                )
                if attempt >= self.max_retries:
                    raise error from exc
                time.sleep(self.retry_backoff_seconds * (2**attempt))
                continue
            except ModelGatewayError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                time.sleep(self.retry_backoff_seconds * (2**attempt))
                continue
            if status in {408, 429} or status >= 500:
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_seconds * (2**attempt))
                    continue
                raise ModelGatewayError("模型服务暂时不可用", code=f"HTTP_{status}", retryable=True)
            if status < 200 or status >= 300:
                raise ModelGatewayError("模型请求被拒绝", code=f"HTTP_{status}")
            try:
                payload = json.loads(raw)
                blocks = payload.get("content", [])
                text = "".join(
                    str(block.get("text", "")) for block in blocks if block.get("type") == "text"
                )
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError) as exc:
                raise ModelGatewayError("模型响应不是合法 JSON", code="INVALID_RESPONSE") from exc
            if not text.strip():
                raise ModelGatewayError("模型响应没有文本内容", code="EMPTY_RESPONSE")
            usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
            return ModelResponse(
                request_id=request.request_id,
                model=request.model,
                provider=self.provider,
                text=text,
                attempts=attempt + 1,
                latency_seconds=time.monotonic() - started,
                input_tokens=usage.get("input_tokens") if isinstance(usage, dict) else None,
                output_tokens=usage.get("output_tokens") if isinstance(usage, dict) else None,
            )
        raise AssertionError("unreachable")


def parse_json_object(text: str) -> dict[str, Any]:
    """解析模型 JSON；允许 fenced block，但不允许前后额外文本。"""

    candidate = text.strip()
    fence = chr(96) * 3
    if candidate.startswith(fence) and candidate.endswith(fence):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ModelGatewayError("模型输出不是合法 JSON", code="INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise ModelGatewayError("模型输出必须是 JSON object", code="JSON_OBJECT_REQUIRED")
    return value


def receipt_for_response(response: ModelResponse) -> ModelCallReceipt:
    return ModelCallReceipt(
        request_id=response.request_id,
        provider=response.provider,
        model=response.model,
        status="COMPLETED",
        attempts=response.attempts,
        latency_seconds=response.latency_seconds,
        response_sha256=response.content_sha256,
        error_code=None,
    )


def build_chat_model(
    *,
    config_path: str | os.PathLike[str] | None = None,
    channel: str = "gemini",
    transport: Transport | None = None,
) -> ChatModel:
    """根据 CLI 配置选择 NewAPI channel 或保持原有环境变量客户端。"""

    if config_path is not None:
        return NewAPIClient.from_config(config_path, channel=channel, transport=transport)
    return OpusClient(transport=transport)


def resolve_model_name(
    requested: str | None,
    *,
    config_path: str | os.PathLike[str] | None = None,
    channel: str = "gemini",
) -> str:
    """为配置驱动调用提供安全默认模型，避免把 Claude 名称发给 Gemini。"""

    if (
        requested
        and requested.strip()
        and not (config_path is not None and requested.startswith("claude-"))
    ):
        return requested
    if config_path is not None:
        defaults = {
            "gemini": "gemini-2.5-pro",
            "gpt": "gpt-5",
            "claude": "claude-opus-4-8",
            # TokenHub 当前 deepseek channel 暴露的稳定低成本模型。
            "deepseek": "vol/deepseek-v4-flash-0731",
        }
        return defaults.get(channel, channel)
    return requested or "claude-opus-4-8"


__all__ = [
    "ChatModel",
    "ModelCallReceipt",
    "ModelGatewayError",
    "ModelRequest",
    "ModelResponse",
    "NewAPIClient",
    "OpusClient",
    "Transport",
    "build_chat_model",
    "parse_json_object",
    "receipt_for_response",
    "resolve_model_name",
]
