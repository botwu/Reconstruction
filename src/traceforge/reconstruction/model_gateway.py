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

# OpenAI 兼容端点上的推理模型（gpt-5 类）把隐藏推理 token 计入 max_tokens 预算：
# nominal 4096 会在产出任何正文前被推理耗尽，端点返回空 content 且 finish_reason=length，
# 历史网关只能抛不透明的 EMPTY_RESPONSE，既跑不通也无法辨识根因。这里为送往 OpenAI 兼容
# channel 的输出预算施加一个足够容纳推理的下限，使小的 nominal max_tokens 不会饿死推理
# 模型输出；非推理模型的 max_tokens 只是上限，自然 EOS 前停止、并不多耗，故不受影响。
# 显式给出的更大预算不被降低。Claude 原生（OpusClient）默认不把隐藏推理计入 max_tokens，
# 故不施加下限，只补 stop_reason=max_tokens 的截断辨识。
MIN_COMPLETION_TOKENS = 32768


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
    finish_reason: str | None = None

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


_SANDBOX_KEY_ALIASES = frozenset(
    {"e2bapikey", "e2b_api_key", "e2b-api-key", "ags_api_key", "agsapikey"}
)


def _parse_config_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip().strip("'\"")


def iter_config_items(path: str | os.PathLike[str]) -> list[tuple[str, Any]]:
    """解析顶层 key / 一行 JSON 或标量，结果只留在进程内存。"""

    try:
        with open(path, encoding="utf-8") as config_file:
            raw_lines = config_file.read().splitlines()
    except OSError as exc:
        raise ModelGatewayError("无法读取模型配置", code="CONFIG_READ_ERROR") from exc
    items: list[tuple[str, Any]] = []
    current: str | None = None
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indented = line.startswith((" ", "\t"))
        if not indented and ":" in stripped:
            name, _, rest = stripped.partition(":")
            current = name.strip()
            rest = rest.strip()
            if rest:
                items.append((current, _parse_config_value(rest)))
                current = None
            continue
        if current is None:
            continue
        items.append((current, _parse_config_value(stripped)))
        current = None
    return items


def _config_channels(path: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """读取 ``newapi_channel_conn`` 配置而不将密钥写入日志或 artifact。

    当前 TokenHub 配置是顶层 channel 名加一行 JSON 对象的 YAML 子集；这里
    不依赖 PyYAML，并且只把解析结果保存在进程内存中。对普通 YAML 映射也
    做了最小兼容，便于测试配置迁移。
    """

    channels: dict[str, dict[str, Any]] = {}
    for name, value in iter_config_items(path):
        if isinstance(value, dict):
            channels[name] = value
    return channels


def load_e2b_api_key(path: str | os.PathLike[str]) -> str | None:
    """读取 AGS/E2B 沙盒密钥；只返回内存中的字符串，不写日志。"""

    for name, value in iter_config_items(path):
        if name.lower() not in _SANDBOX_KEY_ALIASES:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            raw = value.get("key") or value.get("api_key")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


def load_channel_connection(path: str | os.PathLike[str], channel: str) -> tuple[str, str]:
    """读取 channel 的 url/key，只留在内存，不写日志。"""

    entry = _config_channels(path).get(channel)
    if not isinstance(entry, dict):
        raise ModelGatewayError(f"配置未找到 channel：{channel}", code="CHANNEL_MISSING")
    api_key = entry.get("key") or entry.get("api_key")
    raw_url = entry.get("url") or entry.get("base_url")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ModelGatewayError("模型配置缺少密钥", code="API_KEY_MISSING")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ModelGatewayError("模型配置缺少 endpoint", code="BASE_URL_MISSING")
    return raw_url.rstrip("/"), api_key


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
        url, key = load_channel_connection(path, channel)
        return cls(
            api_key=key,
            base_url=url,
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
                "max_tokens": (
                    request.max_tokens
                    if self.channel.lower() in {"claude", "anthropic"}
                    else max(request.max_tokens, MIN_COMPLETION_TOKENS)
                ),
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
                finish_reason = choices[0].get("finish_reason") if choices else None
                if finish_reason == "length":
                    raise ModelGatewayError(
                        "模型在产出正文前耗尽输出预算（finish_reason=length，"
                        "疑似推理占满 max_tokens）",
                        code="RESPONSE_TRUNCATED",
                    )
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
                finish_reason=choices[0].get("finish_reason") if choices else None,
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
                if payload.get("stop_reason") == "max_tokens":
                    raise ModelGatewayError(
                        "模型在产出正文前耗尽输出预算（stop_reason=max_tokens）",
                        code="RESPONSE_TRUNCATED",
                    )
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
                finish_reason=payload.get("stop_reason"),
            )
        raise AssertionError("unreachable")


_ROLE_JSON_KEYS = frozenset(
    {
        "label",
        "decision",
        "task_instruction",
        "acceptance_obligations",
        "candidates",
        "status",
        "missing_context",
    }
)


def parse_json_object(text: str) -> dict[str, Any]:
    """解析完整 JSON 对象，允许外层围栏和说明，不从损坏的根对象内取片段。

    说明文字里的合法 stub 可以跳过；遇到对象语法错误必须交给模型修正，
    不能把嵌套义务或响应检查冒充完整角色结果。
    """

    candidate = text.strip()
    objects: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    index = 0
    while True:
        start = candidate.find("{", index)
        if start < 0:
            break
        try:
            value, consumed = decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError as exc:
            raise ModelGatewayError(
                f"模型输出的完整 JSON 对象语法错误（行 {exc.lineno}，列 {exc.colno}）",
                code="INVALID_JSON",
            ) from exc
        if isinstance(value, dict):
            objects.append(value)
        index = start + max(consumed, 1)
    if not objects:
        raise ModelGatewayError("模型输出不是合法 JSON", code="INVALID_JSON")
    for value in reversed(objects):
        if _ROLE_JSON_KEYS & set(value):
            return value
    return objects[-1]


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


CHANNEL_MODEL_DEFAULTS = {
    "gemini": "gemini-2.5-pro",
    "gpt": "gpt-5",
    "claude": "claude-opus-4-8",
    # TokenHub deepseek channel 实测可用名；vol/ 前缀会 model_not_found。
    "deepseek": "bailian/deepseek-v4-flash-0731",
}


def load_channel_model(
    path: str | os.PathLike[str], channel: str
) -> str | None:
    """读取 channel 上持久化的 model / model_name，不读密钥。"""

    entry = _config_channels(path).get(channel)
    if not isinstance(entry, dict):
        return None
    for key in ("model", "model_name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_model_name(
    requested: str | None,
    *,
    config_path: str | os.PathLike[str] | None = None,
    channel: str = "gemini",
) -> str:
    """为配置驱动调用提供安全默认模型，避免把 Claude 名称发给 Gemini。"""

    if requested and requested.strip():
        rewrite_claude_for_other_channel = (
            config_path is not None
            and requested.startswith("claude-")
            and channel not in {"claude", "anthropic"}
        )
        if not rewrite_claude_for_other_channel:
            return requested
    if config_path is not None:
        configured = load_channel_model(config_path, channel)
        if configured:
            return configured
        return CHANNEL_MODEL_DEFAULTS.get(channel, channel)
    return requested or "claude-opus-4-8"


__all__ = [
    "CHANNEL_MODEL_DEFAULTS",
    "ChatModel",
    "ModelCallReceipt",
    "ModelGatewayError",
    "ModelRequest",
    "ModelResponse",
    "NewAPIClient",
    "OpusClient",
    "Transport",
    "build_chat_model",
    "iter_config_items",
    "load_channel_connection",
    "load_channel_model",
    "load_e2b_api_key",
    "parse_json_object",
    "receipt_for_response",
    "resolve_model_name",
]
