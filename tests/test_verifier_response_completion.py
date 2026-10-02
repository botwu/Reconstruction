"""Verifier 补全响应机制必须经过原始要求约束和同次语义审查。"""

import copy
import json

import pytest
from test_reconstruction_verification import VerifierRuntime, _VERIFIER_PAYLOAD

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.verification import (
    HarborCalibrationExecutor,
    VerificationConfig,
    run_reconstruction_verification,
)
from traceforge.reconstruction.verifier_recovery import (
    VERIFIER_SEMANTIC_REVIEW_PROMPT_VERSION,
    run_verifier_recovery,
)
from traceforge.task_instruction import grounded_response_contract
from traceforge.verifier.bundle import compile_bundle


def task_fixture():
    example = {
        "criteriaSatisfied": [{"id": "criterion-1", "status": "satisfied", "evidence": "proof"}],
        "summary": "summary",
    }
    task = {
        "task_id": "response-completion",
        "task_instruction": (
            "Write review.md; return only verdict, counts, path and acceptance-report."
        ),
        "source_task": {
            "user_texts": [
                "Write review.md. Return APPROVED or CHANGES_REQUIRED, "
                "Critical/Important/Minor finding counts and report path. End with:\n"
                + chr(96) * 3
                + "acceptance-report\n"
                + json.dumps(example)
                + "\n"
                + chr(96) * 3
            ]
        },
        "acceptance_obligations": [
            {
                "id": "obl-001",
                "text": "Write a correct review to review.md",
                "evidence_ref_ids": ["user:1"],
            },
            {
                "id": "obl-002",
                "text": "Return only verdict, finding counts and report path",
                "evidence_ref_ids": ["user:1"],
            },
            {
                "id": "obl-003",
                "text": "Finish with the required acceptance-report JSON structure",
                "evidence_ref_ids": ["user:1"],
            },
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-001",
                "verifier_kind": "FILE",
                "required_paths": ["review.md"],
                "initial_required_paths": [],
                "output_paths": ["review.md"],
            },
            {"obligation_id": "obl-002", "verifier_kind": "NON_FILE"},
            {"obligation_id": "obl-003", "verifier_kind": "NON_FILE"},
        ],
        "response_contract": {
            "schema_version": "traceforge.response-contract.v1",
            "checks": [{"kind": "acceptance_report", "obligation_id": "obl-003"}],
        },
    }
    task["response_contract"] = grounded_response_contract(task)
    return task


def completed_contract(task):
    contract = copy.deepcopy(task["response_contract"])
    contract["checks"].append(
        {
            "kind": "basic_summary",
            "obligation_id": "obl-002",
            "verdicts": ["APPROVED", "CHANGES_REQUIRED"],
            "finding_levels": ["Critical", "Important", "Minor"],
            "report_path": "review.md",
            "match_report": True,
        }
    )
    return contract


class ResponseRuntime:
    model_name = "test-model"

    def __init__(self, task, *, reject_semantics=False, contract=None):
        self.original = task
        self.before = copy.deepcopy(task)
        self.contract = completed_contract(task) if contract is None else contract
        self.reject_semantics = reject_semantics
        self.review_task = None

    def run(self, *, role, instruction, session, output_root):
        assert self.original == self.before
        if role.result_schema == "traceforge.verifier-semantic-review.v1":
            self.review_specification = json.loads(instruction.splitlines()[-1])
            self.review_task = self.review_specification["task"]
            ids = [
                "obl-001",
                *dict.fromkeys(
                    c["obligation_id"] for c in self.review_task["response_contract"]["checks"]
                ),
            ]
            return AgentResult(
                role=role.name,
                backend="test",
                completed=True,
                payload={
                    "decision": "REVISE" if self.reject_semantics else "ACCEPT",
                    "issues": [
                        {
                            "obligation_id": "obl-003",
                            "problem": "格式不能证明事实正确",
                            "counterexample": "虚构报告仍有完整字段",
                            "repair": "不能用格式检查覆盖真实性",
                        }
                    ]
                    if self.reject_semantics
                    else [],
                    "obligation_reviews": [
                        {
                            "obligation_id": oid,
                            "covered": not self.reject_semantics,
                            "reason": "逐条核对义务",
                        }
                        for oid in ids
                    ],
                },
            )
        session.test_outputs_py = _VERIFIER_PAYLOAD["test_outputs_py"]
        payload = {
            **copy.deepcopy(_VERIFIER_PAYLOAD),
            "response_contract": copy.deepcopy(self.contract),
        }
        return AgentResult(
            role=role.name,
            backend="test",
            completed=True,
            payload=payload,
            final_text=json.dumps(payload),
        )


def run_recovery(tmp_path, task, runtime):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print(1)\n")
    return run_verifier_recovery(
        task=task, workspace_root=workspace, agent=runtime, output_root=tmp_path / "recovery"
    )


def test_verifier_completes_missing_summary_before_same_semantic_review(tmp_path):
    task = task_fixture()
    runtime = ResponseRuntime(task)
    result, candidate = run_recovery(tmp_path, task, runtime)
    assert candidate is not None
    assert result["status"] == "READY"
    assert {c["obligation_id"] for c in result["response_contract"]["checks"]} == {
        "obl-002",
        "obl-003",
    }
    assert runtime.review_task["response_contract"] == result["response_contract"]
    assert task == runtime.before
    assert len(task["response_contract"]["checks"]) == 1
    context = runtime.review_specification["verification_context"]
    assert context["phase"] == "RECONSTRUCTION"
    assert context["file_verifier"] == "candidate.test_outputs_py"
    assert context["response_verifier"] == (
        "traceforge.harbor_ags.response_receipt.evaluate_response_contract"
    )
    assert context["response_execution_status"] == "NOT_RUN"
    assert context["response_evidence"] == "真实 rollout 的 trajectory.full.json"
    assert result["unverified_obligations"] == ["obl-002", "obl-003"]
    assert result["semantic_review"]["prompt_version"] == VERIFIER_SEMANTIC_REVIEW_PROMPT_VERSION
    assert not list((tmp_path / "workspace").rglob("trajectory.full.json"))


def test_ready_response_mechanism_does_not_hide_file_verifier_defect(tmp_path):
    class FileRejectingRuntime(ResponseRuntime):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            if kwargs["role"].result_schema == "traceforge.verifier-semantic-review.v1":
                result.payload["decision"] = "REVISE"
                result.payload["obligation_reviews"][0].update(
                    covered=False, reason="正确的跨文件引用被测试错误拒绝"
                )
                result.payload["issues"] = [{
                    "obligation_id": "obl-001",
                    "problem": "验证器把引用限制在之前的改动清单内",
                    "counterexample": "正确报告引用必要但未修改的调用位置仍被拒绝",
                    "repair": "核对实际文件和行号，不额外缩小用户允许的审查范围",
                }]
            return result

    task = task_fixture()
    runtime = FileRejectingRuntime(task)
    result, candidate = run_recovery(tmp_path, task, runtime)
    assert candidate is None
    assert result["status"] == "REVIEW"
    assert result["response_contract"] is None
    assert result["errors"] == ["VERIFIER_SEMANTIC_REPAIR_REQUIRED"]
    reviews = result["semantic_review"]["obligation_reviews"]
    assert reviews[0]["covered"] is False
    assert all(row["covered"] for row in reviews[1:])
    assert result["feedback"]["semantic_review"]["issues"][0]["obligation_id"] == "obl-001"


@pytest.mark.parametrize(
    "fault", ["unsupported", "forged_path", "unknown_obligation", "missing", "semantic"]
)
def test_invalid_response_mechanism_is_not_published(tmp_path, fault):
    task = task_fixture()
    contract = completed_contract(task)
    if fault == "unsupported":
        contract["checks"][-1]["kind"] = "prove_facts"
    elif fault == "forged_path":
        contract["checks"][-1]["report_path"] = "invented.md"
    elif fault == "unknown_obligation":
        contract["checks"][-1]["obligation_id"] = "invented-obligation"
    elif fault == "missing":
        contract = copy.deepcopy(task["response_contract"])
    else:
        task["acceptance_obligations"][2]["text"] = (
            "All statements in the report must be factually true"
        )
    runtime = ResponseRuntime(task, reject_semantics=fault == "semantic", contract=contract)
    result, candidate = run_recovery(tmp_path, task, runtime)
    assert candidate is None
    assert result["status"] == "REVIEW"
    assert result.get("response_contract") is None
    assert result["feedback"]["generation_errors"]
    assert task == runtime.before
    if fault == "semantic":
        assert result["semantic_review"]["status"] == "REVISE"


def test_final_contract_is_identical_in_review_red_bundle_and_verification(tmp_path, monkeypatch):
    task = task_fixture()
    runtime = ResponseRuntime(task)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print(1)\n")
    compiled = []

    def calibrate(self, candidate):
        assert self.task["response_contract"] == runtime.review_task["response_contract"]
        bundle = compile_bundle(
            task=self.task,
            workspace_root=workspace,
            verifier=candidate,
            output_root=tmp_path / "bundles",
        )
        self.bundle = bundle / "task"
        compiled.append(self.bundle)
        return {"status": "PASS", "feedback": ""}

    def publish(self, **kwargs):
        return {"harbor_bundle": str(compiled[0].parent)}

    monkeypatch.setattr(HarborCalibrationExecutor, "run", calibrate)
    monkeypatch.setattr(HarborCalibrationExecutor, "publish_calibrated_bundle", publish)
    result = run_reconstruction_verification(
        task=task,
        workspace_root=workspace,
        model=None,
        agent=runtime,
        output_root=tmp_path / "verification",
        config=VerificationConfig(
            harbor_root=tmp_path,
            model_name="test",
            rollout_model="test/test",
            execute_red=True,
            max_rounds=1,
        ),
    )
    assert result["status"] == "READY"
    control = json.loads((compiled[0] / "tests/control/input-manifest.json").read_text())
    saved = json.loads((tmp_path / "verification/verification.json").read_text())
    assert (
        saved["response_contract"]
        == result["response_contract"]
        == control["task_acceptance"]["response_contract"]
        == runtime.review_task["response_contract"]
    )
    assert result["response_verifier_ready_obligations"] == ["obl-002", "obl-003"]
    assert result["pending_response_obligations"] == ["obl-002", "obl-003"]
    assert result["rollout"] == "NOT_RUN" and result["sft_eligible"] is False
    assert task == runtime.before


@pytest.mark.parametrize("reject_file_verifier", [False, True])
def test_manual_response_review_keeps_file_gate_and_uncertified_rollouts(
    tmp_path, monkeypatch, reject_file_verifier
):
    task = task_fixture()
    task["task_instruction"] = "Write review.md and analyze code quality."
    task["source_task"]["user_texts"] = [task["task_instruction"]]
    task["acceptance_obligations"] = task["acceptance_obligations"][:2]
    task["acceptance_obligations"][1]["text"] = "Analyze code quality"
    task["environment_bindings"] = task["environment_bindings"][:2]
    task["response_contract"] = None
    original = copy.deepcopy(task)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print(1)\n")
    calls = []

    class Runtime(VerifierRuntime):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            if kwargs["role"].result_schema == "traceforge.verifier-semantic-review.v1":
                spec = json.loads(kwargs["instruction"].splitlines()[-1])
                assert spec["manual_response_obligation_ids"] == ["obl-002"]
                assert spec["response_obligation_ids"] == []
                assert spec["task"]["source_task"]["user_texts"] == original["source_task"]["user_texts"]
                if reject_file_verifier:
                    result.payload["decision"] = "REVISE"
                    result.payload["obligation_reviews"][0].update(
                        covered=False, reason="关键词检查会放过错误内容"
                    )
                    result.payload["issues"] = [{"obligation_id": "obl-001", "problem": "漏检"}]
            return result

    def calibrate(self, candidate):
        calls.append("calibration")
        bundle = compile_bundle(
            task=self.task, workspace_root=workspace, verifier=candidate,
            output_root=tmp_path / "bundles",
        )
        self.bundle = bundle / "task"
        assert "analyze code quality" in (self.bundle / "instruction.md").read_text()
        return {"status": "PASS"}

    # 仅用于验证编排和认证边界；这两条合成轨迹不是真实 rollout 证据。
    trajectory = tmp_path / "synthetic-trajectory.json"
    trajectory.write_text(json.dumps({"messages": [{"role": "assistant", "content": "分析内容"}]}))

    def rollout(self, *args, **kwargs):
        calls.append("rollout")
        return {
            "execution": {"status": "COMPLETED"},
            "results": {"quality_gate": {"ok": True}, "trials": [
                {"status": "PASS", "reward": 1.0, "trajectory_path": str(trajectory)}
                for _ in range(2)
            ]},
        }

    monkeypatch.setattr(HarborCalibrationExecutor, "run", calibrate)
    monkeypatch.setattr(HarborCalibrationExecutor, "_run_bundle", rollout)
    result = run_reconstruction_verification(
        task=task, workspace_root=workspace, model=None, agent=Runtime(),
        output_root=tmp_path / "verification",
        config=VerificationConfig(
            harbor_root=tmp_path, model_name="test", rollout_model="test/test",
            execute_red=True, execute_rollout=True, manual_response_review=True, max_rounds=1,
        ),
    )
    assert task == original
    assert result["status"] == "REVIEW" and result["sft_eligible"] is False
    assert result["unverified_obligations"] == ["obl-002"]
    assert result["manual_response_review"] == {"status": "NOT_ASSESSED", "obligation_ids": ["obl-002"]}
    assert "MANUAL_RESPONSE_REVIEW_REQUIRED" in result["errors"]
    assert not any(e.startswith("RESPONSE_VERIFIER_MISSING") for e in result["errors"])
    manifest = json.loads((tmp_path / "verification/execution_manifest.json").read_text())
    assert manifest["certification_closed"] is False
    if reject_file_verifier:
        assert calls == [] and result["rollout"] == "NOT_RUN"
        assert result["iterations"][0]["errors"] == ["VERIFIER_SEMANTIC_REPAIR_REQUIRED"]
    else:
        assert calls == ["calibration", "rollout"]
        assert len(result["response_receipts"]) == 2
        assert all(not r["verified_obligation_ids"] for r in result["response_receipts"])
