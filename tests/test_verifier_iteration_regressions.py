from __future__ import annotations

import json
from pathlib import Path

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.model_gateway import ModelRequest
from traceforge.reconstruction import verification as verification_module
from traceforge.reconstruction.verification import VerificationConfig, run_reconstruction_verification


PAYLOAD = {
    "status": "READY",
    "test_outputs_py": "def test_missing():\n    assert False\ndef test_protective():\n    assert True\ndef test_output():\n    assert True\n",
    "oracle_solutions": [
        {"name": "a", "script": "echo a", "justification": "a"},
        {"name": "b", "script": "echo b", "justification": "b"},
    ],
    "mutation_solutions": [{"name": "m", "script": "echo m", "justification": "m"}],
    "missing_capability_tests": ["test_missing"],
    "protective_tests": ["test_protective"],
    "obligation_coverage": {"o": ["test_output"]},
    "expected_value_strategy": "independent",
    "open_questions": [],
}


class _Agent:
    model_name = "fake"

    def __init__(self):
        self.calls: list[str] = []

    def run(self, *, role, instruction, session, output_root):
        self.calls.append(instruction)
        return AgentResult(
            role=role.name,
            backend="fake",
            payload=dict(PAYLOAD),
            final_text=json.dumps(PAYLOAD),
            completed=True,
        )


class _Executor:
    def __init__(self, **kwargs):
        self.bundle = Path(kwargs["root"]) / "bundle" / "task"
        self.attempts = []
        self.calls = 0

    def run(self, candidate):
        self.calls += 1
        if self.calls == 1:
            return {"status": "FAIL", "feedback": json.dumps({"failed_cases": ["nop"]})}
        return {"status": "PASS", "feedback": "{}"}


def test_agent_verifier_uses_feedback_rounds(monkeypatch, tmp_path: Path):
    agent = _Agent()
    executor = _Executor(root=tmp_path)
    monkeypatch.setattr(verification_module, "HarborCalibrationExecutor", lambda **kwargs: executor)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("x", encoding="utf-8")
    result = run_reconstruction_verification(
        task={
            "task_instruction": "do x",
            "acceptance_obligations": [{"id": "o", "text": "output"}],
        },
        workspace_root=workspace,
        model=None,
        agent=agent,
        output_root=tmp_path / "verification",
        config=VerificationConfig(
            harbor_root=tmp_path / "harbor",
            model_name="fake",
            rollout_model="anthropic/fake",
            execute_red=True,
            max_rounds=2,
        ),
        source={"selected_span_has_file_ops": True},
    )
    assert result["status"] == "READY"
    assert executor.calls == 2
    assert len(agent.calls) == 2
    assert "failed_cases" in agent.calls[1]


def test_verifier_recovery_does_not_return_candidate_with_errors(tmp_path: Path):
    from traceforge.reconstruction.verifier_recovery import run_verifier_recovery

    class Broken(_Agent):
        def run(self, *, role, instruction, session, output_root):
            result = super().run(role=role, instruction=instruction, session=session, output_root=output_root)
            result.errors.append("AGENT_POLICY_ERROR")
            return result

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("x", encoding="utf-8")
    report, candidate = run_verifier_recovery(
        task={"acceptance_obligations": [{"id": "o", "text": "output"}]},
        workspace_root=workspace,
        agent=Broken(),
        output_root=tmp_path / "agent",
    )
    assert candidate is None
    assert report["status"] == "REVIEW"
    assert "AGENT_POLICY_ERROR" in report["errors"]
