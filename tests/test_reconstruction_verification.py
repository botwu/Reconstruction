import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.harbor_ags import response_acceptance
from traceforge.harbor_ags.response_acceptance import (
    _acceptance_report_obligation_ids,
    apply_response_receipts,
)
from traceforge.reconstruction import verification
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse
import pytest

from traceforge.reconstruction.verification import (
    HarborCalibrationExecutor,
    VerificationConfig,
    run_reconstruction_verification,
    verifier_task,
    write_execution_manifest,
    _record_unverified_obligations,
)
from traceforge.reconstruction.verifier_recovery import run_verifier_recovery

def test_verification_rollout_budget_cannot_be_lower_than_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'roles:\n  {"rollout":{"timeout_seconds":14400,"max_iterations":500}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot be lower"):
        VerificationConfig(
            harbor_root=tmp_path,
            model_name="gpt-5",
            rollout_model="anthropic/claude-opus-4-8",
            execute_rollout=True,
            execute_red=True,
            rollout_trials=2,
            config_path=config_path,
            timeout_seconds=300,
            rollout_max_iterations=3,
        ).validate()
    VerificationConfig(
        harbor_root=tmp_path,
        model_name="gpt-5",
        rollout_model="anthropic/claude-opus-4-8",
        execute_rollout=True,
        execute_red=True,
        rollout_trials=2,
        config_path=config_path,
        timeout_seconds=14400,
        rollout_max_iterations=500,
    ).validate()


_VERIFIER_PAYLOAD = {
    "status": "READY",
    "test_outputs_py": (
        "def test_missing(): assert False\n"
        "def test_protective(): assert True\n"
        "def test_output(): assert True\n"
    ),
    "oracle_solutions": [
        {"name": "oracle-a", "script": "echo a", "justification": "a"},
        {"name": "oracle-b", "script": "echo b", "justification": "b"},
    ],
    "mutation_solutions": [{"name": "mutation", "script": "echo m", "justification": "m"}],
    "missing_capability_tests": ["test_missing"],
    "protective_tests": ["test_protective"],
    "obligation_coverage": {"obl-001": ["test_output"]},
    "expected_value_strategy": "independent calculation",
    "open_questions": [],
}

_TASK = {
    "task_instruction": "do x",
    "core_objective": "do x",
    "acceptance_obligations": [{"id": "obl-001", "text": "output exists"}],
}


class VerifierRuntime:
    model_name = "test-model"

    def __init__(self, *, sandbox: bool = False, pytest_runs: list[dict[str, Any]] | None = None):
        self.sandbox = sandbox
        self.pytest_runs = pytest_runs or []
        self.called = False

    def run(self, *, role, instruction, session, output_root):
        self.called = True
        if role.result_schema == "traceforge.verifier-semantic-review.v1":
            return AgentResult(role=role.name, backend="test", completed=True, payload={
                "decision": "ACCEPT", "issues": [], "obligation_reviews": [
                    {"obligation_id": "obl-001", "covered": True, "reason": "编排单测审查替身"}]})
        if self.sandbox:
            session.sandbox = object()
            digest = hashlib.sha256(_VERIFIER_PAYLOAD["test_outputs_py"].encode("utf-8")).hexdigest()
            rows = [
                (path.relative_to(session.workspace).as_posix(),
                 "link:" + str(path.readlink()) if path.is_symlink() else
                 hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir")
                for path in sorted(session.workspace.rglob("*"))
            ]
            input_sha256 = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
            session.pytest_runs.extend([
                dict(row, test_sha256=digest, input_sha256=input_sha256, input_unchanged=True)
                for row in self.pytest_runs
            ])
        session.test_outputs_py = _VERIFIER_PAYLOAD["test_outputs_py"]
        return AgentResult(
            role=role.name,
            backend="test",
            payload=dict(_VERIFIER_PAYLOAD),
            final_text=json.dumps(_VERIFIER_PAYLOAD),
            completed=True,
        )


class FakeVerifierModel:
    def complete(self, request: ModelRequest) -> ModelResponse:
        payload = {
            "status": "READY",
            "test_outputs_py": "def test_missing(): assert False\ndef test_protective(): assert True\ndef test_output(): assert True\n",
            "oracle_solutions": [
                {"name": "oracle-a", "script": "echo a", "justification": "a"},
                {"name": "oracle-b", "script": "echo b", "justification": "b"},
            ],
            "mutation_solutions": [{"name": "mutation", "script": "echo m", "justification": "m"}],
            "missing_capability_tests": ["test_missing"],
            "protective_tests": ["test_protective"],
            "obligation_coverage": {"obl-001": ["test_output"]},
            "expected_value_strategy": "independent calculation",
            "open_questions": [],
        }
        return ModelResponse(request.request_id, request.model, "fake", json.dumps(payload), 1, 0.01)


def test_calibration_feedback_contains_failed_test_diagnostics(tmp_path: Path) -> None:
    verdict = tmp_path / "verdict.json"
    message = "首次异常：测试替身连续超时\n" + "调用栈\n" * 600 + "客户端最终异常"
    verdict.write_text(
        json.dumps(
            {
                "exit_code": 1,
                "tests": [
                    {"name": "test_missing", "status": "PASS"},
                    {"name": "test_protective", "status": "FAIL", "message": message},
                ],
            }
        ),
        encoding="utf-8",
    )
    diagnostics = HarborCalibrationExecutor._failure_diagnostics(
        {
            "results": {
                "quality_gate": {"ok": False, "errors": ["TASK_FAIL"]},
                "trials": [
                    {"status": "FAIL", "reward": 0.0, "verdict_path": str(verdict)}
                ],
            }
        }
    )
    assert diagnostics["trials"][0]["tests"] == [
        {"name": "test_missing", "status": "PASS"},
        {"name": "test_protective", "status": "FAIL", "message": message},
    ]
    assert diagnostics["quality_gate"] == {"ok": False, "errors": ["TASK_FAIL"]}


def test_plan_only_verification_never_marks_ready(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('x')\n", encoding="utf-8")
    result = run_reconstruction_verification(
        task={
            "task_instruction": "do x",
            "core_objective": "do x",
            "acceptance_obligations": [{"id": "obl-001", "text": "output exists"}],
        },
        workspace_root=workspace,
        model=FakeVerifierModel(),
        output_root=tmp_path / "verification",
        config=VerificationConfig(
            harbor_root=tmp_path / "harbor",
            model_name="claude-opus-4-8",
            rollout_model="anthropic/claude-opus-4-8",
        ),
    )
    assert result["status"] == "PENDING_EXECUTION"
    assert result["calibration"] == "NOT_RUN"
    assert result["rollout"] == "NOT_RUN"
    assert (tmp_path / "verification" / "verification.json").is_file()
    manifest = json.loads(
        (tmp_path / "verification" / "execution_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["compile_verifier_status"] == "UNVALIDATED"
    assert manifest["compile_status_immutable"] is True
    assert manifest["red_status"] == "NOT_RUN"
    assert manifest["certification_closed"] is False
    assert "same_model_across_reconstruction_roles" not in manifest
    assert manifest["roles"] == {
        "verifier": "claude-opus-4-8",
        "rollout": "anthropic/claude-opus-4-8",
    }


def test_verification_config_requires_two_replay_trials(tmp_path: Path) -> None:
    config = VerificationConfig(
        harbor_root=tmp_path,
        model_name="claude-opus-4-8",
        rollout_model="anthropic/claude-opus-4-8",
        execute_rollout=True,
        rollout_trials=1,
    )
    try:
        config.validate()
    except ValueError as exc:
        assert "至少两次" in str(exc)
    else:
        raise AssertionError("single trial must not pass reproducibility configuration")


def test_rollout_requires_red_execution(tmp_path: Path) -> None:
    config = VerificationConfig(
        harbor_root=tmp_path,
        model_name="claude-opus-4-8",
        rollout_model="anthropic/claude-opus-4-8",
        execute_rollout=True,
    )
    with pytest.raises(ValueError, match="execute-red"):
        config.validate()


def test_verifier_task_rejects_backfilled_criteria() -> None:
    with pytest.raises(ValueError, match="验收义务"):
        verifier_task({"core_objective": "do x", "success_criteria": ["output exists"]})


def test_verifier_task_keeps_intent_obligation_ids() -> None:
    task = verifier_task(
        {
            "task_instruction": "do x and keep y",
            "acceptance_obligations": [{"id": "obl-001", "text": "keep y", "evidence_ref_ids": ["user:0"]}],
        }
    )
    assert task["task_instruction"] == "do x and keep y"
    assert task["acceptance_obligations"][0]["id"] == "obl-001"


def test_non_file_task_reviews_without_fake_pytest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = VerifierRuntime()
    result = run_reconstruction_verification(
        task=_TASK,
        workspace_root=workspace,
        model=None,
        agent=runtime,
        output_root=tmp_path / "verification",
        config=VerificationConfig(
            harbor_root=tmp_path / "harbor",
            model_name="claude-opus-4-8",
            rollout_model="anthropic/claude-opus-4-8",
        ),
        source={"selected_span_has_file_ops": False},
    )
    assert result["status"] == "REVIEW"
    assert result["errors"] == ["NON_FILE_TASK"]
    assert runtime.called is False
    assert "bundle" not in result
    recovered, candidate = run_verifier_recovery(
        task=_TASK,
        workspace_root=workspace,
        agent=runtime,
        output_root=tmp_path / "agent",
        source={"selected_span_has_file_ops": False},
    )
    assert recovered["errors"] == ["NON_FILE_TASK"]
    assert candidate is None
    assert recovered.get("verifier") is None


def test_sandbox_verifier_requires_pytest_red(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('x')\n", encoding="utf-8")
    missing = VerifierRuntime(sandbox=True, pytest_runs=[])
    recovered, candidate = run_verifier_recovery(
        task=_TASK,
        workspace_root=workspace,
        agent=missing,
        output_root=tmp_path / "missing",
    )
    assert recovered["status"] == "REVIEW"
    assert "SANDBOX_PYTEST_RED_REQUIRED" in recovered["errors"]
    assert candidate is None
    ok = VerifierRuntime(
        sandbox=True,
        pytest_runs=[
            {"name": "test_missing", "status": "FAIL"},
            {"name": "test_protective", "status": "PASS"},
        ],
    )
    recovered, candidate = run_verifier_recovery(
        task=_TASK,
        workspace_root=workspace,
        agent=ok,
        output_root=tmp_path / "ok",
    )
    assert recovered["status"] == "READY"
    assert candidate is not None


def test_host_verifier_ast_stays_pending_execution(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('x')\n", encoding="utf-8")
    result = run_reconstruction_verification(
        task=_TASK,
        workspace_root=workspace,
        model=None,
        agent=VerifierRuntime(),
        output_root=tmp_path / "verification",
        config=VerificationConfig(
            harbor_root=tmp_path / "harbor",
            model_name="claude-opus-4-8",
            rollout_model="anthropic/claude-opus-4-8",
        ),
        source={"selected_span_has_file_ops": True},
    )
    assert result["status"] == "PENDING_EXECUTION"
    assert result["calibration"] == "NOT_RUN"



def test_manifest_requires_rollout_and_complete_obligations(tmp_path: Path) -> None:
    config = VerificationConfig(
        harbor_root=tmp_path / "harbor",
        model_name="claude-opus-4-8",
        rollout_model="anthropic/claude-opus-4-8",
        rollout_trials=2,
    )
    base = {
        "status": "READY",
        "calibration": "PASS",
        "errors": [],
        "unverified_obligations": ["obl-001"],
        "rollout": {
            "results": {
                "quality_gate": {"ok": True},
                "trials": [
                    {"status": "PASS", "reward": 1.0},
                    {"status": "PASS", "reward": 1.0},
                ],
            }
        },
        "sft_eligible": False,
    }
    write_execution_manifest(tmp_path, base, config)
    manifest = json.loads((tmp_path / "execution_manifest.json").read_text())
    assert manifest["certification_closed"] is False
    assert manifest["sft_eligible"] is False
    base["unverified_obligations"] = []
    base["sft_eligible"] = True
    write_execution_manifest(tmp_path, base, config)
    manifest = json.loads((tmp_path / "execution_manifest.json").read_text())
    assert manifest["certification_closed"] is False
    assert manifest["sft_eligible"] is True


def test_response_receipts_clear_only_explicit_acceptance_contract(tmp_path: Path) -> None:
    task = {
        "acceptance_obligations": [
            {"id": "obl-002", "text": "finish with acceptance-report JSON", "verifier_kind": "NON_FILE"},
            {"id": "obl-003", "text": "manual review", "verifier_kind": "NON_FILE"},
        ],
        "environment_bindings": [
            {"obligation_id": "obl-002", "verifier_kind": "NON_FILE"},
            {"obligation_id": "obl-003", "verifier_kind": "NON_FILE"},
        ],
    }
    assert _acceptance_report_obligation_ids(task) == ["obl-002"]
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002", "obl-003"]}
    rollout = {"results": {"trials": []}}
    apply_response_receipts(result, rollout, task, expected_trials=2)
    assert result["status"] == "REVIEW"
    assert result["sft_eligible"] is False
    assert result["unverified_obligations"] == ["obl-002", "obl-003"]
    assert any("TRIAL_COUNT_MISMATCH" in item for item in result["errors"])



def test_response_receipt_skips_failed_trials_without_masking_rollout_cause() -> None:
    task = {
        "acceptance_obligations": [
            {"id": "obl-002", "text": "finish with acceptance-report JSON", "verifier_kind": "NON_FILE"},
        ],
        "environment_bindings": [
            {"obligation_id": "obl-002", "verifier_kind": "NON_FILE"},
        ],
    }
    result = {
        "status": "REVIEW",
        "errors": ["HERMES_REPRODUCIBILITY_FAILED"],
        "unverified_obligations": ["obl-002"],
    }
    rollout = {
        "results": {
            "trials": [
                {"status": "INFRA_ERROR", "reward": None, "error_code": "TrajectoryCaptureError"},
                {"status": "FAIL", "reward": 0.0, "error_code": "TASK_FAILED"},
            ]
        }
    }
    apply_response_receipts(result, rollout, task, expected_trials=2)
    assert result["errors"] == ["HERMES_REPRODUCIBILITY_FAILED"]
    assert "RESPONSE_RECEIPT_INVALID:0" not in result["errors"]
    assert "RESPONSE_RECEIPT_INVALID:1" not in result["errors"]
    assert result["unverified_obligations"] == ["obl-002"]
    assert rollout["results"]["trials"][0]["response_receipt_status"] == "SKIPPED"
    assert rollout["results"]["trials"][1]["response_receipt_status"] == "SKIPPED"
    assert rollout["results"]["trials"][0]["response_receipt_skip_reason"] == "TRIAL_STATUS_INFRA_ERROR"
    assert rollout["results"]["trials"][1]["response_receipt_skip_reason"] == "TRIAL_STATUS_FAIL"


def test_response_receipt_still_rejects_invalid_successful_trial(tmp_path: Path) -> None:
    trajectory_path = tmp_path / "trajectory.full.json"
    trajectory_path.write_text(
        json.dumps({"messages": [{"role": "assistant", "content": "done"}]}),
        encoding="utf-8",
    )
    task = {
        "acceptance_obligations": [
            {"id": "obl-002", "text": "finish with acceptance-report JSON", "verifier_kind": "NON_FILE"},
        ],
        "environment_bindings": [
            {"obligation_id": "obl-002", "verifier_kind": "NON_FILE"},
        ],
    }
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002"]}
    rollout = {
        "results": {
            "trials": [
                {"status": "PASS", "reward": 1.0, "trajectory_path": str(trajectory_path)},
            ]
        }
    }
    apply_response_receipts(result, rollout, task, expected_trials=1)
    assert any(item.startswith("RESPONSE_RECEIPT_INVALID:0:") for item in result["errors"])
    assert "NON_FILE_RESPONSE_UNVERIFIED" in result["errors"]
    assert result["unverified_obligations"] == ["obl-002"]


@pytest.mark.parametrize("trials", [1, True, None, {}, "invalid"])
def test_response_receipt_rejects_malformed_trial_collection(trials: Any) -> None:
    task = {
        "acceptance_obligations": [
            {"id": "obl-002", "text": "acceptance-report", "verifier_kind": "NON_FILE"}
        ],
        "environment_bindings": [{"obligation_id": "obl-002", "verifier_kind": "NON_FILE"}],
    }
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002"]}
    apply_response_receipts(result, {"results": {"trials": trials}}, task, expected_trials=2)
    assert result["status"] == "REVIEW"
    assert result["sft_eligible"] is False
    assert "RESPONSE_RECEIPT_TRIALS_MISSING" in result["errors"]
    assert result["unverified_obligations"] == ["obl-002"]


def test_rollout_rejects_boolean_reward() -> None:
    result = {
        "status": "READY", "errors": [], "unverified_obligations": [],
        "rollout": {
            "execution": {"status": "COMPLETED"},
            "results": {
                "quality_gate": {"ok": True},
                "trials": [{"status": "PASS", "reward": True}],
            },
        },
    }
    assert verification._set_rollout_eligibility(result, expected_trials=1) is False
    assert result["status"] == "REVIEW"
    assert result["sft_eligible"] is False


@pytest.mark.parametrize("fail_write", [False, True])
def test_persistence_failure_cannot_leave_rollout_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_write: bool
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('x')\n")

    def calibrate(self: HarborCalibrationExecutor, candidate: Any) -> dict[str, Any]:
        self.bundle = tmp_path / "calibrated-task"
        return {"status": "PASS"}

    # 合成轨迹只支持持久化错误的确定性回归，不代表真实 rollout。
    trajectory = tmp_path / "synthetic-trajectory.json"
    trajectory.write_text(json.dumps({"messages": [{"role": "assistant", "content": "完成"}]}))

    def replay(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "execution": {"status": "COMPLETED"},
            "results": {
                "quality_gate": {"ok": True},
                "trials": [{"status": "PASS", "reward": 1.0, "trajectory_path": str(trajectory)}] * 2,
            },
        }

    write = verification._write

    def record(path: Path, payload: dict[str, Any]) -> None:
        if fail_write and path.name == "model_exchange.json":
            raise OSError("模拟模型交换记录写入失败")
        write(path, payload)

    monkeypatch.setattr(HarborCalibrationExecutor, "run", calibrate)
    monkeypatch.setattr(HarborCalibrationExecutor, "_run_bundle", replay)
    monkeypatch.setattr(verification, "_write", record)
    result = run_reconstruction_verification(
        task=_TASK, workspace_root=workspace, model=FakeVerifierModel(),
        output_root=tmp_path / "verification",
        config=VerificationConfig(
            harbor_root=tmp_path / "harbor", model_name="test-verifier",
            rollout_model="anthropic/test-rollout", execute_red=True, execute_rollout=True,
        ),
    )
    assert result["status"] == ("REVIEW" if fail_write else "READY")
    assert result["sft_eligible"] is (not fail_write)
    assert bool(result["errors"]) is fail_write
    manifest = json.loads((tmp_path / "verification/execution_manifest.json").read_text())
    assert manifest["certification_closed"] is (not fail_write)
    assert manifest["sft_eligible"] is (not fail_write)


@pytest.mark.parametrize("max_rounds", [1, 4, 100])
def test_verification_accepts_positive_round_budget(tmp_path: Path, max_rounds: int) -> None:
    VerificationConfig(
        harbor_root=tmp_path, model_name="test-verifier",
        rollout_model="anthropic/test-rollout", max_rounds=max_rounds,
    ).validate()


@pytest.mark.parametrize("max_rounds", [0, -1, True, 1.5, "4"])
def test_verification_rejects_invalid_round_budget(tmp_path: Path, max_rounds: Any) -> None:
    with pytest.raises(ValueError, match="正整数"):
        VerificationConfig(
            harbor_root=tmp_path, model_name="test-verifier",
            rollout_model="anthropic/test-rollout", max_rounds=max_rounds,
        ).validate()


def test_receipt_does_not_cover_verdict_merely_placed_before_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {
        "acceptance_obligations": [
            {
                "id": "obl-002",
                "text": "Return only verdict, finding counts, report path.",
                "observable": "The response lists these prior to the acceptance-report block.",
            },
            {"id": "obl-003", "text": "Finish with the required acceptance-report JSON."},
        ],
        "environment_bindings": [
            {"obligation_id": key, "verifier_kind": "NON_FILE"} for key in ("obl-002", "obl-003")
        ],
    }
    assert _acceptance_report_obligation_ids(task) == ["obl-003"]
    monkeypatch.setattr(response_acceptance, "_attach_response_receipts", lambda *args: ([], [
        {"verified_obligation_ids": [], "contract_checks": []}
    ] * 2))
    result = {"unverified_obligations": ["obl-002", "obl-003"]}
    apply_response_receipts(result, {"results": {"trials": []}}, task, expected_trials=2)
    assert result["unverified_obligations"] == ["obl-002", "obl-003"]


def test_valid_receipt_does_not_certify_combined_response_obligation(tmp_path: Path) -> None:
    report = {
        "criteriaSatisfied": [
            {"id": "criterion-1", "status": "satisfied", "evidence": "self-report"}
        ],
        "changedFiles": [], "testsAddedOrUpdated": [], "commandsRun": [],
        "validationOutput": [], "residualRisks": [], "noStagedFiles": True,
        "diffSummary": "", "reviewFindings": [],
    }
    fence = chr(96) * 3
    block = f"{fence}acceptance-report\n{json.dumps(report)}\n{fence}"
    task = {
        "source_task": {"user_texts": ["## Acceptance Contract\n" + block]},
        "acceptance_obligations": [{
            "id": "response",
            "text": "Return the correct verdict, finding counts and report path, then acceptance-report JSON.",
        }],
        "environment_bindings": [{"obligation_id": "response", "verifier_kind": "NON_FILE"}],
    }
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps({"messages": [{"role": "assistant", "content": block}]}))
    rollout = {"results": {"trials": [
        {"status": "PASS", "trajectory_path": str(path)},
    ]}}
    result = {"status": "READY", "unverified_obligations": ["response"]}
    apply_response_receipts(result, rollout, task, expected_trials=1)
    assert rollout["results"]["trials"][0]["response_receipt_status"] == "VERIFIED"
    assert result["unverified_obligations"] == ["response"]
    assert result["response_receipts"][0]["verified_obligation_ids"] == []


def _response_task(*, semantic: bool = False) -> dict:
    obligations = [
        {"id": "obl-001", "text": "output exists"},
        {"id": "obl-002", "text": "finish with acceptance-report JSON"},
    ]
    if semantic:
        obligations.append({"id": "obl-003", "text": "the acceptance-report correctly reviews every code change"})
    return {
        **_TASK, "acceptance_obligations": obligations,
        "environment_bindings": [
            {"obligation_id": item["id"], "verifier_kind": "FILE" if item["id"] == "obl-001" else "NON_FILE"}
            for item in obligations
        ],
    }


def test_response_structure_obligation_is_pending_until_rollout_not_a_precondition() -> None:
    result = {"errors": [], "unverified_obligations": []}
    assert not _record_unverified_obligations(result, task=_response_task())
    assert result["unverified_obligations"] == ["obl-002"]
    assert result["pending_response_obligations"] == ["obl-002"]
    assert result["errors"] == []


def test_unknown_semantic_response_obligation_remains_visible_without_blocking_attempt() -> None:
    result = {"errors": [], "unverified_obligations": []}
    assert not _record_unverified_obligations(result, task=_response_task(semantic=True))
    assert result["unverified_obligations"] == ["obl-002", "obl-003"]
    assert result["pending_response_obligations"] == ["obl-002", "obl-003"]


def test_receipt_does_not_clear_obligations_without_machine_contract(tmp_path: Path) -> None:
    from test_response_receipt import trajectory

    path = tmp_path / "trajectory.full.json"
    path.write_bytes(trajectory())
    rollout = {"results": {"trials": [
        {"status": "PASS", "reward": 1.0, "trajectory_path": str(path)},
        {"status": "PASS", "reward": 1.0, "trajectory_path": str(path)},
    ]}}
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002", "obl-003"]}
    apply_response_receipts(result, rollout, _response_task(semantic=True), 2)
    assert result["unverified_obligations"] == ["obl-002", "obl-003"]
    assert len(result["response_receipts"]) == 2
    assert all(item["verification_scope"] == "REPORT_STRUCTURE_ONLY" for item in result["response_receipts"])
    assert all(item["semantic_verified"] is False for item in result["response_receipts"])


def test_missing_trial_entry_does_not_clear_response_obligation() -> None:
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002"]}
    apply_response_receipts(result, {"results": {"trials": [None]}}, _response_task(), 1)
    assert result["unverified_obligations"] == ["obl-002"]
    assert result["status"] == "REVIEW"
    assert "NON_FILE_RESPONSE_UNVERIFIED" in result["errors"]


def test_report_structure_path_reaches_verifier_before_response_exists(tmp_path: Path, monkeypatch) -> None:
    from traceforge.reconstruction import verifier_recovery

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print(1)\n")
    calls = []

    def stop_at_verifier(**kwargs):
        calls.append(kwargs["task"])
        return {"errors": ["FIXTURE_STOP_AFTER_GATE"]}, None

    monkeypatch.setattr(verifier_recovery, "run_verifier_recovery", stop_at_verifier)
    result = run_reconstruction_verification(
        task=_response_task(), workspace_root=workspace, model=None, agent=object(),
        output_root=tmp_path / "verification",
        config=VerificationConfig(
            harbor_root=tmp_path, model_name="test", rollout_model="test/test",
            execute_red=True, execute_rollout=True, max_rounds=1,
        ),
    )
    assert len(calls) == 1
    assert result["unverified_obligations"] == ["obl-002"]
    assert result["pending_response_obligations"] == ["obl-002"]
    assert result["rollout"] == "NOT_RUN"


def test_machine_contract_clears_only_after_all_real_trial_receipts(tmp_path: Path) -> None:
    from test_response_receipt import acceptance_contract, trajectory

    task = _response_task(semantic=True)
    task["response_contract"] = acceptance_contract()
    task["response_contract"]["checks"][0]["obligation_id"] = "obl-002"
    path = tmp_path / "trajectory.full.json"
    path.write_bytes(trajectory())
    rollout = {"results": {"trials": [
        {"status": "PASS", "reward": 1.0, "trajectory_path": str(path)},
        {"status": "PASS", "reward": 1.0, "trajectory_path": str(path)},
    ]}}
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002", "obl-003"]}
    apply_response_receipts(result, rollout, task, 2)
    assert result["unverified_obligations"] == ["obl-003"]
    assert result["pending_response_obligations"] == ["obl-003"]
    assert result["response_receipts"][0]["verified_obligation_ids"] == ["obl-002"]


def test_machine_contract_failure_keeps_obligation_unverified(tmp_path: Path) -> None:
    from test_response_receipt import acceptance_contract, trajectory

    task = _response_task()
    task["response_contract"] = acceptance_contract()
    task["response_contract"]["checks"][0].update(
        obligation_id="obl-002", criterion_ids=["different-criterion"]
    )
    path = tmp_path / "trajectory.full.json"
    path.write_bytes(trajectory())
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002"]}
    apply_response_receipts(result, {"results": {"trials": [
        {"status": "PASS", "reward": 1.0, "trajectory_path": str(path)},
    ]}}, task, 1)
    assert result["unverified_obligations"] == ["obl-002"]
    assert result["status"] == "REVIEW"


def test_one_trial_contract_failure_prevents_clearing_for_all_trials(tmp_path: Path) -> None:
    from test_response_receipt import acceptance_contract, report, trajectory

    task = _response_task()
    task["response_contract"] = acceptance_contract()
    task["response_contract"]["checks"][0]["obligation_id"] = "obl-002"
    good = tmp_path / "good.json"
    good.write_bytes(trajectory())
    bad_report = report()
    bad_report["criteriaSatisfied"][0]["id"] = "different-criterion"
    bad = tmp_path / "bad.json"
    fence = chr(96) * 3
    bad.write_bytes(trajectory(fence + "acceptance-report\n" + json.dumps(bad_report) + "\n" + fence))
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002"]}
    apply_response_receipts(result, {"results": {"trials": [
        {"status": "PASS", "reward": 1.0, "trajectory_path": str(good)},
        {"status": "PASS", "reward": 1.0, "trajectory_path": str(bad)},
    ]}}, task, 2)
    assert result["unverified_obligations"] == ["obl-002"]
    assert result["status"] == "REVIEW"
    assert result["sft_eligible"] is False


def test_basic_summary_contract_uses_trial_snapshot_in_orchestration(tmp_path: Path) -> None:
    from test_response_receipt import _summary_fixture

    trial_root = tmp_path / "trial"
    data, contract, _ = _summary_fixture(trial_root)
    path = trial_root / "agent/trajectory.full.json"
    path.parent.mkdir()
    path.write_bytes(data)
    task = _response_task()
    task["acceptance_obligations"][1]["text"] = "Return only the verdict, finding counts and report path."
    contract["checks"][0]["obligation_id"] = "obl-002"
    task["response_contract"] = contract
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002"]}
    apply_response_receipts(result, {"results": {"trials": [
        {"status": "PASS", "reward": 1.0, "trajectory_path": str(path)},
    ]}}, task, 1)
    assert result["unverified_obligations"] == []
    assert result["errors"] == []
    check = result["response_receipts"][0]["contract_checks"][0]
    assert check["verification_scope"] == "REPORT_CONSISTENCY_ONLY"
    assert check["report_sha256"]


def test_explicit_report_contract_bypasses_only_unrequested_legacy_fields(tmp_path: Path) -> None:
    from test_response_receipt import acceptance_contract, trajectory

    task = _response_task()
    task["response_contract"] = acceptance_contract()
    task["response_contract"]["checks"][0].update(
        obligation_id="obl-002", criterion_ids=[], required_fields={"summary": "string"}
    )
    fence = chr(96) * 3
    path = tmp_path / "trajectory.full.json"
    path.write_bytes(trajectory(fence + 'acceptance-report\n{"summary":"finished"}\n' + fence))
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002"]}
    apply_response_receipts(result, {"results": {"trials": [
        {"status": "PASS", "reward": 1.0, "trajectory_path": str(path)},
    ]}}, task, 1)
    assert result["unverified_obligations"] == []
    assert result["errors"] == []
    assert result["response_receipts"][0]["verification_scope"] == "REPORT_BINDING_ONLY"
    assert result["response_receipts"][0]["contract_checks"][0]["verification_scope"] == "REPORT_STRUCTURE_ONLY"



@pytest.mark.parametrize("failure", ["execution", "certification"])
def test_shared_receipt_cannot_clear_obligations_after_execution_or_certification_failure(tmp_path, failure):
    from test_response_receipt import acceptance_contract, trajectory

    path = tmp_path / "synthetic-trajectory.json"
    path.write_bytes(trajectory())
    task = _response_task()
    task["response_contract"] = acceptance_contract()
    task["response_contract"]["checks"][0]["obligation_id"] = "obl-002"
    trial = {"status": "PASS", "reward": 1.0, "trajectory_path": str(path), "content_valid": failure != "certification"}
    rollout = {"execution": {"status": "FAILED" if failure == "execution" else "COMPLETED"},
               "results": {"trials": [trial]}}
    result = {"status": "READY", "errors": [], "unverified_obligations": ["obl-002"]}
    apply_response_receipts(result, rollout, task, 1)
    assert result["status"] == "REVIEW"
    assert result["unverified_obligations"] == ["obl-002"]
    assert not result.get("response_receipts")


def test_unrequested_report_does_not_inherit_legacy_required_fields(tmp_path):
    from test_response_receipt import trajectory

    path = tmp_path / "synthetic-trajectory.json"
    path.write_bytes(trajectory('```acceptance-report\n{"summary":"合成回复"}\n```'))
    result = {"status": "READY", "errors": [], "unverified_obligations": []}
    rollout = {"execution": {"status": "COMPLETED"}, "results": {"trials": [
        {"status": "PASS", "reward": 1.0, "trajectory_path": str(path)},
    ]}}
    apply_response_receipts(result, rollout, {}, 1)
    assert result["status"] == "READY"
    assert result["response_receipts"][0]["verification_scope"] == "REPORT_BINDING_ONLY"


@pytest.mark.parametrize("defect", [None, "test", "criteria", "oracle", "missing_review", "stale_review", "incomplete_review",
                                  "missing_binding", "task_binding", "source_binding", "workspace_binding"])
def test_reviewed_candidate_resume_reuses_exact_candidate_or_regenerates(tmp_path, monkeypatch, defect):
    from copy import deepcopy
    from test_artifact_review import candidate as fixture_candidate, TASK
    from traceforge.reconstruction import verifier_recovery

    seed = fixture_candidate()
    task, source = deepcopy(TASK), None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print(1)")
    review = {
        "status": "ACCEPT", "errors": [], "candidate_id": seed.candidate_id,
        "prompt_version": verifier_recovery.VERIFIER_SEMANTIC_REVIEW_PROMPT_VERSION,
        "test_sha256": hashlib.sha256(seed.test_outputs_py.encode()).hexdigest(),
        "agent": {"completed": True},
    }
    audit = {"status": "READY", "errors": [], "verifier": json.loads(json.dumps(seed.to_dict())),
             "semantic_review": review, "agent": {"completed": True},
             "unverified_obligations": ["extract"], "pending_file_semantic_obligations": ["extract"]}
    audit["input_binding"] = verifier_recovery.verifier_input_binding(TASK, workspace, None)
    frozen = deepcopy(audit)
    if defect == "test":
        audit["verifier"]["test_outputs_py"] += "# changed"
    elif defect == "criteria":
        audit["verifier"]["file_semantic_checks"]["extract"] = "changed"
    elif defect == "oracle":
        audit["verifier"]["oracle_solutions"][0]["script"] = "print(0)"
    elif defect == "missing_review":
        audit.pop("semantic_review")
    elif defect == "stale_review":
        review["prompt_version"] = "old"
    elif defect == "incomplete_review":
        review["agent"]["completed"] = False
    elif defect == "missing_binding":
        audit.pop("input_binding")
    elif defect == "task_binding":
        task["task_instruction"] += "新增不同要求"
    elif defect == "source_binding":
        source = {"raw_session": {"messages": [{"role": "user", "content": "另一个原始请求"}]}}
    elif defect == "workspace_binding":
        (workspace / "main.py").write_text("print(2)")
    calls = []

    def generate(**kwargs):
        calls.append(("generate", kwargs.get("feedback")))
        return frozen, seed

    class Calibration:
        def __init__(self, **kwargs):
            self.bundle = tmp_path / "bundle"
            self.attempts = []

        def run(self, candidate):
            calls.append(("calibrate", candidate))
            self.attempts.append({"status": "FAIL"})
            # 校准失败仍使用既有返修反馈，第二次停止以免执行真实 rollout。
            return {"status": "FAIL", "feedback": json.dumps({"failure": "真实校准替身反例"})}

    monkeypatch.setattr(verifier_recovery, "run_verifier_recovery", generate)
    monkeypatch.setattr(verification, "HarborCalibrationExecutor", Calibration)
    result = run_reconstruction_verification(
        task=task, source=source, workspace_root=workspace, model=None, output_root=tmp_path / "out",
        agent=object(), config=VerificationConfig(
            harbor_root=tmp_path, model_name="fixture", rollout_model="test/model",
            execute_red=True, max_rounds=2,
        ), reviewed_candidate=(seed, audit),
    )
    assert result["candidate_resume"]["status"] == ("RESTORED" if defect is None else "REGENERATE")
    assert calls[0][0] == ("calibrate" if defect is None else "generate")
    assert calls[-2][0] == "generate"
    assert calls[-2][1]["failure"] == "真实校准替身反例"
    assert calls[-2][1]["previous_candidate"] == seed.to_dict()
    assert result["calibration_runs"] == [{"status": "FAIL"}, {"status": "FAIL"}]
    assert result["status"] == "REVIEW"


def test_skipped_calibration_returns_to_author_without_relaxing_grade(tmp_path: Path) -> None:
    from traceforge.verifier.grading import grade

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    test_file = tmp_path / "test_outputs.py"
    test_file.write_text(
        "import pytest\ndef test_existing_behavior(): assert True\n"
        "def test_migrated_interface(): pytest.skip('旧接口已迁移，当前测试未覆盖')\n"
    )
    trial_root = tmp_path / "jobs" / "job-1" / "trial-1"
    logs = trial_root / "verifier"
    verdict = grade(workspace=workspace, tests=test_file, log_dir=logs)
    (trial_root / "config.json").write_text(json.dumps({"task": {"path": str(workspace)}}))
    (trial_root / "result.json").write_text(json.dumps({"config": {"task": {"path": str(workspace)}}}))
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "rollout_plan.json").write_text(json.dumps({
        "dataset": {"dataset_root": str(tmp_path), "task_relative_paths": ["workspace"]},
        "jobs_root": str(tmp_path / "jobs"), "job_name": "job-1",
    }))
    assert verdict["error_code"] == "TEST_CASES_INCOMPLETE"
    assert verdict["reward"] is None and not (logs / "reward.json").exists()
    assert verdict["tests"][1]["status"] == "SKIPPED"
    assert "旧接口已迁移" in verdict["tests"][1]["message"]
    trial = {
        "status": "INFRA_ERROR", "reward": None, "error_code": "RewardFileNotFoundError",
        "content_errors": ["FILE_SNAPSHOT_EXECUTION_INCOMPLETE"],
        "verdict_path": str(logs / "verdict.json"), "result_path": str(trial_root / "result.json"),
    }
    run = {
        "plan": str(plan), "execution": {"status": "COMPLETED"},
        "results": {
            "cleanup": {"ok": True}, "trials": [trial],
            "quality_gate": {"ok": False, "reasons": [
                "TRIAL_INCOMPLETE_OR_INFRA_ERROR", "TRAJECTORY_OR_ARTIFACT_CONTENT_INVALID",
            ]},
        },
    }
    assert HarborCalibrationExecutor._case("oracle", "oracle_pass", run, "PASS").status == "MISMATCH"
    diagnostics = HarborCalibrationExecutor._failure_diagnostics(run)
    assert diagnostics["trials"][0]["error_code"] == "TEST_CASES_INCOMPLETE"
    assert "旧接口已迁移" in diagnostics["trials"][0]["tests"][1]["message"]
    # 缺失清理、执行失败或另一个捕获错误都不能被测试跳过掩盖。
    for key, value in [
        ("cleanup", {"ok": False}),
        ("quality_gate", {"ok": False, "reasons": ["TRIAL_COUNT_MISMATCH"]}),
    ]:
        broken = {**run, "results": {**run["results"], key: value}}
        assert HarborCalibrationExecutor._case("oracle", "oracle_pass", broken, "PASS").status == "INFRA_ERROR"
    trial["content_errors"].append("FILE_SNAPSHOT_INITIAL_WORKSPACE_MISMATCH")
    assert HarborCalibrationExecutor._case("oracle", "oracle_pass", run, "PASS").status == "INFRA_ERROR"
    trial["content_errors"] = []
    trial["error_code"] = "TimeoutError"
    assert HarborCalibrationExecutor._case("oracle", "oracle_pass", run, "PASS").status == "INFRA_ERROR"
    trial["error_code"] = "RewardFileNotFoundError"
    other_verdict = tmp_path / "other-verdict.json"
    other_verdict.write_bytes((logs / "verdict.json").read_bytes())
    trial["verdict_path"] = str(other_verdict)
    assert HarborCalibrationExecutor._case("oracle", "oracle_pass", run, "PASS").status == "INFRA_ERROR"
    trial["verdict_path"] = str(logs / "verdict.json")
    (trial_root / "result.json").write_text(json.dumps({"config": {"task": {"path": str(tmp_path)}}}))
    assert HarborCalibrationExecutor._case("oracle", "oracle_pass", run, "PASS").status == "INFRA_ERROR"
