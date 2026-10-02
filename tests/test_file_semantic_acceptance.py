"""真实工作区与文件语义回执的绑定，禁止 pytest 单独清除语义义务。"""

import hashlib
import json

import pytest

from traceforge.harbor_ags.response_acceptance import apply_file_semantic_receipts
from traceforge.harbor_ags.results import (
    HarborResultError,
    build_file_artifact_snapshot,
    validate_file_semantic_receipt,
)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def _trial(tmp_path):
    task = tmp_path / "task"
    initial = task / "workspace"
    initial.mkdir(parents=True)
    (initial / "main.py").write_text("def main(): return 1\n")
    checks = {"extract": "提取实际业务实现并由 main 消费"}
    contract = {
        "file_semantic_checks": checks,
        "acceptance_obligations": [{"id": "extract", "text": checks["extract"]}],
    }
    _write(task / "tests/control/input-manifest.json", {
        "schema_version": "traceforge.control-input-manifest.v1",
        "task_acceptance": contract,
        "workspace_sha256": {"main.py": hashlib.sha256((initial / "main.py").read_bytes()).hexdigest()},
    })
    (task / "instruction.md").write_text("提取业务实现。")
    (task / "task.toml").write_text('schema_version = "1.4"\n')
    (task / "tests/test_outputs.py").write_text("def test_output(): assert True\n")
    trial = tmp_path / "job/trial-1"
    _write(trial / "config.json", {"task": {"path": str(task)}})
    _write(trial / "result.json", {"exception_info": None, "finished_at": "2026-10-02"})
    _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
    final = trial / "artifacts/logs/artifacts/traceforge/workspace"
    final.mkdir(parents=True)
    (final / "main.py").write_text("from business import run\ndef main(): return run()\n")
    (final / "business.py").write_text("def run(): return 1\n")
    return trial, task, contract


def _receipt(snapshot):
    return {
        "schema_version": "traceforge.file-semantic-review.v1",
        "binding": snapshot["binding"],
        "status": "ACCEPT",
        "obligations": [{
            "obligation_id": "extract", "covered": True, "reason": "main 消费真实实现。",
            "evidence": [{"path": "final/main.py", "quote": "return run()"},
                         {"path": "final/business.py", "quote": "def run(): return 1"}],
        }],
    }


def _apply(trial, contract):
    result = {"status": "READY", "errors": [], "unverified_obligations": []}
    rollout = {"execution": {"status": "COMPLETED"}, "results": {"trials": [{
        "status": "PASS", "content_valid": True, "result_path": str(trial / "result.json"),
        "reward": 1.0,
    }]}}
    apply_file_semantic_receipts(result, rollout, contract, 1)
    assert rollout["results"]["trials"][0]["reward"] == 1.0
    return result


def test_file_semantic_receipt_accepts_exact_execution_and_workspaces(tmp_path):
    trial, task, contract = _trial(tmp_path)
    snapshot = build_file_artifact_snapshot(trial, expected_task=task)
    receipt = _receipt(snapshot)
    assert validate_file_semantic_receipt(receipt, snapshot, contract["file_semantic_checks"]) == []
    _write(trial / "verifier/file-semantic-review.json", receipt)
    result = _apply(trial, contract)
    assert result["status"] == "READY"
    assert result["unverified_obligations"] == []


def test_passing_pytest_cannot_replace_missing_semantic_receipt(tmp_path):
    trial, _, contract = _trial(tmp_path)
    result = _apply(trial, contract)
    assert result["status"] == "REVIEW"
    assert result["unverified_obligations"] == ["extract"]
    assert "FILE_SEMANTIC_UNVERIFIED" in result["errors"]


@pytest.mark.parametrize("relative", [
    "artifacts/logs/artifacts/traceforge/workspace/business.py", "result.json",
])
def test_changed_final_or_execution_invalidates_receipt(tmp_path, relative):
    trial, _, contract = _trial(tmp_path)
    _write(trial / "verifier/file-semantic-review.json", _receipt(build_file_artifact_snapshot(trial)))
    target = trial / relative
    target.write_text(target.read_text() + "\n")
    result = _apply(trial, contract)
    assert result["status"] == "REVIEW"
    assert any("BINDING_MISMATCH" in error for error in result["errors"])


@pytest.mark.parametrize("relative", [
    "tests/test_outputs.py", "instruction.md",
])
def test_changed_task_or_test_invalidates_receipt(tmp_path, relative):
    trial, task, contract = _trial(tmp_path)
    _write(trial / "verifier/file-semantic-review.json", _receipt(build_file_artifact_snapshot(trial)))
    target = task / relative
    target.write_text(target.read_text() + "\n")
    assert _apply(trial, contract)["status"] == "REVIEW"


def test_initial_workspace_must_equal_frozen_manifest(tmp_path):
    trial, task, _ = _trial(tmp_path)
    (task / "workspace/main.py").write_text("changed")
    with pytest.raises(HarborResultError, match="INITIAL_WORKSPACE"):
        build_file_artifact_snapshot(trial)


def test_snapshot_rejects_symlink_and_mismatched_expected_task(tmp_path):
    trial, task, _ = _trial(tmp_path)
    with pytest.raises(HarborResultError, match="TASK_MISMATCH"):
        build_file_artifact_snapshot(trial, expected_task=tmp_path / "other")
    (trial / "artifacts/logs/artifacts/traceforge/workspace/link").symlink_to(task / "workspace/main.py")
    with pytest.raises(HarborResultError, match="UNSAFE"):
        build_file_artifact_snapshot(trial)


@pytest.mark.parametrize("change", ["reject", "missing_id", "invented_quote", "traversal", "cross_trial"])
def test_rejected_incomplete_or_unbound_review_cannot_pass(tmp_path, change):
    trial, _, contract = _trial(tmp_path)
    receipt = _receipt(build_file_artifact_snapshot(trial))
    if change == "reject":
        receipt["status"] = "REVISE"
        receipt["obligations"][0]["covered"] = False
    elif change == "missing_id":
        receipt["obligations"] = []
    elif change == "invented_quote":
        receipt["obligations"][0]["evidence"][0]["quote"] = "not in actual file"
    elif change == "traversal":
        receipt["obligations"][0]["evidence"][0]["path"] = "final/../../outside"
    else:
        receipt["binding"]["trial_path"] = str(tmp_path / "job/other")
    _write(trial / "verifier/file-semantic-review.json", receipt)
    assert _apply(trial, contract)["status"] == "REVIEW"


def test_calibration_revise_is_valid_evidence_but_cannot_accept_solver(tmp_path):
    trial, _, contract = _trial(tmp_path)
    snapshot = build_file_artifact_snapshot(trial)
    receipt = _receipt(snapshot)
    receipt["status"] = "REVISE"
    receipt["obligations"][0]["covered"] = False
    checks = contract["file_semantic_checks"]
    assert validate_file_semantic_receipt(receipt, snapshot, checks, require_accepted=False) == []
    assert validate_file_semantic_receipt(receipt, snapshot, checks)
    receipt["obligations"][0]["covered"] = True
    assert "FILE_SEMANTIC_DECISION_INCONSISTENT" in validate_file_semantic_receipt(
        receipt, snapshot, checks, require_accepted=False,
    )


def test_initial_only_evidence_does_not_judge_final_implementation(tmp_path):
    trial, _, contract = _trial(tmp_path)
    snapshot = build_file_artifact_snapshot(trial)
    receipt = _receipt(snapshot)
    receipt["obligations"][0]["evidence"] = [{"path": "initial/main.py", "quote": "return 1"}]
    assert "FILE_SEMANTIC_FINAL_EVIDENCE_MISSING:extract" in validate_file_semantic_receipt(
        receipt, snapshot, contract["file_semantic_checks"],
    )


@pytest.mark.parametrize("name,valid", [("main.py", True), ("never-existed.py", False)])
def test_final_absence_must_be_actual_deletion_from_initial(tmp_path, name, valid):
    trial, _, contract = _trial(tmp_path)
    (trial / "artifacts/logs/artifacts/traceforge/workspace/main.py").unlink()
    snapshot = build_file_artifact_snapshot(trial)
    receipt = _receipt(snapshot)
    receipt["obligations"][0]["evidence"] = [{"path": "final/" + name, "absent": True}]
    errors = validate_file_semantic_receipt(receipt, snapshot, contract["file_semantic_checks"])
    assert bool(errors) is not valid


@pytest.mark.parametrize("agent_mode", ["nop", "oracle"])
def test_calibration_reader_requires_contracted_final_snapshot_without_changing_reward(
    tmp_path, monkeypatch, agent_mode,
):
    from traceforge.harbor_ags import results

    trial, _, _ = _trial(tmp_path)
    _write(trial / "result.json", {
        "finished_at": "2026-10-02", "verifier_result": {"rewards": {"task": 1.0}},
    })
    monkeypatch.setattr(results, "_cleanup_ok", lambda _: True)
    report = results.read_rollout_results(trial.parent, agent_mode=agent_mode)
    assert report["quality_gate"]["ok"]
    assert report["trials"][0]["artifact_snapshot"]["workspace"].endswith("traceforge/workspace")
    import shutil
    shutil.rmtree(trial / "artifacts/logs/artifacts/traceforge/workspace")
    report = results.read_rollout_results(trial.parent, agent_mode=agent_mode)
    assert report["trials"][0]["reward"] == 1.0
    assert report["trials"][0]["status"] == "PASS"
    assert not report["quality_gate"]["ok"]
    assert "FILE_SNAPSHOT_WORKSPACE_MISSING" in report["trials"][0]["content_errors"][0]


def test_reviewer_receives_bound_test_and_actual_oracle_text(tmp_path):
    trial, task, contract = _trial(tmp_path)
    (trial / "agent").mkdir()
    (trial / "agent/oracle.txt").write_text("actual solution stdout")
    (trial / "agent/exit-code.txt").write_text("0")
    snapshot = build_file_artifact_snapshot(trial)
    assert snapshot["evidence_files"]["verifier/test_outputs.py"] == str(task / "tests/test_outputs.py")
    assert snapshot["evidence_files"]["agent/oracle.txt"] == str(trial / "agent/oracle.txt")
    assert "config.json" not in snapshot["evidence_files"]
    assert "result.json" not in snapshot["evidence_files"]
    receipt = _receipt(snapshot)
    (trial / "agent/oracle.txt").write_text("changed execution")
    assert validate_file_semantic_receipt(
        receipt, build_file_artifact_snapshot(trial), contract["file_semantic_checks"],
    ) == ["FILE_SEMANTIC_BINDING_MISMATCH"]


def test_missing_snapshot_keeps_obligation_unverified(tmp_path):
    import shutil

    trial, _, contract = _trial(tmp_path)
    shutil.rmtree(trial / "artifacts/logs/artifacts/traceforge/workspace")
    result = _apply(trial, contract)
    assert result["status"] == "REVIEW"
    assert result["unverified_obligations"] == ["extract"]


def test_workspace_collection_manifest_is_visible_and_bound_when_present(tmp_path):
    trial, _, contract = _trial(tmp_path)
    legacy = build_file_artifact_snapshot(trial)
    assert "verifier/workspace-collection.json" not in legacy["evidence_files"]
    path = trial / "artifacts/logs/artifacts/traceforge/workspace-collection.json"
    collection = {
        "schema_version": "traceforge.workspace-collection.v1", "status": "COLLECTED", "errors": [],
        "files": [{"path": name, "sha256": value} for name, value in legacy["final_files"].items()],
        "excluded": [{"path": ".venv", "reason": "新增虚拟环境"}],
    }
    _write(path, collection)
    snapshot = build_file_artifact_snapshot(trial)
    assert snapshot["evidence_files"]["verifier/workspace-collection.json"] == str(path)
    assert snapshot["binding"]["execution_sha256"] != legacy["binding"]["execution_sha256"]
    receipt = _receipt(snapshot)
    _write(path, {**collection, "excluded": []})
    assert validate_file_semantic_receipt(
        receipt, build_file_artifact_snapshot(trial), contract["file_semantic_checks"],
    ) == ["FILE_SEMANTIC_BINDING_MISMATCH"]


@pytest.mark.parametrize("defect", ["missing", "error", "mismatch"])
def test_new_collection_hook_requires_complete_matching_receipt(tmp_path, defect):
    from traceforge.harbor_task import workspace_snapshot_hook

    trial, task, _ = _trial(tmp_path)
    (task / "task.toml").write_text('schema_version = "1.4"\n' + workspace_snapshot_hook(task))
    final = trial / "artifacts/logs/artifacts/traceforge/workspace"
    receipt = {
        "schema_version": "traceforge.workspace-collection.v1", "status": "COLLECTED", "errors": [],
        "files": [{"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in final.iterdir()],
    }
    manifest = final.parent / "workspace-collection.json"
    _write(manifest, receipt)
    assert build_file_artifact_snapshot(trial)["final_files"]
    if defect == "missing":
        manifest.unlink()
    elif defect == "error":
        receipt.update(status="ERROR", errors=["收集失败，目录可能来自上一次运行"])
        _write(manifest, receipt)
    else:
        (final / "business.py").write_text("未记入收集清单的内容")
    with pytest.raises(HarborResultError, match="COLLECTION"):
        build_file_artifact_snapshot(trial)
