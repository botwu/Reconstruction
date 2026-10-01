from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.reconstruction.agents.runtime import AgentResult
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

    def __init__(self, intermediate_review=False):
        self.calls: list[str] = []
        self.intermediate_review = intermediate_review

    def run(self, *, role, instruction, session, output_root):
        if role.result_schema == "traceforge.verifier-semantic-review.v1":
            revise = self.intermediate_review and len(self.calls) == 2
            return AgentResult(role=role.name, backend="fake", completed=True, payload={
                "decision": "REVISE" if revise else "ACCEPT",
                "issues": [{"problem": "还需修正测试替身"}] if revise else [],
                "obligation_reviews": [
                    {"obligation_id": "o", "covered": not revise, "reason": "反馈编排单测审查替身"}]})
        self.calls.append(instruction)
        payload = dict(PAYLOAD)
        if self.intermediate_review and len(self.calls) >= 3:
            payload["oracle_solutions"] = [
                {**item, "script": item["script"] + " adjusted"}
                for item in PAYLOAD["oracle_solutions"]
            ]
        return AgentResult(
            role=role.name,
            backend="fake",
            payload=payload,
            final_text=json.dumps(payload),
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

    def publish_calibrated_bundle(self, *, label, trials):
        del label, trials
        return {}


@pytest.mark.parametrize("intermediate_review", [False, True])
def test_agent_verifier_uses_feedback_rounds(monkeypatch, tmp_path: Path, intermediate_review):
    agent = _Agent(intermediate_review)
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
            max_rounds=3,
        ),
        source={"selected_span_has_file_ops": True},
    )
    assert result["status"] == "READY"
    assert executor.calls == 2
    assert len(agent.calls) == (3 if intermediate_review else 2)
    for instruction in agent.calls[1:]:
        feedback = json.loads(instruction.split("PREVIOUS_CALIBRATION_FEEDBACK:\n")[1])
        assert feedback["failed_cases"] == ["nop"]


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


def test_verifier_stage_resumes_with_saved_feedback(monkeypatch, tmp_path: Path):
    agent = _Agent()
    executor = _Executor(root=tmp_path)
    executor.calls = 1
    monkeypatch.setattr(verification_module, "HarborCalibrationExecutor", lambda **kwargs: executor)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("x", encoding="utf-8")
    feedback = {"failed_cases": ["oracle_returns_wrong_shape"]}
    result = run_reconstruction_verification(
        task={"task_instruction": "do x", "acceptance_obligations": [{"id": "o", "text": "output"}]},
        workspace_root=workspace, model=None, agent=agent,
        output_root=tmp_path / "resumed",
        config=VerificationConfig(
            harbor_root=tmp_path / "harbor", model_name="fake", rollout_model="anthropic/fake",
            execute_red=True, max_rounds=2,
        ),
        source={"selected_span_has_file_ops": True},
        initial_feedback=feedback, start_round=7,
    )
    assert result["status"] == "READY"
    assert [item["round"] for item in result["iterations"]] == [7]
    assert "CALIBRATION_ROUND: 7" in agent.calls[0]
    supplied = json.loads(agent.calls[0].split("PREVIOUS_CALIBRATION_FEEDBACK:\n")[1])
    assert supplied == feedback == {"failed_cases": ["oracle_returns_wrong_shape"]}
    assert (tmp_path / "resumed/agent/round-07/verifier.json").is_file()


@pytest.mark.parametrize("start_round", [0, -1, True])
def test_verifier_stage_rejects_invalid_resume_round(tmp_path: Path, start_round):
    with pytest.raises(ValueError, match="起始轮次"):
        run_reconstruction_verification(
            task={}, workspace_root=tmp_path, model=None, output_root=tmp_path / "output",
            config=VerificationConfig(
                harbor_root=tmp_path, model_name="fake", rollout_model="anthropic/fake",
            ),
            start_round=start_round,
        )
