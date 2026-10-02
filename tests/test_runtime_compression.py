"""验证实际 Hermes 压缩可行性检查与辅助 SDK 调用，不访问网络。"""

import json
from types import SimpleNamespace

import pytest

from traceforge.reconstruction.agents.runtime import pin_hermes_compression


@pytest.mark.parametrize("aux_model,aux_endpoint,same_route", [
    ("gpt-6-astra/azure/sfa", "https://tokenhub.example/v1/", True),
    ("smaller-model", "https://tokenhub.example/v1/", False),
    ("gpt-6-astra/azure/sfa", "https://other.example/v1/", False),
])
def test_native_compression_keeps_own_window_and_role_timeout(
    monkeypatch, aux_model, aux_endpoint, same_route,
):
    auxiliary = pytest.importorskip("agent.auxiliary_client")
    compression = pytest.importorskip("agent.conversation_compression")
    metadata = pytest.importorskip("agent.model_metadata")
    httpx = pytest.importorskip("httpx")
    openai = pytest.importorskip("openai")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "fixture-summary", "object": "chat.completion", "created": 0,
            "model": aux_model, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "完整摘要"},
                 "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    client = openai.OpenAI(
        api_key="fixture", base_url=aux_endpoint,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(auxiliary, "_get_auxiliary_task_config", lambda task: {
        "provider": "custom", "model": aux_model, "base_url": aux_endpoint, "timeout": 30,
    })
    monkeypatch.setattr(auxiliary, "get_text_auxiliary_client",
                        lambda *args, **kwargs: (client, aux_model))
    monkeypatch.setattr(auxiliary, "_get_cached_client",
                        lambda *args, **kwargs: (client, aux_model))
    # 复现真实网关别名缺失元数据时返回256k，检查显式窗口是否到达原生入口。
    monkeypatch.setattr(metadata, "get_model_context_length",
                        lambda *args, **kwargs: kwargs.get("config_context_length") or 256_000)
    compressor = SimpleNamespace(
        model="gpt-6-astra/azure/sfa", provider="custom",
        base_url="https://tokenhub.example/v1", api_key="fixture",
        api_mode="chat_completions", context_length=1_000_000,
        threshold_tokens=500_000, threshold_percent=0.5,
    )
    runtime = {key: getattr(compressor, key)
               for key in ("model", "provider", "base_url", "api_key", "api_mode")}
    agent = SimpleNamespace(
        context_compressor=compressor, compression_enabled=True,
        _current_main_runtime=lambda: runtime, _custom_providers=[],
        _aux_compression_context_length_config=None, model=compressor.model,
        provider="custom", _emit_status=lambda *args: None,
    )
    original_timeout = auxiliary._get_task_timeout
    try:
        with pin_hermes_compression(agent, context_length=1_000_000, timeout_seconds=120):
            compression.check_compression_model_feasibility(agent)
            assert compressor.threshold_tokens == (500_000 if same_route else 256_000)
            response = auxiliary.call_llm(
                task="compression", main_runtime=runtime,
                messages=[{"role": "user", "content": "真实分支的固定测试输入"}],
            )
            assert response.choices[0].message.content == "完整摘要"
            assert json.loads(requests[-1].content)["model"] == aux_model
            assert requests[-1].extensions["timeout"]["read"] == 120
            assert auxiliary._get_task_timeout("web_extract") == 30
        assert auxiliary._get_task_timeout is original_timeout
        with pytest.raises(RuntimeError, match="测试中断"):
            with pin_hermes_compression(agent, context_length=None, timeout_seconds=90):
                raise RuntimeError("测试中断")
        assert auxiliary._get_task_timeout is original_timeout
    finally:
        client.close()
