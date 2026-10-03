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
    commands = []

    async def exec_as_root(self, environment, **kwargs):
        commands.append(kwargs["command"])
        return SimpleNamespace(return_code=0, stdout="ripgrep 14.1.1 (rev 4649aa9700)\n")

    async def setup(self, environment):
        called.append("native_setup")

    async def upload_file(source, destination):
        called.append((destination, Path(source).read_bytes()))

    monkeypatch.setattr(LosslessHermesAgent, "setup", setup)
    monkeypatch.setattr(GatewayHermesAgent, "exec_as_root", exec_as_root)
    agent = object.__new__(GatewayHermesAgent)
    asyncio.run(agent.setup(SimpleNamespace(upload_file=upload_file)))
    assert called[0] == "native_setup"
    with as_file(files("harbor_ags.resources").joinpath("hermes_harness.py")) as source:
        assert called[1] == (
            "/tmp/harbor_ags_runtime/hermes_harness_base.py", source.read_bytes(),
        )
    assert called[2][0] == "/tmp/harbor_ags_runtime/hermes_harness.py"
    assert called[2][1] == Path(gateway_harness.__file__).read_bytes()
    assert called[3][0].endswith("ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz")
    assert "uname -m" in commands[0] and "sha256sum" in commands[0]
    assert "/usr/local/bin/rg --version" in commands[0]


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


def test_gateway_setup_rejects_corrupt_ripgrep_before_upload(monkeypatch):
    import asyncio

    pytest.importorskip("harbor_ags.agent")
    from traceforge.harbor_ags.agent import GatewayHermesAgent

    original = Path.read_bytes

    def read_bytes(path):
        return b"corrupt" if path.name.endswith(".tar.gz") else original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    agent = object.__new__(GatewayHermesAgent)
    with pytest.raises(ValueError, match="ripgrep.*哈希"):
        asyncio.run(agent.setup(SimpleNamespace()))


@pytest.mark.parametrize("return_code", [0, 17])
def test_native_run_preserves_text_and_capture_credentials_on_both_exits(
    monkeypatch, return_code
):
    import asyncio

    native = pytest.importorskip("harbor_ags.agent")
    agent_type = native.LosslessHermesAgent
    agent = object.__new__(agent_type)
    for name, value in {
        "model_name": "anthropic/fixture", "workspace": "/home/user/workspace",
        "toolsets": "file,terminal", "input_profile": native.TASK_BUNDLE_INPUT_PROFILE,
        "capture_port": 8788, "max_iterations": 3, "context_id": "fixture",
        "session_id": "fixture", "hermes_source_dir": "/home/user/.hermes/hermes-agent",
        "_observed_commit": "fixture-commit",
    }.items():
        setattr(agent, name, value)
    uploaded, captures, executions, stopped = [], [], [], []

    async def upload(self, environment, **kwargs):
        uploaded.append(kwargs)

    async def start(self, environment, **kwargs):
        captures.append(kwargs)

    async def stop(self, environment):
        stopped.append(True)

    async def execute(self, environment, **kwargs):
        executions.append(kwargs)
        return SimpleNamespace(return_code=return_code)

    monkeypatch.setenv("HERMES_REDACT_SECRETS", "true")
    monkeypatch.setattr(agent_type, "_model_settings", lambda self: (
        "fixture", "https://upstream.invalid", "fixture-upstream-credential",
    ))
    monkeypatch.setattr(agent_type, "_upload_config_text", upload)
    monkeypatch.setattr(agent_type, "_start_capture", start)
    monkeypatch.setattr(agent_type, "_stop_capture", stop)
    monkeypatch.setattr(agent_type, "exec_as_agent", execute)
    monkeypatch.setattr(agent_type, "version", lambda self: "fixture")
    literal = 'BAD_AUTH = {"errors": [{"message": "Denied"}]}'
    instruction = literal + "\n\n" + native.render_runtime_appendix(
        workspace_root=agent.workspace, toolsets=agent.toolsets,
    )
    if return_code:
        with pytest.raises(RuntimeError, match="Hermes harness 退出码 17"):
            asyncio.run(agent._run(instruction, SimpleNamespace(), SimpleNamespace()))
    else:
        asyncio.run(agent._run(instruction, SimpleNamespace(), SimpleNamespace()))
    assert uploaded[0]["content"] == instruction
    assert captures == [{
        "upstream": "https://upstream.invalid", "api_key": "fixture-upstream-credential",
    }]
    assert stopped == [True]
    assert len(executions) == 1
    execution = executions[0]
    assert execution["env"]["HERMES_REDACT_SECRETS"] == "false"
    assert execution["env"]["HERMES_LLM_API_KEY"] == "capture-proxy-local"
    assert execution["env"]["HERMES_LLM_BASE_URL"] == "http://127.0.0.1:8788"
    assert "fixture-upstream-credential" not in json.dumps(execution)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "ANTHROPIC_AUTH_TOKEN",
                 "ANTHROPIC_BASE_URL", "TOKENHUB_KEY", "TOKENHUB_BASE_URL", "OPENAI_API_KEY"):
        assert f"-u {name}" in execution["command"]
        assert name not in execution["env"]
