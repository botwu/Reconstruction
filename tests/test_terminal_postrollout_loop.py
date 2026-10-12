"""未评分 terminal 的实跑后审只把已确认初态缺口交回原作者。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from traceforge.reconstruction import pipeline
from traceforge.reconstruction.researcher import ReconstructionRuntime


def _judge(status: str = "SOLVER_ERROR", *, gap_confirmed: bool = False) -> dict:
    probe = {"purpose": "load", "status": "FAIL" if gap_confirmed else "PASS",
             "code_sha256": "same-probe"}
    return {
        "label": "INSUFFICIENT" if gap_confirmed else "SUFFICIENT",
        "status": "REVIEW" if gap_confirmed else "READY",
        "errors": [], "environment_probes": [probe],
        "missing_context": ["原始入口依赖缺失"] if gap_confirmed else [],
        "rollout_review": {
            "status": "COMPLETE", "errors": [], "acceptance": "NOT_ASSESSED",
            "sft_eligible": False,
            "requirements": [
                {"trial": trial, "obligation_id": "behavior", "status": status,
                 "reason": "核对实际源码与调用返回", "evidence_refs": [f"{trial}/answer"]}
                for trial in ("trial-1", "trial-2")
            ],
        },
    }


@pytest.fixture
def loop_case(tmp_path: Path, monkeypatch):
    task = {"task_id": "original", "core_objective": "修复指定缺陷并分析原因",
            "acceptance_obligations": [{"id": "behavior"}]}
    source = {"tool_timeline": [], "tasks": [task]}
    author = ReconstructionRuntime(Mock(model_name="fixture"), source=source, task=task,
                                   runtime_factory=Mock())
    initial = tmp_path / "initial"
    initial.mkdir()
    (initial / "main.py").write_text("original = True\n", encoding="utf-8")
    candidate = {"workspace": str(initial)}
    evidence = {
        "trials": [
            {"trial": trial, "answer": "原答卷", "tool_events": [{"result": "完整工具返回"}]}
            for trial in ("trial-1", "trial-2")
        ],
        "evidence_refs": ["trial-1/answer", "trial-2/answer"],
    }
    case = SimpleNamespace(task=task, source=source, author=author, candidate=candidate,
                           evidence=evidence, root=tmp_path / "run",
                           result_status="ROLLOUT_COMPLETED", evidence_mode="valid",
                           executed_task=task)

    def task_result(**kwargs):
        directory = kwargs["root"] / "tasks" / task["task_id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "rollout-evidence.json"
        if case.evidence_mode == "outside":
            path = tmp_path / "unrelated-evidence.json"
        path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        if case.evidence_mode == "missing":
            path.unlink()
        return {
            "status": case.result_status,
            "stopped_at": None if case.result_status == "ROLLOUT_COMPLETED" else "rollout",
            "workspace": candidate["workspace"], "selected_index": 0,
            "completion": {"candidates": [candidate]}, "errors": [],
            "verification": {"status": "NOT_ASSESSED", "unverified_obligations": ["behavior"]},
            "acceptance": "NOT_ASSESSED", "sft_eligible": False,
            "rollout_evidence_path": str(path), "executed_task": case.executed_task,
        }

    case.runner = Mock(side_effect=task_result)
    case.reviewer = Mock(return_value=_judge())
    monkeypatch.setattr(pipeline, "_task_result", case.runner)
    monkeypatch.setattr(pipeline, "run_workspace_sufficiency", case.reviewer)
    monkeypatch.setattr(pipeline, "completion_evidence_context",
                        lambda *args: {"origin": "原始轨迹"})

    def environment(**kwargs):
        judge = kwargs["sufficiency"]
        return {
            "status": judge["status"], "workspace_sha256": "same-initial",
            "execution_readiness": "PROBED", "execution_errors": [],
            "probes": judge["environment_probes"],
        }

    monkeypatch.setattr(pipeline, "build_environment_contract", environment)

    def run(**resume):
        return pipeline._run_task_loop(
            task=task, source=source, root=case.root, agent=case.author,
            verification_model=None,
            verification_config=SimpleNamespace(
                disable_verification=True, execute_rollout=True, rollout_trials=2),
            replay=SimpleNamespace(files=[]), support={}, task_source=source, **resume,
        )

    case.run = run
    return case


def test_native_completion_reviews_original_candidate_without_repairing_solver(loop_case):
    case = loop_case
    original_task = copy.deepcopy(case.task)
    result = case.run()
    assert result["status"] == "ROLLOUT_COMPLETED"
    assert result["acceptance"] == "NOT_ASSESSED" and result["sft_eligible"] is False
    assert case.runner.call_count == case.reviewer.call_count == 1
    reviewed = case.reviewer.call_args.kwargs
    assert reviewed["workspace_root"] == case.candidate["workspace"]
    assert reviewed["agent"] is case.author
    assert reviewed["rollout_evidence"] == case.evidence
    feedback_path = reviewed["repair_feedback"]["downstream_failure"]["evidence_path"]
    assert json.loads(Path(feedback_path).read_text()) == case.evidence
    assert case.task == original_task
    assert Path(case.candidate["workspace"], "main.py").read_text() == "original = True\n"
    saved = json.loads((case.root / "tasks/original/rollout-review.json").read_text())
    assert saved == result["rollout_review"]
    assert {row["status"] for row in saved["requirements"]} == {"SOLVER_ERROR"}


@pytest.mark.parametrize("failure", ["missing_review", "missing_requirement", "agent_failure"])
def test_unfinished_review_cannot_be_reported_as_completed(loop_case, failure):
    judge = _judge()
    if failure == "missing_review":
        judge.pop("rollout_review")
    else:
        review = judge["rollout_review"]
        review["status"] = "REVIEW_INCOMPLETE"
        review["errors"] = [
            "ROLLOUT_REVIEW_COVERAGE_INCOMPLETE" if failure == "missing_requirement"
            else "AGENT_INCOMPLETE"
        ]
        if failure == "missing_requirement":
            review["requirements"].pop()
    loop_case.reviewer.return_value = judge
    result = loop_case.run()
    assert result["status"] == "REVIEW" and result["stopped_at"] == "rollout_review"
    assert result["errors"]
    assert result["acceptance"] == "NOT_ASSESSED" and result["sft_eligible"] is False
    assert loop_case.runner.call_count == 1


def test_confirmed_gap_reuses_original_author_and_preserves_attempt(loop_case):
    case = loop_case
    case.reviewer.side_effect = [
        _judge("ENVIRONMENT_GAP", gap_confirmed=True),
        _judge("ENVIRONMENT_GAP", gap_confirmed=True), _judge("SUPPORTED"),
    ]
    result = case.run()
    assert result["status"] == "ROLLOUT_COMPLETED"
    assert case.runner.call_count == 2 and case.reviewer.call_count == 3
    original, retry = (call.kwargs for call in case.runner.call_args_list)
    assert retry["root"] == case.root / "attempts/001"
    assert original["root"] == case.root
    assert retry["completion_seed"] is case.candidate
    assert retry["agent"] is case.author and retry["task"] is case.task
    feedback = retry["completion_feedback"]
    assert feedback["failed_probes"][0]["status"] == "FAIL"
    assert "downstream_failure" not in feedback
    assert json.loads((case.root / "tasks/original/rollout-evidence.json").read_text()) == case.evidence
    confirmation = case.reviewer.call_args_list[1].kwargs
    assert confirmation["workspace_root"] == case.candidate["workspace"]
    assert "rollout_evidence" not in confirmation
    assert set(confirmation["repair_feedback"]) == {"suspected_gaps", "instruction"}
    assert confirmation["output_root"].name == "initial-confirmation"
    assert (case.root / "tasks/original/rollout-evidence.json").is_file()
    assert (retry["root"] / "tasks/original/rollout-evidence.json").is_file()
    assert result["task_repair_audit"][0]["action"] == "REPAIR_INITIAL_ENVIRONMENT"
    assert result["acceptance"] == "NOT_ASSESSED" and result["sft_eligible"] is False


def test_same_gap_and_initial_workspace_stop_without_endless_repair(loop_case):
    loop_case.reviewer.return_value = _judge("ENVIRONMENT_GAP", gap_confirmed=True)
    result = loop_case.run()
    assert result["status"] == "REVIEW" and loop_case.runner.call_count == 2
    assert result["task_repair_audit"][-1]["stop_reason"] == "NO_PROGRESS"
    assert result["errors"] == ["ROLLOUT_ENVIRONMENT_GAP"]


def test_unconfirmed_gap_stays_review_and_never_rewrites_initial_state(loop_case):
    loop_case.reviewer.return_value = _judge("ENVIRONMENT_GAP")
    result = loop_case.run()
    assert result["status"] == "REVIEW"
    assert loop_case.runner.call_count == 1
    assert result["task_repair_audit"][-1]["stop_reason"] == "NO_CONFIRMED_INITIAL_ENVIRONMENT_GAP"


def test_native_execution_failure_does_not_trigger_semantic_diagnosis(loop_case):
    loop_case.result_status = "ROLLOUT_INCOMPLETE"
    result = loop_case.run()
    assert result["status"] == "ROLLOUT_INCOMPLETE"
    assert loop_case.runner.call_count == 1
    loop_case.reviewer.assert_not_called()


@pytest.mark.parametrize("mode", ["missing", "outside"])
def test_missing_or_unrelated_actual_evidence_cannot_trigger_review(loop_case, mode):
    loop_case.evidence_mode = mode
    result = loop_case.run()
    assert result["status"] == "REVIEW"
    assert result["errors"][0] == "ROLLOUT_EVIDENCE_UNAVAILABLE"
    assert loop_case.runner.call_count == 1
    loop_case.reviewer.assert_not_called()


def test_missing_independent_runtime_does_not_silently_skip_postrollout_review(loop_case):
    loop_case.author = Mock()
    result = loop_case.run()
    assert result["status"] == "REVIEW"
    assert result["errors"] == ["ROLLOUT_REVIEW_RUNTIME_REQUIRED"]
    loop_case.reviewer.assert_not_called()


@pytest.mark.parametrize("probe_purpose", ["rollout", "load"])
def test_final_failure_or_wrong_probe_tag_needs_independent_initial_confirmation(
    loop_case, probe_purpose,
):
    case = loop_case
    final_judge = _judge("ENVIRONMENT_GAP", gap_confirmed=True)
    final_judge["environment_probes"][0].update(
        purpose=probe_purpose, python_code="raise AssertionError('solver implementation is wrong')",
    )
    initial_judge = _judge()
    initial_judge.pop("rollout_review")
    case.reviewer.side_effect = [final_judge, initial_judge]
    result = case.run()
    assert result["status"] == "REVIEW"
    assert case.runner.call_count == 1 and case.reviewer.call_count == 2
    assert result["task_repair_audit"][-1]["stop_reason"] == "NO_CONFIRMED_INITIAL_ENVIRONMENT_GAP"
    confirmation = case.reviewer.call_args_list[1].kwargs
    assert "rollout_evidence" not in confirmation
    assert "solver implementation is wrong" not in json.dumps(confirmation["repair_feedback"])
    assert confirmation["workspace_root"] == case.candidate["workspace"]
    assert result["rollout_review"]["initial_confirmation_path"].endswith(
        "/initial-confirmation/sufficiency.json",
    )


def test_variant_review_uses_actual_executed_task_without_rewriting_original(loop_case):
    case = loop_case
    original = copy.deepcopy(case.task)
    executed = {**case.task, "core_objective": "已接受变体的实际目标",
                "acceptance_obligations": [{"id": "actual-obligation"}]}
    case.executed_task = executed
    result = case.run()
    assert case.reviewer.call_args.kwargs["task"] is executed
    assert case.runner.call_args.kwargs["task"] is case.task
    assert case.task == original
    assert result["executed_task"] is executed
    assert result["acceptance"] == "NOT_ASSESSED"


@pytest.fixture
def resumed_case(loop_case, monkeypatch):
    case = loop_case
    initial = case.runner(root=case.root)
    case.runner.reset_mock()
    task_root = case.root / "tasks" / case.task["task_id"]
    receipt = task_root / "native-rollout.json"
    plan = task_root / "native-rollout/plans/current"
    receipt.write_text(json.dumps({"plan": str(plan), "errors": [],
                                   "execution": {"status": "COMPLETED"}}))
    initial.update(task_id=case.task["task_id"], source_task=case.task,
                   rollout_backend="native_harbor", rollout_record=str(receipt))
    case.native = Mock(return_value={
        "execution_completed": True, "quality_gate": {"ok": True},
        "acceptance": {"status": "NOT_ASSESSED", "errors": []},
        "trials": [{"native_trial": {"trial": name, "completed": True}}
                   for name in ("trial-1", "trial-2")],
    })
    monkeypatch.setattr(pipeline, "read_rollout_acceptance", case.native, raising=False)
    monkeypatch.setattr(pipeline, "build_terminal_rollout_evidence", lambda trials: case.evidence)
    case.initial, case.receipt = initial, receipt
    return case


def test_resume_native_result_skips_completed_reconstruction_and_reviews(resumed_case):
    case = resumed_case
    before = copy.deepcopy(case.initial)
    result = case.run(initial_result=case.initial)
    case.runner.assert_not_called()
    case.native.assert_called_once()
    assert case.reviewer.call_count == 1
    assert result["status"] == "ROLLOUT_COMPLETED"
    assert result["acceptance"] == "NOT_ASSESSED" and result["sft_eligible"] is False
    assert case.initial == before


def test_resumed_native_gap_rejoins_existing_initial_confirmation_and_repair(resumed_case):
    case = resumed_case
    case.reviewer.side_effect = [
        _judge("ENVIRONMENT_GAP", gap_confirmed=True),
        _judge("ENVIRONMENT_GAP", gap_confirmed=True), _judge("SUPPORTED"),
    ]
    result = case.run(initial_result=case.initial)
    assert result["status"] == "ROLLOUT_COMPLETED"
    assert case.runner.call_count == 1 and case.reviewer.call_count == 3
    retry = case.runner.call_args.kwargs
    assert retry["root"] == case.root / "attempts/001"
    assert retry["completion_seed"] == case.candidate
    assert retry["agent"] is case.author and retry["task"] is case.task
    assert result["task_repair_audit"][0]["action"] == "REPAIR_INITIAL_ENVIRONMENT"


@pytest.mark.parametrize("failure", ["task", "incomplete", "missing_receipt", "native_rejected"])
def test_resume_rejects_unrelated_or_unproven_native_result(resumed_case, failure):
    case = resumed_case
    if failure == "task":
        case.initial["source_task"] = {**case.task, "core_objective": "different task"}
    elif failure == "incomplete":
        case.initial["status"] = "ROLLOUT_INCOMPLETE"
    elif failure == "missing_receipt":
        case.receipt.unlink()
    else:
        case.native.return_value["acceptance"]["errors"] = ["NATIVE_TRACE_INCOMPLETE"]
    with pytest.raises(pipeline.ReconstructionError, match="恢复"):
        case.run(initial_result=case.initial)
    case.runner.assert_not_called()
    case.reviewer.assert_not_called()


def test_confirmed_initial_gap_reaches_actual_author_prompt_without_solver_output(loop_case):
    from traceforge.reconstruction.agents import AgentSession, COMPLETION_REPLAYED_ROLE
    from traceforge.reconstruction.agents.runtime import AgentResult

    case = loop_case
    case.evidence["trials"][0].update(answer="SOLVER_ANSWER_DO_NOT_COPY",
                                    final_artifacts={"main.py": "SOLVER_FINAL_IMPLEMENTATION"})
    final_judge = _judge("ENVIRONMENT_GAP", gap_confirmed=True)
    final_judge["rollout_review"]["requirements"][0]["reason"] = "SOLVER_GRADING_DO_NOT_COPY"
    initial_judge = _judge("ENVIRONMENT_GAP", gap_confirmed=True)
    initial_judge["reason"] = "INITIAL_GAP_FROM_ORIGINAL_SOURCE"
    initial_judge["environment_probes"][0].update(
        probe_id="initial-confirmed", python_code="import required_original_module",
        executions=[{"stderr": "ORIGINAL_IMPORT_ERROR", "exit_code": 1}],
    )
    case.reviewer.side_effect = [final_judge, initial_judge, _judge("SUPPORTED")]
    case.run(completion_feedback={"baseline_observations": {"observed": "ORIGINAL_BASELINE"}})
    feedback = case.runner.call_args.kwargs["completion_feedback"]
    native = Mock(model_name="offline", backend="test")
    native.run.return_value = AgentResult(role="completion", backend="test", completed=True, payload={})
    case.author.agent = native
    author_root = case.root / "captured-author"
    case.author.run(role=COMPLETION_REPLAYED_ROLE, instruction="", output_root=author_root,
                    session=AgentSession(workspace=Path(case.candidate["workspace"]),
                                         repair_feedback=feedback))
    instruction = native.run.call_args.kwargs["instruction"]
    rendered = (author_root / "task/instruction.md").read_text()
    for text in (instruction, rendered):
        assert "INITIAL_GAP_FROM_ORIGINAL_SOURCE" in text
        assert "import required_original_module" in text and "ORIGINAL_IMPORT_ERROR" in text
        assert "ORIGINAL_BASELINE" in text
        assert "SOLVER_ANSWER_DO_NOT_COPY" not in text
        assert "SOLVER_FINAL_IMPLEMENTATION" not in text
        assert "SOLVER_GRADING_DO_NOT_COPY" not in text
        assert "downstream_failure" not in text
    saved = (case.root / "tasks/original/rollout-evidence.json").read_text()
    assert "SOLVER_ANSWER_DO_NOT_COPY" in saved and "SOLVER_FINAL_IMPLEMENTATION" in saved
    assert "SOLVER_GRADING_DO_NOT_COPY" in (
        case.root / "tasks/original/rollout-review.json").read_text()
