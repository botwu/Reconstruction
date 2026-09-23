import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse
import pytest

from traceforge.reconstruction.verification import (
    HarborCalibrationExecutor,
    VerificationConfig,
    run_reconstruction_verification,
    verifier_task,
    write_execution_manifest,
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
        if self.sandbox:
            session.sandbox = object()
            digest = hashlib.sha256(_VERIFIER_PAYLOAD["test_outputs_py"].encode("utf-8")).hexdigest()
            session.pytest_runs.extend([dict(row, test_sha256=digest) for row in self.pytest_runs])
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
    verdict.write_text(
        json.dumps(
            {
                "exit_code": 1,
                "tests": [
                    {"name": "test_missing", "status": "PASS"},
                    {"name": "test_protective", "status": "FAIL"},
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
        {"name": "test_protective", "status": "FAIL"},
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
