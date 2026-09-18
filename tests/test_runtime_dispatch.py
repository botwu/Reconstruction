from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from traceforge.reconstruction.agents import COMPLETION_ROLE, AgentSession, build_hermes_runtime


class NativeDispatchAgent:
    def run_conversation(self, instruction, system_message=None, task_id=None):
        import run_agent
        result = run_agent.handle_function_call(
            "read_file", {"path": "foo.py"}, task_id=task_id, tool_call_id="tc1"
        )
        return {
            "final_response": json.dumps({"observed": result}, ensure_ascii=False),
            "completed": True,
            "messages": [],
            "api_calls": 1,
        }


def test_native_executor_routes_traceforge_role_tool(tmp_path: Path, monkeypatch) -> None:
    native_module = SimpleNamespace(handle_function_call=lambda *args, **kwargs: "native-bypass")
    monkeypatch.setitem(sys.modules, "run_agent", native_module)
    runtime = build_hermes_runtime(
        model_name="test-model",
        factory=lambda **kwargs: NativeDispatchAgent(),
        base_url="https://example.test",
        api_key="sk-test",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "foo.py").write_text("def main(): return 1\n", encoding="utf-8")
    session = AgentSession(workspace=workspace, replay_files={"foo.py": "def main(): return 1\n"})
    result = runtime.run(
        role=COMPLETION_ROLE,
        instruction="inspect",
        session=session,
        output_root=tmp_path / "run",
    )
    assert result.completed is True
    assert result.payload["observed"].startswith("def main")
    assert session.tool_events[0]["name"] == "read_file"
    assert native_module.handle_function_call("x") == "native-bypass"


def test_native_executor_does_not_fallback_to_unscoped_tool(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def native(*args, **kwargs):
        calls.append(str(args[0]))
        return "native-bypass"

    native_module = SimpleNamespace(handle_function_call=native)
    monkeypatch.setitem(sys.modules, "run_agent", native_module)

    class UnknownToolAgent:
        def run_conversation(self, instruction, system_message=None, task_id=None):
            import run_agent

            result = run_agent.handle_function_call(
                "write_test", {"content": "def test_noop(): pass"}, task_id=task_id
            )
            return {"final_response": json.dumps({"observed": result}), "completed": True}

    runtime = build_hermes_runtime(
        model_name="test-model",
        factory=lambda **kwargs: UnknownToolAgent(),
        base_url="https://example.test",
        api_key="sk-test",
    )
    session = AgentSession(workspace=tmp_path / "workspace")
    result = runtime.run(
        role=COMPLETION_ROLE,
        instruction="inspect",
        session=session,
        output_root=tmp_path / "run",
    )
    assert calls == []
    assert "TOOL_NOT_ALLOWED:write_test" in result.errors
    assert native_module.handle_function_call("x") == "native-bypass"


def test_dispatcher_restores_missing_attribute_after_exception(tmp_path: Path, monkeypatch) -> None:
    native_module = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "run_agent", native_module)

    class FailingAgent:
        def run_conversation(self, instruction, system_message=None, task_id=None):
            raise RuntimeError("model failed")

    runtime = build_hermes_runtime(
        model_name="test-model",
        factory=lambda **kwargs: FailingAgent(),
        base_url="https://example.test",
        api_key="sk-test",
    )
    runtime.run(
        role=COMPLETION_ROLE,
        instruction="inspect",
        session=AgentSession(workspace=tmp_path / "workspace"),
        output_root=tmp_path / "run",
    )
    assert not hasattr(native_module, "handle_function_call")


def test_hermes_global_dispatch_and_cwd_are_serialized(tmp_path: Path, monkeypatch) -> None:
    native_module = SimpleNamespace(handle_function_call=lambda *args, **kwargs: "native")
    monkeypatch.setitem(sys.modules, "run_agent", native_module)
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    class BlockingAgent:
        def run_conversation(self, instruction, system_message=None, task_id=None):
            nonlocal calls
            with calls_lock:
                calls += 1
                number = calls
            if number == 1:
                entered.set()
                assert release.wait(timeout=5)
            else:
                second_entered.set()
            return {"final_response": json.dumps({"ok": True}), "completed": True}

    runtime = build_hermes_runtime(
        model_name="test-model",
        factory=lambda **kwargs: BlockingAgent(),
        base_url="https://example.test",
        api_key="sk-test",
    )

    first = threading.Thread(
        target=runtime.run,
        kwargs={
            "role": COMPLETION_ROLE,
            "instruction": "first",
            "session": AgentSession(workspace=tmp_path / "first"),
            "output_root": tmp_path / "run-first",
        },
    )
    second = threading.Thread(
        target=runtime.run,
        kwargs={
            "role": COMPLETION_ROLE,
            "instruction": "second",
            "session": AgentSession(workspace=tmp_path / "second"),
            "output_root": tmp_path / "run-second",
        },
    )
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    time.sleep(0.1)
    assert not second_entered.is_set()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert second_entered.is_set()
    assert native_module.handle_function_call("x") == "native"
