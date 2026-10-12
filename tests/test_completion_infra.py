"""按 R04:1090 的真实异常形状区分当前沙箱故障与原材料缺口。"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from hermes_fakes import FakeHermesAgent

from traceforge.reconstruction.agents import (
    COMPLETION_ROLE,
    SandboxedAgentRuntime,
    build_hermes_runtime,
)
from traceforge.reconstruction.agents.runtime import _bind_agent_tools
from traceforge.reconstruction.agents.sandbox import LocalExecRuntime, SandboxBinding
from traceforge.reconstruction.agents.session import AgentSession
from traceforge.reconstruction.terminal_universe_environment import ReplayedFile, ReplayResult
from traceforge.reconstruction.workspace_completion import run_workspace_completion


@pytest.mark.parametrize("transport_failure", [True, False])
def test_sandbox_transport_failure_finishes_tool_audit_without_claiming_missing_source(
    tmp_path: Path, transport_failure: bool,
) -> None:
    class Runtime:
        async def exec(self, *args, **kwargs):
            if transport_failure:
                raise RuntimeError("Code.unknown: ")
            return SimpleNamespace(return_code=1, stdout="", stderr="FileNotFoundError")

    session = AgentSession(sandbox=SandboxBinding(Runtime()))
    holder = SimpleNamespace()
    trace = tmp_path / "tool_events.jsonl"
    _bind_agent_tools(holder, role=COMPLETION_ROLE, session=session, trace_path=trace)
    result = holder._invoke_tool("read_file", {"path": "helper.java"}, "completion", "read-1")
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [event["status"] for event in events] == ["STARTED", "FINISHED"]
    assert events[-1]["ok"] is False
    assert events[-1]["tool_call_id"] == "read-1"
    if transport_failure:
        assert result.startswith("error: INFRA_ERROR_SANDBOX_EXEC:")
        assert "当前重建沙箱" in result
        assert session.policy_errors[0].startswith("INFRA_ERROR_SANDBOX_EXEC:")
        assert "file not found" not in result
        assert events[-1]["infrastructure_error"]["message"] == "Code.unknown: "
    else:
        assert result == "error: file not found"
        assert not session.policy_errors


@pytest.mark.parametrize(
    "tool_fails,cleanup_fails,decision",
    [(True, True, "REVIEW"), (True, False, "READY"),
     (False, True, "READY"), (False, False, "REVIEW")],
)
def test_completion_keeps_model_and_cleanup_facts_separate_and_withholds_infra_questions(
    tmp_path: Path, tool_fails: bool, cleanup_fails: bool, decision: str,
) -> None:
    question = "请恢复原文件的读取、编辑能力；若涉及安全软件加密，请手动解密。"
    payload = {"candidates": [{
        "files": [], "dependencies": [], "runtime_constraints": [],
        "uncertainties": [], "decision": decision,
    }], "open_questions": [question]}

    class Runtime(LocalExecRuntime):
        async def exec(self, command, **kwargs):
            if tool_fails and "p.read_text(encoding=" in command:
                raise RuntimeError("Code.unknown: ")
            return await super().exec(command, **kwargs)

        async def stop(self, *, delete=True):
            if cleanup_fails:
                raise RuntimeError("清理服务失败")
            await super().stop(delete=delete)

    class Model(FakeHermesAgent):
        def run_conversation(self, instruction, system_message=None, task_id=None):
            # Hermes 实际执行器会把抛出异常呈现给模型，模型仍可完成最终 JSON。
            with suppress(RuntimeError):
                self._invoke_tool("read_file", {"path": "helper.java"}, task_id, "read-1")
            return super().run_conversation(instruction, system_message, task_id)

    runtime = Runtime(tmp_path / "sandbox")
    agent = SandboxedAgentRuntime(build_hermes_runtime(
        model_name="fixture", base_url="https://example.test", api_key="fixture",
        factory=lambda **kwargs: Model(completion=payload, **kwargs),
    ), lambda: runtime)
    output = tmp_path / "completion"
    result = run_workspace_completion(
        task={"core_objective": "检查原 helper"},
        replay=ReplayResult(
            (ReplayedFile("helper.java", "class Helper {}", "ev", "PARTIAL"),), (), (), (),
        ),
        timeline=[], agent=agent, output_root=output,
    )
    assert result["status"] == "REVIEW"
    assert result["agent"]["model_completed"] is True
    assert result["agent"]["sandbox_cleanup_confirmed"] is not cleanup_fails
    assert all(candidate["workspace"] is None for candidate in result["candidates"])
    if tool_fails or cleanup_fails:
        assert result["agent"]["completed"] is False
        assert result["reconstructability"]["status"] == "INFRA_ERROR"
        assert result["open_questions"] == []
        expected_questions = [] if tool_fails else [question]
        assert result["diagnostics"]["unassessed_open_questions"] == expected_questions
        assert not any("MODEL_DECISION_" in error for error in result["errors"])
        assert not any(
            "MODEL_DECISION_" in error
            for candidate in result["candidates"] for error in candidate["errors"]
        )
        assert not (output / "candidates").exists()
        if cleanup_fails:
            assert "CONTAINER_CLEANUP_ERROR:RuntimeError" in result["errors"]
    else:
        assert result["agent"]["completed"] is True
        assert result["open_questions"] == [question]
        assert "CANDIDATE_000:MODEL_DECISION_REVIEW" in result["errors"]
    private = json.loads((output / "private/model_exchange.json").read_text())
    assert json.loads(private["response"]["text"]) == payload
