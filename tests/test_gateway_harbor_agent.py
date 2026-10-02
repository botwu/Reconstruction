"""请求行为保持原生记录，只修复网关传输和模型名字被改写的问题。"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceforge.harbor_ags import gateway_harness


@pytest.mark.parametrize("model", [
    "anthropic/claude-opus-4-8/awsb_L/sfa", "gpt-6-astra/azure/sfa",
])
def test_gateway_overrides_normalized_model_at_actual_sdk_boundary(model):
    class NativeAgent:
        def __init__(self, **kwargs):
            self.api_mode = "anthropic_messages"
            self.model = kwargs["model"].split("/", 1)[-1]

        def _anthropic_messages_create(self, kwargs):
            return kwargs

    agent = gateway_harness.gateway_agent_type(NativeAgent)(model=model)
    original = {"model": "normalized-name", "stream": True, "messages": [{"role": "user"}]}
    result = agent._anthropic_messages_create(original)
    assert agent._disable_streaming is True
    assert result == {**original, "model": model, "stream": False}
    assert original["stream"] is True


def test_wrapper_runs_original_harness_and_restores_import(tmp_path, monkeypatch):
    class NativeAgent:
        def __init__(self, **kwargs):
            self.api_mode = "anthropic_messages"

        def _anthropic_messages_create(self, kwargs):
            return kwargs

    module = SimpleNamespace(AIAgent=NativeAgent)
    monkeypatch.setitem(sys.modules, "run_agent", module)
    monkeypatch.setattr(gateway_harness, "__file__", str(tmp_path / "gateway_harness.py"))
    output = tmp_path / "observed.json"
    (tmp_path / "hermes_harness_base.py").write_text(
        "import json\nfrom run_agent import AIAgent\nfrom pathlib import Path\n"
        "a=AIAgent(model='anthropic/exact-route/provider')\n"
        "result=a._anthropic_messages_create({'model':'stripped'})\n"
        f"Path({str(output)!r}).write_text(json.dumps(result))\n"
    )
    gateway_harness.main()
    assert json.loads(output.read_text()) == {
        "model": "anthropic/exact-route/provider", "stream": False,
    }
    assert module.AIAgent is NativeAgent


def test_gateway_adapter_reuses_native_harness_bytes(tmp_path, monkeypatch):
    import asyncio
    from importlib.resources import as_file, files

    native = pytest.importorskip("harbor_ags.agent")
    from traceforge.harbor_ags.agent import GatewayHermesAgent

    LosslessHermesAgent = native.LosslessHermesAgent

    called = []

    async def setup(self, environment):
        called.append("native_setup")

    async def upload_file(source, destination):
        called.append((destination, Path(source).read_bytes()))

    monkeypatch.setattr(LosslessHermesAgent, "setup", setup)
    agent = object.__new__(GatewayHermesAgent)
    asyncio.run(agent.setup(SimpleNamespace(upload_file=upload_file)))
    assert called[0] == "native_setup"
    with as_file(files("harbor_ags.resources").joinpath("hermes_harness.py")) as source:
        assert called[1] == (
            "/tmp/harbor_ags_runtime/hermes_harness_base.py", source.read_bytes(),
        )
    assert called[2][0] == "/tmp/harbor_ags_runtime/hermes_harness.py"
    assert called[2][1] == Path(gateway_harness.__file__).read_bytes()


def test_nonstreaming_wire_request_uses_exact_model_with_real_sdk():
    anthropic = pytest.importorskip("anthropic")
    httpx = pytest.importorskip("httpx")
    requests = []

    def receive(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_local_probe", "type": "message", "role": "assistant",
            "model": "claude-opus-4-8", "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    client = anthropic.Anthropic(
        api_key="local-test-only", base_url="http://local-test.invalid",
        http_client=httpx.Client(transport=httpx.MockTransport(receive)),
    )

    class NativeAgent:
        def __init__(self, **kwargs):
            self.api_mode = "anthropic_messages"

        def _anthropic_messages_create(self, kwargs):
            return client.messages.create(**kwargs)

    model = "anthropic/claude-opus-4-8/awsb_L/sfa"
    try:
        agent = gateway_harness.gateway_agent_type(NativeAgent)(model=model)
        response = agent._anthropic_messages_create({
            "model": "claude-opus-4-8/awsb_L/sfa", "max_tokens": 16,
            "messages": [{"role": "user", "content": "local probe"}],
        })
        assert response.stop_reason == "end_turn"
        assert requests[0]["model"] == model
        assert requests[0]["stream"] is False
    finally:
        client.close()
