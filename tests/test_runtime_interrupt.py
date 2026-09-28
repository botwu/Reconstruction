"""中断必须向调用方传播，沙盒清理仍应执行。"""

from pathlib import Path

import pytest

from traceforge.reconstruction.agents import sandbox
from traceforge.reconstruction.agents.roles import COMPLETION_ROLE
from traceforge.reconstruction.agents.runtime import AgentResult, SandboxedAgentRuntime
from traceforge.reconstruction.agents.session import AgentSession


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_sandbox_runtime_preserves_exception_control_flow_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    error = error_type("测试异常")
    events: list[str] = []
    deleted: list[bool] = []

    async def prepare_stub(**kwargs: object) -> None:
        events.append("prepare")

    class StubContainer:
        async def stop(self, *, delete: bool) -> None:
            events.append("stop")
            deleted.append(delete)

    class StubAgent:
        model_name = "stub"

        def run(self, **kwargs: object) -> AgentResult:
            events.append("run")
            raise error

    monkeypatch.setattr(sandbox, "prepare_role_sandbox", prepare_stub)
    runtime = SandboxedAgentRuntime(StubAgent(), StubContainer)
    session = AgentSession()
    arguments = {
        "role": COMPLETION_ROLE,
        "instruction": "只验证异常传播与清理",
        "session": session,
        "output_root": tmp_path,
    }

    if error_type is RuntimeError:
        result = runtime.run(**arguments)
        assert result.errors == ["AGENT_RUNTIME_ERROR:RuntimeError:测试异常"]
        assert result.backend == "hermes-sandbox"
        assert result.payload == {}
        assert result.completed is False
    else:
        with pytest.raises(error_type) as caught:
            runtime.run(**arguments)
        assert caught.value is error

    assert events == ["prepare", "run", "stop"]
    assert deleted == [True]
    assert session.sandbox_stopped is True
    assert session.sandbox_cleanup_error is None
    assert session.policy_errors == []
