from __future__ import annotations

import json
from pathlib import Path

from hermes_fakes import FakeHermesFactory
from traceforge.reconstruction.agents import (
    COMPLETION_ROLE,
    SandboxedAgentRuntime,
    build_hermes_runtime,
)
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.sandbox import LocalExecRuntime
from traceforge.reconstruction.agents.session import AgentSession, execute_tool
from traceforge.reconstruction.terminal_universe_environment import validate_completion_candidate
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.workspace_completion import run_workspace_completion


def test_bind_skips_hermes_mcp_refresh(tmp_path: Path) -> None:
    from traceforge.reconstruction.agents.runtime import _bind_agent_tools

    class Holder:
        pass

    agent = Holder()
    session = AgentSession(allow_write=True)
    _bind_agent_tools(agent, role=COMPLETION_ROLE, session=session)
    assert agent._skip_mcp_refresh is True
    assert {item["function"]["name"] for item in agent.tools} == {
        "list_dir",
        "read_file",
        "list_evidence",
        "read_evidence",
        "write_file",
        "web_search",
        "web_search",
    }


def test_sandbox_write_uses_runtime_not_host_workspace(tmp_path: Path) -> None:
    runtime = LocalExecRuntime(tmp_path / "ags")
    factory = FakeHermesFactory(
        completion={
            "candidates": [
                {
                    "files": [],
                    "dependencies": [],
                    "runtime_constraints": [],
                    "uncertainties": [],
                    "decision": "READY",
                }
            ]
        }
    )
    agent = SandboxedAgentRuntime(
        build_hermes_runtime(
            model_name="claude-opus-4-6",
            factory=factory,
            base_url="https://tokenhub.example/v1",
            api_key="sk-test",
        ),
        lambda: runtime,
    )
    workspace = tmp_path / "host_workspace"
    workspace.mkdir()
    (workspace / "foo.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    session = AgentSession(
        workspace=workspace,
        evidence=[{"evidence_ref_id": "ev-1", "name": "read"}],
        allow_write=True,
    )
    result = agent.run(
        role=COMPLETION_ROLE,
        instruction="inspect only",
        session=session,
        output_root=tmp_path / "out",
    )
    assert isinstance(result, AgentResult)
    assert result.backend == "hermes-sandbox"
    assert runtime.started and runtime.stopped
    assert (runtime.remote / "foo.py").is_file()
    assert not (workspace / "support.py").exists()


def test_sandbox_file_tools_roundtrip(tmp_path: Path) -> None:
    binding_runtime = LocalExecRuntime(tmp_path)
    session = AgentSession(
        evidence=[{"evidence_ref_id": "ev-1"}],
        allow_write=True,
    )
    from traceforge.reconstruction.agents.sandbox import run_coro, prepare_role_sandbox
    from traceforge.reconstruction.agents.roles import COMPLETION_ROLE

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    session.workspace = workspace
    run_coro(
        prepare_role_sandbox(
            role=COMPLETION_ROLE,
            runtime=binding_runtime,
            session=session,
            staging_root=tmp_path / "stage",
        )
    )
    listed = execute_tool("list_dir", {"path": "."}, session)
    assert "a.txt" in listed
    assert "hello" in execute_tool("read_file", {"path": "a.txt"}, session)
    written = execute_tool(
        "write_file",
        {"path": "b.txt", "content": "new", "evidence_ref_ids": ["ev-1"]},
        session,
    )
    assert written.startswith("wrote")
    assert "new" in execute_tool("read_file", {"path": "b.txt"}, session)


def test_file_tools_accept_hermes_scratch_aliases(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    scratch = tmp_path / "completion" / "hermes_scratch"
    workspace.mkdir()
    scratch.mkdir(parents=True)
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    session = AgentSession(workspace=workspace, path_aliases=[scratch], allow_write=True)
    session.evidence = [{"evidence_ref_id": "ev-1"}]
    assert "hello" in execute_tool(
        "read_file", {"path": str(scratch / "a.txt")}, session
    )
    written = execute_tool(
        "write_file",
        {
            "path": str(scratch / "b.txt"),
            "content": "new",
            "evidence_ref_ids": ["ev-1"],
        },
        session,
    )
    assert written.startswith("wrote")
    assert session.writes[0]["path"] == "b.txt"
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "new"


def test_file_tools_remap_intent_hermes_workspace_host_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = AgentSession(workspace=workspace, allow_write=True)
    session.evidence = [{"evidence_ref_id": "ev-1"}]
    leaked = (
        tmp_path
        / "intent"
        / "tasks"
        / "task_x"
        / "hermes_workspace"
        / "imgui"
        / "imgui.h"
    )
    written = execute_tool(
        "write_file",
        {
            "path": str(leaked),
            "content": "// stub\n",
            "evidence_ref_ids": ["ev-1"],
        },
        session,
    )
    assert written.startswith("wrote")
    assert session.writes[0]["path"] == "imgui/imgui.h"
    assert (workspace / "imgui" / "imgui.h").read_text(encoding="utf-8") == "// stub\n"


def test_file_tools_accept_absolute_workspace_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    session = AgentSession(workspace=workspace)
    listed = execute_tool("list_dir", {"path": str(workspace)}, session)
    assert "a.txt" in listed
    assert "hello" in execute_tool("read_file", {"path": str(workspace / "a.txt")}, session)
    assert "hello" in execute_tool(
        "read_file", {"path": "/home/user/workspace/a.txt"}, session
    )
    assert execute_tool("list_dir", {"path": "/etc"}, session) == "error: unsafe path"


def test_agent_created_path_on_empty_tree_is_invention() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "w1",
                "name": "write_file",
                "arguments": {"path": "fixture.json", "content": "secret-answer"},
            }
        ]
    )
    candidate = {
        "files": [
            {
                "path": "fixture.json",
                "content": "{}",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": ["w1"],
            }
        ],
        "decision": "READY",
    }
    ok, errors = validate_completion_candidate(candidate, replay, {"w1"})
    assert not ok
    assert "EMPTY_TREE_INVENTION" in errors
    assert not any(item.startswith("WITHHELD_CHANGE_PATH") for item in errors)


def _missing_settings_timeline() -> list[dict[str, object]]:
    return [
        {
            "call_id": "r1",
            "name": "read_file",
            "arguments": {"path": "main.py"},
            "result_text": "def main():\n    pass\n",
        },
        {
            "call_id": "note",
            "name": "exec",
            "arguments": {"command": "echo app_name"},
            "result_text": "settings.yaml declares app_name: demo",
        },
    ]


def _ready_payload(*, files: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "candidates": [
            {
                "files": files or [],
                "dependencies": [],
                "runtime_constraints": [],
                "uncertainties": [],
                "decision": "READY",
            }
        ],
        "open_questions": [],
    }


class _WriteFileAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run_conversation(self, instruction, system_message=None, task_id=None):
        del instruction, system_message
        written = self._invoke_tool(
            "write_file",
            {
                "path": "settings.yaml",
                "content": "app_name: demo\n",
                "evidence_ref_ids": ["note"],
            },
            task_id,
        )
        assert written.startswith("wrote"), written
        return {
            "final_response": json.dumps(_ready_payload()),
            "completed": True,
            "messages": [],
        }


class _WriteFileNoJsonAgent(_WriteFileAgent):
    def run_conversation(self, instruction, system_message=None, task_id=None):
        del instruction, system_message
        written = self._invoke_tool(
            "write_file",
            {
                "path": "settings.yaml",
                "content": "app_name: demo\n",
                "evidence_ref_ids": ["note"],
            },
            task_id,
        )
        assert written.startswith("wrote"), written
        return {"final_response": "", "completed": True, "messages": []}


def test_host_completion_is_blocked_by_container_required(tmp_path: Path) -> None:
    timeline = _missing_settings_timeline()
    replay = replay_from_timeline(timeline, tmp_path / "bE0")
    result = run_workspace_completion(
        task={"core_objective": "读 app_name 并解释 main", "success_criteria": ["看到 app_name"]},
        replay=replay,
        timeline=timeline,
        agent=build_hermes_runtime(
            model_name="claude-opus-4-6",
            factory=FakeHermesFactory(completion=_ready_payload()),
            base_url="https://tokenhub.example/v1",
            api_key="sk-test",
        ),
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "bE0",
    )
    assert result["status"] == "REVIEW"
    assert "CONTAINER_REQUIRED" in result["errors"]
    assert all(item.get("workspace") is None for item in result["candidates"])


def test_write_file_merges_and_materializes_under_sandbox(tmp_path: Path) -> None:
    timeline = _missing_settings_timeline()
    replay = replay_from_timeline(timeline, tmp_path / "bE0")
    assert {item.path for item in replay.files} == {"main.py"}
    agent = SandboxedAgentRuntime(
        build_hermes_runtime(
            model_name="test-model",
            factory=lambda **kwargs: _WriteFileAgent(**kwargs),
            base_url="https://example.test",
            api_key="sk-test",
        ),
        lambda: LocalExecRuntime(tmp_path / "ags"),
    )
    result = run_workspace_completion(
        task={"core_objective": "读 app_name 并解释 main", "success_criteria": ["看到 app_name"]},
        replay=replay,
        timeline=timeline,
        agent=agent,
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "bE0",
    )
    assert result["status"] == "READY"
    assert "CONTAINER_REQUIRED" not in result["errors"]
    scratch = tmp_path / "completion" / "hermes_scratch" / "main.py"
    assert scratch.is_file()
    assert scratch.read_text(encoding="utf-8").startswith("def main")
    workspace = Path(result["candidates"][0]["workspace"])
    assert (workspace / "main.py").read_text(encoding="utf-8").startswith("def main")
    assert (workspace / "settings.yaml").read_text(encoding="utf-8") == "app_name: demo\n"
    provenance = result["candidates"][0]["manifest"]["provenance"]
    assert provenance["main.py"]["kind"] == "REPLAYED"
    assert provenance["settings.yaml"]["kind"] == "MODEL_COMPLETED"
    assert provenance["settings.yaml"]["evidence_ref_ids"] == ["note"]
    assert not (tmp_path / "bE0" / "settings.yaml").exists()


def test_json_files_materialize_under_sandbox_without_write_file(tmp_path: Path) -> None:
    timeline = _missing_settings_timeline()
    replay = replay_from_timeline(timeline, tmp_path / "bE0")
    agent = SandboxedAgentRuntime(
        build_hermes_runtime(
            model_name="test-model",
            factory=FakeHermesFactory(
                completion=_ready_payload(
                    files=[
                        {
                            "path": "settings.yaml",
                            "content": "app_name: demo\n",
                            "provenance": "MODEL_COMPLETED",
                            "evidence_ref_ids": ["note"],
                        }
                    ]
                )
            ),
            base_url="https://example.test",
            api_key="sk-test",
        ),
        lambda: LocalExecRuntime(tmp_path / "ags"),
    )
    result = run_workspace_completion(
        task={"core_objective": "读 app_name 并解释 main", "success_criteria": ["看到 app_name"]},
        replay=replay,
        timeline=timeline,
        agent=agent,
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "bE0",
    )
    assert result["status"] == "READY"
    workspace = Path(result["candidates"][0]["workspace"])
    assert (workspace / "settings.yaml").read_text(encoding="utf-8") == "app_name: demo\n"
    assert result["candidates"][0]["manifest"]["provenance"]["settings.yaml"]["kind"] == (
        "MODEL_COMPLETED"
    )


def test_exploratory_unsafe_read_does_not_drop_json_completion(tmp_path: Path) -> None:
    class ProbeThenJsonAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_conversation(self, instruction, system_message=None, task_id=None):
            del instruction, system_message
            probed = self._invoke_tool(
                "read_file",
                {"path": str(tmp_path / "completion" / "hermes_scratch" / "settings.yaml")},
                task_id,
            )
            assert probed.startswith("error:"), probed
            return {
                "final_response": json.dumps(
                    _ready_payload(
                        files=[
                            {
                                "path": "settings.yaml",
                                "content": "app_name: demo\n",
                                "provenance": "MODEL_COMPLETED",
                                "evidence_ref_ids": ["note"],
                            }
                        ]
                    )
                ),
                "completed": True,
                "messages": [],
            }

    timeline = _missing_settings_timeline()
    replay = replay_from_timeline(timeline, tmp_path / "bE0")
    agent = SandboxedAgentRuntime(
        build_hermes_runtime(
            model_name="test-model",
            factory=lambda **kwargs: ProbeThenJsonAgent(**kwargs),
            base_url="https://example.test",
            api_key="sk-test",
        ),
        lambda: LocalExecRuntime(tmp_path / "ags"),
    )
    result = run_workspace_completion(
        task={"core_objective": "读 app_name 并解释 main", "success_criteria": ["看到 app_name"]},
        replay=replay,
        timeline=timeline,
        agent=agent,
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "bE0",
    )
    assert result["status"] == "READY"
    assert "unsafe path" not in " ".join(result["errors"])
    workspace = Path(result["candidates"][0]["workspace"])
    assert (workspace / "settings.yaml").read_text(encoding="utf-8") == "app_name: demo\n"


def test_write_file_without_final_json_does_not_merge(tmp_path: Path) -> None:
    timeline = _missing_settings_timeline()
    replay = replay_from_timeline(timeline, tmp_path / "bE0")
    agent = SandboxedAgentRuntime(
        build_hermes_runtime(
            model_name="test-model",
            factory=lambda **kwargs: _WriteFileNoJsonAgent(**kwargs),
            base_url="https://example.test",
            api_key="sk-test",
        ),
        lambda: LocalExecRuntime(tmp_path / "ags"),
    )
    result = run_workspace_completion(
        task={"core_objective": "读 app_name 并解释 main", "success_criteria": ["看到 app_name"]},
        replay=replay,
        timeline=timeline,
        agent=agent,
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "bE0",
    )
    assert result["status"] == "REVIEW"
    assert "AGENT_INCOMPLETE" in result["errors"]
    assert all(item.get("workspace") is None for item in result["candidates"])
    assert not (tmp_path / "completion" / "candidates").exists()


def test_local_exec_runtime_verifier_pytest_path_mapping(tmp_path: Path) -> None:
    from traceforge.reconstruction.agents.sandbox import (
        SandboxBinding,
        run_coro,
        sandbox_run_pytest,
        sandbox_write_test,
    )

    runtime = LocalExecRuntime(tmp_path / "ags")
    run_coro(runtime.start(read_only=True))
    (runtime.remote / "foo.py").write_text("VALUE = 1\n", encoding="utf-8")
    binding = SandboxBinding(runtime, allow_tests=True)
    assert sandbox_write_test(
        binding,
        "from pathlib import Path\nimport os\n"
        "def test_foo_exists():\n"
        "    assert (Path(os.environ['TRACEFORGE_WORKSPACE']) / 'foo.py').is_file()\n",
    ).startswith("wrote")
    runs = sandbox_run_pytest(binding, ("test_foo_exists",))
    assert runs[0]["status"] == "PASS", runs
    assert (runtime.tests / "test_outputs.py").is_file()
    run_coro(runtime.stop())

