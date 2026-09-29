from __future__ import annotations

import tempfile
from pathlib import Path

from hermes_fakes import FakeHermesAgent, FakeHermesFactory

from traceforge.reconstruction.agents import SandboxedAgentRuntime, build_hermes_runtime
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.sandbox import LocalExecRuntime
from traceforge.reconstruction.terminal_universe_environment import ReplayedFile, ReplayResult
from traceforge.reconstruction.workspace_completion import run_workspace_completion
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency


def _agent(root: Path, *, completion: dict | None = None) -> SandboxedAgentRuntime:
    factory = FakeHermesFactory(completion=completion)
    return SandboxedAgentRuntime(
        build_hermes_runtime(
            model_name="test-model",
            factory=factory,
            base_url="https://example.test",
            api_key="sk-test",
        ),
        lambda: LocalExecRuntime(root / ("runtime-" + next(tempfile._get_candidate_names()))),
    )


def test_sufficiency_uses_input_inventory_after_sandbox_cleanup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "observed.txt").write_text("observed\n", encoding="utf-8")
    class InspectingAgent(FakeHermesAgent):
        def run_conversation(self, instruction, system_message=None, task_id=None):
            if task_id == "sufficiency":
                self._invoke_tool("list_dir", {"path": "."}, task_id)
                self._invoke_tool("read_file", {"path": "observed.txt"}, task_id)
            return super().run_conversation(instruction, system_message, task_id)

    class InspectingFactory(FakeHermesFactory):
        def __call__(self, **kwargs):
            self.last_kwargs = kwargs
            return InspectingAgent(intent_ok=self.intent_ok, completion=self.completion, **kwargs)

    inspecting = SandboxedAgentRuntime(
        build_hermes_runtime(
            model_name="test-model", factory=InspectingFactory(), base_url="https://example.test", api_key="sk-test"
        ),
        lambda: LocalExecRuntime(tmp_path / ("runtime-" + next(tempfile._get_candidate_names()))),
    )
    result = run_workspace_sufficiency(
        task={"core_objective": "inspect the observed file"},
        workspace_root=workspace,
        agent=inspecting,
        output_root=tmp_path / "sufficiency",
    )
    assert result["status"] == "READY"
    assert result["file_count"] == 1
    assert result["read_only_probe"]["write_denied"] is True
    assert result["sandbox_cleanup_confirmed"] is True


def test_completion_review_retains_payload_diagnostics(tmp_path: Path) -> None:
    class FailedRuntime:
        backend = "hermes-sandbox"
        model_name = "test-model"

        def run(self, **_: object) -> AgentResult:
            return AgentResult(
                role="completion",
                backend=self.backend,
                payload={
                    "open_questions": ["missing dependency"],
                    "candidates": [
                        {
                            "files": [],
                            "dependencies": [],
                            "runtime_constraints": [],
                            "uncertainties": [],
                            "decision": "REVIEW",
                        }
                    ],
                },
                errors=["AGENT_TIMEOUT"],
                final_text="{}",
                completed=False,
            )

    result = run_workspace_completion(
        task={"core_objective": "inspect"},
        replay=ReplayResult((ReplayedFile("a.txt", "a", "ev", "PARTIAL"),), (), (), ()),
        timeline=[],
        agent=FailedRuntime(),
        output_root=tmp_path / "completion",
    )
    assert result["status"] == "REVIEW"
    assert "AGENT_TIMEOUT" in result["errors"]
    assert result["open_questions"] == ["missing dependency"]
    assert result["candidates"][0]["decision"] == "REVIEW"


def test_agent_can_revise_its_write_without_losing_original_evidence(tmp_path: Path) -> None:
    from traceforge.reconstruction.agents.session import (
        AgentSession,
        collect_workspace_writes,
        execute_tool,
    )

    session = AgentSession(
        workspace=tmp_path / "workspace",
        evidence=[{"evidence_ref_id": "ev", "name": "evidence"}],
        partial_files={"context.txt": "original excerpt"},
        allow_write=True,
    )
    session.workspace.mkdir()
    args = {"path": "context.txt", "content": "original excerpt\nfirst completion",
            "evidence_ref_ids": ["ev"]}
    assert execute_tool("write_file", args, session) == "wrote context.txt"
    revised = {**args, "content": "original excerpt\nrevised completion"}
    assert execute_tool("write_file", revised, session) == "wrote context.txt"
    assert len(session.writes) == 2
    assert collect_workspace_writes(session)[0]["content"] == revised["content"]
    assert (session.workspace / "context.txt").read_text() == revised["content"]
    assert "PARTIAL_OBSERVED_CONTENT_LOST" in execute_tool(
        "write_file", {**revised, "content": "discarded original"}, session,
    )
    assert len(session.writes) == 2
    assert session.partial_files["context.txt"] == "original excerpt"
