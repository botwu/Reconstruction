from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

from hermes_fakes import FakeHermesFactory, tagged_record
from traceforge.reconstruction.agents import (
    COMPLETION_ROLE,
    DEFAULT_HERMES_HOME,
    INTENT_ROLE,
    SUFFICIENCY_ROLE,
    AgentSession,
    HermesUnavailableError,
    build_hermes_runtime,
    resolve_hermes_home,
    resolve_rollout_model,
)
from traceforge.reconstruction.agents.runtime import (
    _load_hermes_factory,
    anthropic_sdk_base_url,
    classify_hermes_failure,
    apply_anthropic_messages_client,
    merge_completion_files,
    is_fatal_tool_result,
    pin_anthropic_channel_env,
    pin_hermes_timeout_env,
)
from traceforge.reconstruction.agents.session import execute_tool
from traceforge.reconstruction.intent_recovery import run_intent_recovery
from traceforge.reconstruction.session_source import build_reconstruction_source
from traceforge.screening.observable import build_spans


def _session() -> dict[str, object]:
    return {
        "messages": [
            {"role": "user", "content": "把 foo.py 里的入口函数读出来，不要改文件"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "exec",
                            "arguments": {"command": "cat foo.py"},
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "def main():\n    return 1\n"},
        ],
        "meta": {},
        "tools": [],
        "domain_meta": {},
    }


def _record(raw_line: str) -> dict[str, object]:
    return tagged_record(raw_line)


def _runtime(factory: FakeHermesFactory | None = None):
    return build_hermes_runtime(
        model_name="claude-opus-4-6",
        factory=factory or FakeHermesFactory(),
        base_url="https://tokenhub.example/v1/chat/completions",
        api_key="sk-test",
        provider="anthropic",
    )


def test_unknown_evidence_reference_does_not_discard_agent_payload() -> None:
    """A bad read reference is recoverable; policy writes remain fatal."""

    assert is_fatal_tool_result("read_evidence", "error: unknown evidence_ref_id: ev-typo") is False
    assert is_fatal_tool_result("write_file", "error: PROTECTED_FILE_OVERWRITE:foo.py") is True


def test_runtime_copies_replay_files_when_workspace_missing(tmp_path: Path) -> None:
    runtime = _runtime()
    session = AgentSession(replay_files={"foo.py": "def main():\n    return 1\n"})
    runtime.run(
        role=INTENT_ROLE,
        instruction="recover q",
        session=session,
        output_root=tmp_path / "out",
    )
    copied = tmp_path / "out/hermes_workspace/foo.py"
    assert copied.read_text(encoding="utf-8") == "def main():\n    return 1\n"


def test_hermes_runtime_configures_model_on_agent(tmp_path: Path) -> None:
    factory = FakeHermesFactory()
    runtime = _runtime(factory)
    raw_line = json.dumps(_session(), ensure_ascii=False)
    source = build_reconstruction_source(raw_line=raw_line, record=_record(raw_line))
    result = run_intent_recovery(
        source=source,
        agent=runtime,
        output_root=tmp_path / "intent",
    )
    assert result["status"] == "READY"
    assert result["tasks"][0]["agent"]["backend"] == "hermes"
    assert factory.last_kwargs["model"] == "claude-opus-4-6"
    assert factory.last_kwargs["provider"] == "anthropic"
    assert factory.last_kwargs["base_url"] == "https://tokenhub.example/v1"
    assert factory.last_kwargs["api_key"] == "sk-test"
    task_id = source["tasks"][0]["task_id"]
    trace = json.loads((tmp_path / "intent/tasks" / task_id / "private/agent_trace.json").read_text(encoding="utf-8"))
    assert trace["model"] == "claude-opus-4-6"
    assert "instruction" not in trace
    assert trace["privacy"]["private_thinking_reasoning"] == "omitted"
    assert "Intent Recovery Agent" in INTENT_ROLE.identity


def test_build_hermes_runtime_reads_config_channel(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        'claude:\n  {"url": "https://tokenhub.example/v1", "key": "sk-from-config"}\n',
        encoding="utf-8",
    )
    factory = FakeHermesFactory()
    runtime = build_hermes_runtime(
        model_name="claude-opus-4-6",
        config_path=config,
        channel="claude",
        factory=factory,
    )
    runtime.run(
        role=INTENT_ROLE,
        instruction="recover q",
        session=AgentSession(user_texts=["只读展示入口"]),
        output_root=tmp_path / "out",
    )
    assert factory.last_kwargs["model"] == "claude-opus-4-6"
    assert factory.last_kwargs["api_key"] == "sk-from-config"
    assert factory.last_kwargs["provider"] == "anthropic"


def test_anthropic_sdk_base_url_strips_v1() -> None:
    assert anthropic_sdk_base_url("https://tokenhub.sensetime.com/") == (
        "https://tokenhub.sensetime.com/"
    )
    assert anthropic_sdk_base_url("https://tokenhub.sensetime.com/v1") == (
        "https://tokenhub.sensetime.com/"
    )
    assert anthropic_sdk_base_url(
        "https://tokenhub.sensetime.com/v1/chat/completions"
    ) == "https://tokenhub.sensetime.com/"


def test_apply_anthropic_messages_client_uses_sdk_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeAnthropic:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    class Agent:
        api_key = "old"
        _anthropic_api_key = "old"
        _anthropic_client = "old"

    agent = Agent()
    apply_anthropic_messages_client(
        agent,
        base_url="https://tokenhub.sensetime.com/v1",
        api_key="sk-test",
    )
    assert captured["base_url"] == "https://tokenhub.sensetime.com/"
    assert captured["api_key"] == "sk-test"
    assert "timeout" in captured
    assert agent.api_key == "sk-test"
    assert agent._anthropic_api_key == "sk-test"


def test_apply_anthropic_messages_client_skips_fake_agent() -> None:
    agent = FakeHermesFactory()()
    apply_anthropic_messages_client(
        agent, base_url="https://tokenhub.sensetime.com/", api_key="sk-test"
    )
    assert not hasattr(agent, "_anthropic_client")
    assert not getattr(agent, "_traceforge_tokenhub_hooks", False)


def test_apply_anthropic_messages_client_rewires_stale_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TRACEFORGE_MODEL_TIMEOUT_SECONDS", raising=False)
    captured: list[object] = []

    class FakeAnthropic:
        def __init__(self, **kwargs: object) -> None:
            captured.append(dict(kwargs))

        def close(self) -> None:
            captured.append("closed")

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    class Agent:
        api_mode = "anthropic_messages"
        api_key = "old"
        _anthropic_api_key = "old"
        _anthropic_client = "old"

        def _replace_primary_openai_client(self, *, reason: str) -> bool:
            raise AssertionError(f"must not rebuild OpenAI: {reason}")

        def _rebuild_anthropic_client(self) -> None:
            raise AssertionError("must not use Hermes .env rebuild")

        def _try_refresh_anthropic_client_credentials(self) -> bool:
            return True

        def _compute_non_stream_stale_timeout(self, api_kwargs: object) -> float:
            return 180.0

    agent = Agent()
    apply_anthropic_messages_client(
        agent,
        base_url="https://tokenhub.sensetime.com/v1",
        api_key="sk-test",
    )
    assert agent._disable_streaming is True
    assert agent._try_refresh_anthropic_client_credentials() is False
    assert agent._compute_non_stream_stale_timeout({}) == 120.0
    assert captured[0]["api_key"] == "sk-test"
    assert captured[0]["timeout"] == 120.0
    first_client = agent._anthropic_client
    assert agent._replace_primary_openai_client(reason="stale_stream_pool_cleanup") is True
    assert "closed" in captured
    assert agent._anthropic_client is not first_client
    assert captured[-1]["api_key"] == "sk-test"
    assert captured[-1]["base_url"] == "https://tokenhub.sensetime.com/"


def test_apply_anthropic_messages_client_hooks_when_client_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    captured: list[object] = []

    class FakeAnthropic:
        def __init__(self, **kwargs: object) -> None:
            captured.append(dict(kwargs))

        def close(self) -> None:
            captured.append("closed")

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = FakeAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    class Agent:
        api_mode = "anthropic_messages"
        api_key = "old"
        _anthropic_api_key = "old"
        _anthropic_client = None

        def _replace_primary_openai_client(self, *, reason: str) -> bool:
            raise AssertionError(f"must not rebuild OpenAI: {reason}")

        def _interruptible_api_call(self, api_kwargs: object) -> str:
            return "nonstream"

        def _interruptible_streaming_api_call(
            self, api_kwargs: object, on_first_delta: object = None
        ) -> str:
            raise AssertionError("streaming path must be redirected")

    agent = Agent()
    apply_anthropic_messages_client(
        agent,
        base_url="https://tokenhub.sensetime.com/v1",
        api_key="sk-test",
    )
    assert agent._disable_streaming is True
    assert agent._traceforge_tokenhub_hooks is True
    assert isinstance(agent._anthropic_client, FakeAnthropic)
    assert agent._interruptible_streaming_api_call({"model": "x"}) == "nonstream"
    assert agent._replace_primary_openai_client(reason="stale_stream_pool_cleanup") is True
    assert "closed" in captured


def test_pin_hermes_timeout_env_caps_stream_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACEFORGE_MODEL_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "180")
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")
    monkeypatch.delenv("HERMES_API_CALL_STALE_TIMEOUT", raising=False)
    with pin_hermes_timeout_env() as timeout:
        assert timeout == 90.0
        assert os.environ["HERMES_STREAM_STALE_TIMEOUT"] == "90"
        assert os.environ["HERMES_API_CALL_STALE_TIMEOUT"] == "90"
        assert os.environ["HERMES_STREAM_RETRIES"] == "0"
    assert os.environ["HERMES_STREAM_STALE_TIMEOUT"] == "180"
    assert os.environ["HERMES_STREAM_RETRIES"] == "2"
    assert "HERMES_API_CALL_STALE_TIMEOUT" not in os.environ


@pytest.mark.parametrize("limit", [3, 100000])
def test_runtime_honors_explicit_role_iteration_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limit: int
) -> None:
    monkeypatch.setenv("TRACEFORGE_AGENT_MAX_ITERATIONS", str(limit))
    factory = FakeHermesFactory()
    runtime = _runtime(factory)
    runtime.run(
        role=COMPLETION_ROLE,
        instruction="complete q",
        session=AgentSession(user_texts=["补齐入口上下文"]),
        output_root=tmp_path / "out",
    )
    assert factory.last_kwargs["max_iterations"] == limit


def test_hermes_runtime_forces_non_stream_even_on_fake_agent(tmp_path: Path) -> None:
    factory = FakeHermesFactory()
    runtime = _runtime(factory)
    runtime.run(
        role=INTENT_ROLE,
        instruction="recover q",
        session=AgentSession(user_texts=["只读展示入口"]),
        output_root=tmp_path / "out",
    )
    agent = factory.last_agent
    assert agent is not None
    assert agent._disable_streaming is True


def test_pin_anthropic_channel_env_hides_hermes_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-hermes-env")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "sk-hermes-token")
    with pin_anthropic_channel_env("sk-channel"):
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-channel"
        assert "ANTHROPIC_TOKEN" not in os.environ
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-hermes-env"
    assert os.environ["ANTHROPIC_TOKEN"] == "sk-hermes-token"


def test_build_hermes_runtime_requires_real_agent_without_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "traceforge.reconstruction.agents.runtime._load_hermes_factory",
        lambda _home=None: None,
    )
    with pytest.raises(HermesUnavailableError, match="AIAgent"):
        build_hermes_runtime(model_name="claude-opus-4-6")


def test_completion_agent_rejects_protected_write() -> None:
    session = AgentSession(
        replay_files={"foo.py": "def main():\n    return 1\n"},
        protected_paths={"foo.py"},
        evidence=[{"evidence_ref_id": "c1", "name": "exec"}],
        allow_write=True,
    )
    result = execute_tool(
        "write_file",
        {"path": "foo.py", "content": "changed\n", "evidence_ref_ids": ["c1"]},
        session,
    )
    assert "PROTECTED_FILE_OVERWRITE" in result
    assert session.writes == []


def test_role_identities_are_distinct() -> None:
    assert "Intent Recovery Agent" in INTENT_ROLE.identity
    assert "Workspace Completion Agent" in COMPLETION_ROLE.identity
    assert "Workspace Sufficiency Agent" in SUFFICIENCY_ROLE.identity
    assert INTENT_ROLE.allow_write is False
    assert COMPLETION_ROLE.allow_write is True
    assert SUFFICIENCY_ROLE.allow_write is False
    assert "terminal" not in COMPLETION_ROLE.toolsets


def _fake_hermes_home(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "run_agent.py").write_text(
        "class AIAgent:\n    marker = 'tmp-hermes'\n",
        encoding="utf-8",
    )
    return tmp_path


def test_resolve_hermes_home_prefers_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_hermes_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert resolve_hermes_home() == home.resolve()


def test_resolve_hermes_home_prefers_explicit_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_home = _fake_hermes_home(tmp_path / "env")
    explicit = _fake_hermes_home(tmp_path / "explicit")
    monkeypatch.setenv("HERMES_HOME", str(env_home))
    assert resolve_hermes_home(explicit) == explicit.resolve()


def test_resolve_hermes_home_uses_default_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_hermes_home(tmp_path)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(
        "traceforge.reconstruction.agents.runtime.DEFAULT_HERMES_HOME",
        home,
    )
    assert resolve_hermes_home() == home.resolve()
    assert DEFAULT_HERMES_HOME.name == "hermes-agent"


def test_resolve_hermes_home_requires_run_agent(tmp_path: Path) -> None:
    with pytest.raises(HermesUnavailableError, match="run_agent.py"):
        resolve_hermes_home(tmp_path)


def test_load_hermes_factory_imports_from_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _fake_hermes_home(tmp_path)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    previous = sys.modules.pop("run_agent", None)
    root = str(home.resolve())
    try:
        factory = _load_hermes_factory(home)
        assert getattr(factory, "marker", None) == "tmp-hermes"
    finally:
        sys.modules.pop("run_agent", None)
        if root in sys.path:
            sys.path.remove(root)
        if previous is not None:
            sys.modules["run_agent"] = previous


def test_merge_completion_files_prefers_sandbox_write() -> None:
    session = AgentSession(allow_write=True)
    session.writes.append(
        {
            "path": "Config.h",
            "content": "#pragma once\n// observed name, body unobserved\n",
            "provenance": "SYNTHETIC_STUB",
            "evidence_ref_ids": ["c1"],
        }
    )
    payload = merge_completion_files(
        {
            "candidates": [
                {
                    "files": [
                        {
                            "path": "Config.h",
                            "content": "different json body",
                            "provenance": "MODEL_COMPLETED",
                            "evidence_ref_ids": ["c1"],
                        }
                    ],
                    "decision": "READY",
                }
            ]
        },
        session,
    )
    assert payload.get("_merge_errors") in (None, [])
    assert payload["candidates"][0]["files"][0]["content"].startswith("#pragma once")


@pytest.mark.parametrize("retry_missing_refs", [False, True])
def test_production_runtime_binds_role_scoped_proxy_and_writes_with_provenance(
    tmp_path: Path, retry_missing_refs: bool,
) -> None:
    class ProxyAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_conversation(self, instruction, system_message=None, task_id=None):
            names = {item["function"]["name"] for item in self.tools}
            assert names == {
                "list_dir",
                "read_file",
                "list_evidence",
                "read_evidence",
                "write_file",
                "web_search",
            }
            assert self._skip_mcp_refresh is True
            assert self._invoke_tool("list_evidence", {}, task_id).startswith("[")
            if retry_missing_refs:
                rejected = self._invoke_tool(
                    "write_file", {"path": "context.txt", "content": "from-evidence"}, task_id,
                )
                assert rejected == "error: evidence_ref_ids required"
                assert not (root / "context.txt").exists()
            assert self._invoke_tool(
                "write_file",
                {"path": "context.txt", "content": "from-evidence", "evidence_ref_ids": ["e1"]},
                task_id,
            ) == "wrote context.txt"
            return {"final_response": "{\"candidates\":[]}", "completed": True, "messages": []}

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "complete.txt").write_text("complete", encoding="utf-8")
    runtime = build_hermes_runtime(
        model_name="test-model", factory=lambda **kwargs: ProxyAgent(**kwargs),
        base_url="https://example.test", api_key="sk-test",
    )
    session = AgentSession(
        workspace=root, evidence=[{"evidence_ref_id": "e1", "name": "exec", "text": "ok"}],
        protected_paths={"complete.txt"}, allow_write=True,
    )
    result = runtime.run(role=COMPLETION_ROLE, instruction="x", session=session, output_root=tmp_path / "out")
    assert not result.errors
    assert (root / "context.txt").read_text(encoding="utf-8") == "from-evidence"
    assert session.writes[0]["evidence_ref_ids"] == ["e1"]


def test_production_runtime_blocks_protected_write_and_sufficiency_is_read_only(tmp_path: Path) -> None:
    class ProxyAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_conversation(self, instruction, system_message=None, task_id=None):
            names = {item["function"]["name"] for item in self.tools}
            assert names == {"list_dir", "read_file", "run_environment_probe"}
            assert self._invoke_tool("write_file", {"path": "x", "content": "bad", "evidence_ref_ids": ["e1"]}, task_id).startswith("error:")
            return {"final_response": "{\"label\":\"SUFFICIENT\",\"decision\":\"READY\",\"confidence\":0.8}", "completed": True, "messages": []}

    root = tmp_path / "workspace"
    root.mkdir()
    runtime = build_hermes_runtime(
        model_name="test-model", factory=lambda **kwargs: ProxyAgent(**kwargs),
        base_url="https://example.test", api_key="sk-test",
    )
    session = AgentSession(workspace=root, evidence=[{"evidence_ref_id": "e1"}], allow_write=False)
    result = runtime.run(role=SUFFICIENCY_ROLE, instruction="x", session=session, output_root=tmp_path / "out")
    assert "TOOL_NOT_ALLOWED:write_file" in result.errors
    assert not list(root.iterdir())


def test_resolve_rollout_model_uses_channel_not_forged_anthropic() -> None:
    assert resolve_rollout_model(None, channel="deepseek", model_name="vol/deepseek-v4") == (
        "vol/vol/deepseek-v4"
    )
    assert resolve_rollout_model(None, channel="deepseek", model_name="deepseek-v4") == (
        "deepseek/deepseek-v4"
    )
    assert resolve_rollout_model("anthropic/claude-x", channel="deepseek", model_name="x") == (
        "anthropic/claude-x"
    )


def test_openai_root_endpoint_adds_v1():
    from traceforge.reconstruction.agents.runtime import openai_sdk_base_url, HermesNativeRuntime
    assert openai_sdk_base_url("https://tokenhub.example") == "https://tokenhub.example/v1"
    assert openai_sdk_base_url("https://tokenhub.example/v1/chat/completions") == "https://tokenhub.example/v1"
    assert openai_sdk_base_url("https://proxy.example/custom-api") == "https://proxy.example/custom-api"
    runtime = HermesNativeRuntime(factory=lambda **kw: None, base_url="https://tokenhub.example", api_key="fixture", model_name="bailian/deepseek", provider="bailian")
    assert runtime.base_url == "https://tokenhub.example/v1"


def test_namespaced_rollout_model_preserves_upstream_id():
    value = resolve_rollout_model(None, channel="deepseek", model_name="bailian/deepseek-v4-flash-0731")
    provider, upstream = value.split("/", 1)
    assert provider == "bailian"
    assert upstream == "bailian/deepseek-v4-flash-0731"


def test_classify_hermes_failure_ignores_timeout_in_valid_json() -> None:
    assert classify_hermes_failure(
        '{"success_criteria":["add timeout/retry guidance"]}'
    ) is None
    assert classify_hermes_failure("API call failed (attempt 1/3): timeout") == (
        "MODEL_API_FAILED"
    )


def test_non_anthropic_runtime_forces_chat_completions(tmp_path: Path) -> None:
    factory = FakeHermesFactory()
    runtime = build_hermes_runtime(
        model_name="gpt-5",
        factory=factory,
        base_url="https://tokenhub.example/v1",
        api_key="sk-test",
        provider="gpt",
    )
    result = runtime.run(
        role=INTENT_ROLE,
        instruction="return a JSON intent",
        session=AgentSession(),
        output_root=tmp_path / "out",
    )
    assert result.completed
    assert factory.last_kwargs["api_mode"] == "chat_completions"
