"""缺失上下文与混合义务不能被文件存在性或局部校准掩盖。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from traceforge.reconstruction import verification as verification_module
from traceforge.reconstruction import verifier_recovery as recovery_module
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.verification import VerificationConfig, run_reconstruction_verification
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency


class _JudgmentRuntime:
    """只测判定合并规则；沙盒生命周期本身由独立测试覆盖。"""
    backend = "hermes-sandbox"
    model_name = "fixture"

    def __init__(self, payload):
        self.payload = payload

    def run(self, *, role, instruction, session, output_root):
        session.sandbox_started = True
        session.sandbox_stopped = True
        session.read_only_probe_blocked = True
        session.tool_events.append({"name": "read_file", "ok": True})
        return AgentResult(
            role=role.name, backend=self.backend, payload=dict(self.payload),
            completed=True, final_text="fixture",
        )


def _task():
    return {
        "task_instruction": "修复账单代码，保持现有计费规则",
        "core_objective": "保持计费规则并修复账单代码",
        "acceptance_obligations": [{"id": "billing", "text": "修复账单代码"}],
        "environment_bindings": [{
            "obligation_id": "billing", "verifier_kind": "FILE",
            "required_paths": ["billing.py"], "observable": "账单函数符合业务规则",
        }],
    }


@pytest.mark.parametrize("missing", [[], ["incomplete pricing business rules"]])
def test_missing_business_rules_never_upgraded(tmp_path: Path, missing):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "billing.py").write_text("def bill(\n", encoding="utf-8")
    result = run_workspace_sufficiency(
        task=_task(), workspace_root=workspace,
        agent=_JudgmentRuntime({
            "label": "INSUFFICIENT", "decision": "REVIEW", "confidence": 0.9,
            "reason": "Incomplete source; essential pricing rules cannot be recovered from this excerpt.",
            "missing_context": missing,
        }), output_root=tmp_path / "judge",
    )
    assert result["status"] == "REVIEW"
    assert result["label"] == "INSUFFICIENT"
    assert result["missing_context"] == missing
    assert result["missing_binding_paths"] == []


def test_partial_excerpt_can_pass_when_it_satisfies_the_actual_task(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "billing.py").write_text("# observed excerpt\ndef discount(x): return x * 0.9\n", encoding="utf-8")
    task = _task()
    task["task_instruction"] = "解释 billing.py 中已观察到的折扣表达式"
    task["core_objective"] = task["task_instruction"]
    result = run_workspace_sufficiency(
        task=task, workspace_root=workspace,
        agent=_JudgmentRuntime({
            "label": "SUFFICIENT", "decision": "READY", "confidence": 0.9,
            "reason": "The observed expression contains all facts required to explain it.",
            "missing_context": [],
        }), output_root=tmp_path / "judge",
    )
    assert result["status"] == "READY"


def _config(tmp_path: Path, *, execute_red=True):
    return VerificationConfig(
        harbor_root=tmp_path / "harbor", model_name="fixture",
        rollout_model="fixture/model", execute_red=execute_red,
        execute_rollout=execute_red, rollout_trials=2,
    )


def test_mixed_non_file_obligation_still_runs_file_red(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "billing.py").write_text("def bill(x): return x\n", encoding="utf-8")
    task = _task()
    task["acceptance_obligations"].append({"id": "research", "text": "检索官方税率来源并引用"})
    task["environment_bindings"].append({
        "obligation_id": "research", "verifier_kind": "NON_FILE", "required_paths": [], "observable": "",
    })
    constructed: list[bool] = []

    class FakeExec:
        def __init__(self, **kwargs):
            del kwargs
            constructed.append(True)
            self.attempts = []
            self.bundle = None

        def run(self, generated):
            del generated
            pytest.fail("recovery without a candidate must not calibrate")

    monkeypatch.setattr(verification_module, "HarborCalibrationExecutor", FakeExec)
    monkeypatch.setattr(
        recovery_module,
        "run_verifier_recovery",
        lambda **kwargs: ({"status": "REVIEW", "errors": ["VERIFIER_REVIEW"], "unverified_obligations": []}, None),
    )
    result = run_reconstruction_verification(
        task=task, workspace_root=workspace, model=None,
        agent=object(), output_root=tmp_path / "verification",
        config=_config(tmp_path),
    )
    assert constructed
    assert result["unverified_obligations"] == ["research"]
    assert "UNVERIFIED_OBLIGATIONS" not in result["errors"]
    assert result["sft_eligible"] is False
    assert result["rollout"] == "NOT_RUN"


@pytest.mark.parametrize("execute_red", [True, False])
def test_recovered_unverified_obligations_override_ready_candidate(tmp_path: Path, monkeypatch, execute_red):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "billing.py").write_text("def bill(x): return x\n", encoding="utf-8")
    def recover(**kwargs):
        return {"status": "READY", "errors": [], "unverified_obligations": ["billing"]}, object()
    def forbidden(*args, **kwargs):
        pytest.fail("an incomplete candidate must not be calibrated or compiled")
    monkeypatch.setattr(recovery_module, "run_verifier_recovery", recover)
    monkeypatch.setattr(verification_module, "HarborCalibrationExecutor", lambda **kwargs: SimpleNamespace(attempts=[], bundle=None, run=forbidden))
    monkeypatch.setattr(verification_module, "compile_bundle", forbidden)
    result = run_reconstruction_verification(
        task=_task(), workspace_root=workspace, model=None, agent=object(),
        output_root=tmp_path / "verification", config=_config(tmp_path, execute_red=execute_red),
    )
    assert result["status"] == "REVIEW"
    assert result["unverified_obligations"] == ["billing"]
    assert "UNVERIFIED_OBLIGATIONS" in result["errors"]
    assert result["sft_eligible"] is False
    assert result["rollout"] == "NOT_RUN"
