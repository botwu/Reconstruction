"""跨阶段返修只针对独立复现的初态缺口，不能把 solver 失败改成通过。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from traceforge.reconstruction import eligible_reconstruction as pipeline
from traceforge.reconstruction.researcher import ReconstructionRuntime


def run_loop(tmp_path: Path, monkeypatch, *, diagnosis: str, repeat: bool = False,
             transport_failed: bool = False):
    task = {"task_id": "original", "core_objective": "修复用户指定的缺陷"}
    source = {"tool_timeline": [], "tasks": [task]}
    researcher = ReconstructionRuntime(Mock(model_name="test"), source=source, task=task,
                                       runtime_factory=Mock())
    candidate = {"workspace": str(tmp_path / "initial"), "env_root": str(tmp_path / "env")}
    failed = {"status": "REVIEW", "stopped_at": "verification", "workspace": candidate["workspace"],
              "selected_index": 0, "completion": {"candidates": [candidate]},
              "verification": {"status": "REVIEW", "errors": ["VERIFIER_CALIBRATION_FAILED"]}}
    if transport_failed:
        failed["verification"]["rollout"] = {
            "execution": {"status": "FAILED"}, "results": {"quality_gate": {"ok": False}}}
    runner = Mock(side_effect=None if repeat else [failed, {"status": "READY"}], return_value=failed)
    monkeypatch.setattr(pipeline, "_task_result", runner)
    gap = diagnosis != "SUFFICIENT"
    judge = {"label": "INSUFFICIENT" if gap else "SUFFICIENT", "status": "REVIEW" if gap else "READY",
             "missing_context": ["必要辅助函数损坏"] if gap else [],
             "environment_probes": [{"purpose": "load", "status": diagnosis, "code_sha256": "probe"}]}
    reviewer = Mock(return_value=judge)
    monkeypatch.setattr(pipeline, "run_workspace_sufficiency", reviewer)
    monkeypatch.setattr(pipeline, "completion_evidence_context", lambda *args: {"source": "original"})
    monkeypatch.setattr(pipeline, "build_environment_contract",
                        lambda **kwargs: {"status": "REVIEW" if gap else "READY", "workspace_sha256": "same"})
    result = pipeline._run_task_loop(
        task=task, source=source, root=tmp_path / "run", agent=researcher,
        verifier_agent=researcher, verification_config=None, verification_model=None,
        replay=SimpleNamespace(files=[]), support={}, task_source=source,
    )
    return result, runner, reviewer, task, candidate, researcher


def test_confirmed_initial_gap_returns_original_candidate_and_author(tmp_path, monkeypatch):
    result, runner, reviewer, task, candidate, author = run_loop(tmp_path, monkeypatch, diagnosis="FAIL")
    assert result["status"] == "READY"
    retry = runner.call_args_list[1].kwargs
    assert retry["completion_seed"] is candidate
    assert retry["agent"] is author and retry["task"] is task
    assert retry["completion_feedback"]["downstream_failure"]["stage"] == "verification"
    assert reviewer.call_args.kwargs["workspace_root"] == candidate["workspace"]
    assert runner.call_args_list[0].kwargs["root"] != retry["root"]
    assert result["task_repair_audit"][0]["status"] == "REVIEW"


@pytest.mark.parametrize("diagnosis", ["SUFFICIENT", "REJECTED"])
def test_solver_failure_or_invalid_probe_does_not_repair_initial_state(tmp_path, monkeypatch, diagnosis):
    result, runner, *_ = run_loop(tmp_path, monkeypatch, diagnosis=diagnosis)
    assert result["status"] == "REVIEW"
    assert runner.call_count == 1
    assert result["task_repair_audit"][-1]["stop_reason"] == "NO_CONFIRMED_INITIAL_ENVIRONMENT_GAP"


def test_transport_failure_keeps_receipt_without_source_repair(tmp_path, monkeypatch):
    result, runner, reviewer, *_ = run_loop(tmp_path, monkeypatch, diagnosis="FAIL", transport_failed=True)
    assert result["status"] == "REVIEW"
    assert runner.call_count == 1 and reviewer.call_count == 0
    assert result["task_repair_audit"][-1]["stop_reason"] == "DOWNSTREAM_EXECUTION_INCOMPLETE"


def test_repeated_candidate_and_diagnosis_stop_without_erasing_failure(tmp_path, monkeypatch):
    result, runner, *_ = run_loop(tmp_path, monkeypatch, diagnosis="FAIL", repeat=True)
    assert result["status"] == "REVIEW" and runner.call_count == 2
    assert result["task_repair_audit"][-1]["stop_reason"] == "NO_PROGRESS"
    assert (tmp_path / "run/tasks/original/task_repair_audit.json").is_file()
