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


@pytest.mark.parametrize("intermediate_review", [False, True])
def test_baseline_facts_reach_independent_review_and_survive_calibration(
    monkeypatch, tmp_path: Path, intermediate_review,
):
    from traceforge.reconstruction.agents.session import execute_tool

    observations = [{
        "finding": "该异常在任务前已存在，是否应修复须按原始目标判断",
        "source_message_indices": [1],
        "source_excerpt": "raise RuntimeError('old failure')",
        "initial_probe": {"status": "FAIL", "stderr": "RuntimeError: old failure"},
    }]
    raw = {"messages": [
        {"role": "user", "content": "仅抽取读取接口并分析现有问题"},
        {"role": "tool", "content": observations[0]["source_excerpt"]},
    ]}

    class ObservingAgent(_Agent):
        def __init__(self):
            super().__init__(intermediate_review)
            self.reviews = []

        def run(self, **kwargs):
            if kwargs["role"].result_schema == "traceforge.verifier-semantic-review.v1":
                spec = json.loads(kwargs["instruction"].splitlines()[-1])
                self.reviews.append(spec)
                assert spec["baseline_observations"] == observations
                assert "read_session_message" in kwargs["role"].tools
                message = json.loads(execute_tool("read_session_message", {"index": 1}, kwargs["session"]))
                assert message == raw["messages"][1]
            return super().run(**kwargs)

    agent = ObservingAgent()
    executor = _Executor(root=tmp_path)
    monkeypatch.setattr(verification_module, "HarborCalibrationExecutor", lambda **kw: executor)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("x")
    feedback = {"baseline_observations": observations}
    result = run_reconstruction_verification(
        task={"task_instruction": "do x", "acceptance_obligations": [{"id": "o", "text": "output"}]},
        workspace_root=workspace, model=None, agent=agent, output_root=tmp_path / "verification",
        config=VerificationConfig(
            harbor_root=tmp_path / "harbor", model_name="fake", rollout_model="anthropic/fake",
            execute_red=True, max_rounds=3,
        ),
        source={"selected_span_has_file_ops": True, "raw_session": raw},
        initial_feedback=feedback,
    )
    assert result["status"] == "READY"
    assert len(agent.reviews) == (3 if intermediate_review else 2)
    for instruction in agent.calls:
        supplied = json.loads(instruction.split("PREVIOUS_CALIBRATION_FEEDBACK:\n")[1])
        assert supplied["baseline_observations"] == observations
    checkpoints = sorted((tmp_path / "verification/agent").glob("round-*/verifier.json"))
    for path in checkpoints:
        checkpoint = json.loads(path.read_text())
        assert checkpoint["feedback"]["baseline_observations"] == observations
        assert checkpoint["semantic_review"]["baseline_observations"] == observations
    if intermediate_review:
        assert json.loads(checkpoints[1].read_text())["status"] == "REVIEW"
    assert feedback == {"baseline_observations": observations}


@pytest.mark.parametrize("changed", ["facts", "prompt_version"])
def test_new_baseline_or_prompt_rechecks_unchanged_candidate(tmp_path: Path, changed):
    from traceforge.reconstruction.verifier_recovery import run_verifier_recovery

    class RejectingAgent(_Agent):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            if kwargs["role"].result_schema == "traceforge.verifier-semantic-review.v1":
                result.payload.update(decision="REVISE", issues=[{"problem": "缺少初态事实"}])
                result.payload["obligation_reviews"][0]["covered"] = False
            return result

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("x")
    args = {"task": {"task_instruction": "do x", "acceptance_obligations": [{"id": "o", "text": "output"}]},
            "workspace_root": workspace}
    first, _ = run_verifier_recovery(
        **args, agent=RejectingAgent(), output_root=tmp_path / "first",
        feedback={"baseline_observations": [{"finding": "旧事实"}]},
    )
    feedback = first["feedback"]
    if changed == "facts":
        feedback["baseline_observations"] = [{"finding": "新增来源证据"}]
    else:
        feedback["semantic_review"]["prompt_version"] = "old-review"
    result, candidate = run_verifier_recovery(
        **args, agent=_Agent(), output_root=tmp_path / "second", feedback=feedback,
    )
    assert candidate is not None
    assert result["semantic_review"]["status"] == "ACCEPT"
    assert not result["semantic_review"].get("reused_rejection")


@pytest.mark.parametrize("observations", ["claim", {"finding": "claim"}, ["claim"]])
def test_invalid_baseline_observations_are_rejected(tmp_path: Path, observations):
    from traceforge.reconstruction.verifier_recovery import run_verifier_recovery

    with pytest.raises(ValueError, match="baseline_observations"):
        run_verifier_recovery(
            task={"acceptance_obligations": [{"id": "o", "text": "output"}]},
            workspace_root=tmp_path, agent=_Agent(), output_root=tmp_path / "out",
            feedback={"baseline_observations": observations},
        )
