"""网关输出预算下限与截断可辨识性。

推理模型（gpt-5 类）经 OpenAI 兼容端点调用时，隐藏推理 token 计入 ``max_tokens``：
nominal 4096 会在产出任何正文前被推理耗尽，端点返回空 ``content`` 且
``finish_reason == "length"``。历史网关只抛不透明的 ``EMPTY_RESPONSE``，既无法自动
跑通，也无法辨识根因。这里钉住两条行为：

1. ``NewAPIClient`` 为送出的 ``max_tokens`` 施加一个足够的下限，使小的 nominal 预算
   不会饿死推理模型输出；显式给出的更大预算不被降低。
2. 空正文且 ``finish_reason``/``stop_reason`` 指示 length 截断时，抛可辨识的
   ``RESPONSE_TRUNCATED``（不可重试——同预算重试只会再次截断），而非 ``EMPTY_RESPONSE``。
"""

from __future__ import annotations

import json

import pytest

from traceforge.reconstruction.model_gateway import (
    MIN_COMPLETION_TOKENS,
    ModelGatewayError,
    ModelRequest,
    NewAPIClient,
    OpusClient,
)


def _request(max_tokens: int | None = None) -> ModelRequest:
    kwargs = {} if max_tokens is None else {"max_tokens": max_tokens}
    return ModelRequest(
        request_id="req-budget",
        model="gpt-5",
        system="return JSON",
        prompt='{"ok": true}',
        response_schema="object",
        **kwargs,
    )


def test_min_completion_tokens_leaves_room_for_reasoning() -> None:
    """下限必须足够容纳推理模型的隐藏推理预算，不锁死具体值。"""

    assert MIN_COMPLETION_TOKENS >= 16384


def test_newapi_floors_small_max_tokens_for_reasoning_models() -> None:
    captured: dict[str, object] = {}

    def transport(url, headers, body, timeout):
        captured["body"] = json.loads(body)
        return 200, b'{"choices":[{"message":{"content":"{\\"ok\\":true}"},"finish_reason":"stop"}]}'

    client = NewAPIClient(api_key="k", base_url="https://gateway.example/v1", transport=transport)
    client.complete(_request())  # 使用默认 max_tokens=4096

    assert captured["body"]["max_tokens"] == MIN_COMPLETION_TOKENS


def test_newapi_preserves_explicit_budget_above_floor() -> None:
    captured: dict[str, object] = {}

    def transport(url, headers, body, timeout):
        captured["body"] = json.loads(body)
        return 200, b'{"choices":[{"message":{"content":"{\\"ok\\":true}"},"finish_reason":"stop"}]}'

    client = NewAPIClient(api_key="k", base_url="https://gateway.example/v1", transport=transport)
    client.complete(_request(max_tokens=99999))

    assert captured["body"]["max_tokens"] == 99999


def test_newapi_reports_length_truncation_as_distinct_code() -> None:
    def transport(url, headers, body, timeout):
        return 200, b'{"choices":[{"message":{"content":""},"finish_reason":"length"}]}'

    client = NewAPIClient(api_key="k", base_url="https://gateway.example/v1", transport=transport)
    with pytest.raises(ModelGatewayError) as exc_info:
        client.complete(_request())

    assert exc_info.value.code == "RESPONSE_TRUNCATED"
    assert exc_info.value.retryable is False


def test_newapi_still_reports_genuine_empty_as_empty_response() -> None:
    def transport(url, headers, body, timeout):
        return 200, b'{"choices":[{"message":{"content":""},"finish_reason":"stop"}]}'

    client = NewAPIClient(api_key="k", base_url="https://gateway.example/v1", transport=transport)
    with pytest.raises(ModelGatewayError) as exc_info:
        client.complete(_request())

    assert exc_info.value.code == "EMPTY_RESPONSE"


def test_opus_reports_max_tokens_stop_reason_as_truncation(monkeypatch) -> None:
    monkeypatch.setenv("TOKENHUB_KEY", "unit-secret")

    def transport(url, headers, body, timeout):
        return 200, b'{"content":[],"stop_reason":"max_tokens"}'

    client = OpusClient(base_url="https://gateway.example/v1", transport=transport)
    with pytest.raises(ModelGatewayError) as exc_info:
        client.complete(_request())

    assert exc_info.value.code == "RESPONSE_TRUNCATED"
    assert exc_info.value.retryable is False
