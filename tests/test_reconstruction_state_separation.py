"""重建完成与真实解题验收分离；确定性替身只验证编排状态。"""

import json
from pathlib import Path

import pytest
from test_reconstruction_verification import _VERIFIER_PAYLOAD, _response_task

from traceforge.reconstruction.verification import (
    HarborCalibrationExecutor,
    VerificationConfig,
    run_reconstruction_verification,
)
from traceforge.task_instruction import grounded_response_contract
from traceforge.verifier.synthesis import candidate_from_payload


@pytest.fixture
def calibrated_case(tmp_path: Path, monkeypatch):
    from traceforge.reconstruction import verifier_recovery

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print(1)\n")
    task = _response_task()
    example = {
        "summary": "结果摘要",
        "criteriaSatisfied": [{"id": "criterion-001", "status": "pass", "evidence": "证据"}],
    }
    task["source_task"] = {
        "user_texts": [
            "最终输出以下结构：\n"
            + chr(96) * 3
            + "acceptance-report\n"
            + json.dumps(example)
            + "\n"
            + chr(96) * 3
        ]
    }
    task["response_contract"] = {
        "schema_version": "traceforge.response-contract.v1",
        "checks": [{"kind": "acceptance_report", "obligation_id": "obl-002"}],
    }
    task["response_contract"] = grounded_response_contract(task)
    candidate, _ = candidate_from_payload(
        _VERIFIER_PAYLOAD,
        obligation_ids=["obl-001"],
        model_name="test",
        prompt_sha256="p",
        response_sha256="r",
        task=task,
    )
    assert candidate is not None
    state = {
        "audit": {
            "status": "READY",
            "errors": [],
            "unverified_obligations": ["obl-002"],
            "semantic_review": {
                "status": "ACCEPT",
                "errors": [],
                "obligation_reviews": [
                    {"obligation_id": "obl-001", "covered": True, "reason": "覆盖文件行为"},
                    {"obligation_id": "obl-002", "covered": True, "reason": "仅约束响应结构"},
                ],
            },
        },
        "candidate": candidate,
        "calibration": "PASS",
        "calls": [],
    }

    def recover(**kwargs):
        return state["audit"], state["candidate"]

    def calibrate(self, candidate):
        state["calls"].append("red")
        self.bundle = tmp_path / "calibrated-task"
        return {"status": state["calibration"], "feedback": "{}"}

    def publish(self, **kwargs):
        state["calls"].append("publish")
        return {
            "harbor_bundle": str(tmp_path / "harbor_bundle"),
            "rollout_plan": str(tmp_path / "rollout-plan"),
        }

    def forbid_rollout(*args, **kwargs):
        pytest.fail("重建阶段不应执行 Hermes rollout")

    monkeypatch.setattr(verifier_recovery, "run_verifier_recovery", recover)
    monkeypatch.setattr(HarborCalibrationExecutor, "run", calibrate)
    monkeypatch.setattr(HarborCalibrationExecutor, "publish_calibrated_bundle", publish)
    monkeypatch.setattr(HarborCalibrationExecutor, "_run_bundle", forbid_rollout)

    def run():
        result = run_reconstruction_verification(
            task=task,
            workspace_root=workspace,
            model=None,
            agent=object(),
            output_root=tmp_path / "verification",
            config=VerificationConfig(
                harbor_root=tmp_path,
                model_name="test",
                rollout_model="test/test",
                execute_red=True,
                execute_rollout=False,
                max_rounds=1,
            ),
        )
        manifest = json.loads((tmp_path / "verification/execution_manifest.json").read_text())
        return result, manifest

    return task, state, run


def test_red_only_is_ready_with_reviewed_response_mechanism_pending(calibrated_case):
    _, state, run = calibrated_case
    result, manifest = run()
    assert state["calls"] == ["red", "publish"]
    assert result["status"] == "READY"
    assert result["calibration"] == "PASS"
    assert result["rollout"] == "NOT_RUN"
    assert result["unverified_obligations"] == ["obl-002"]
    assert result["pending_response_obligations"] == ["obl-002"]
    assert result["errors"] == []
    assert result["sft_eligible"] is False
    assert manifest["reconstruction_status"] == "READY"
    assert manifest["certification_closed"] is False
    assert manifest["sft_eligible"] is False


@pytest.mark.parametrize(
    "gap", ["missing_contract", "unsupported_contract", "unreviewed", "semantic_gap"]
)
def test_response_without_supported_reviewed_mechanism_stays_review(calibrated_case, gap):
    task, state, run = calibrated_case
    if gap == "missing_contract":
        task.pop("response_contract")
    elif gap == "unsupported_contract":
        task["response_contract"]["checks"][0]["kind"] = "prove_all_facts"
    elif gap == "unreviewed":
        state["audit"].pop("semantic_review")
    else:
        state["audit"]["semantic_review"]["obligation_reviews"][1]["covered"] = False
    result, manifest = run()
    # 缺少验收机制只影响最终可交付判断，不重新变成上游准入闸。
    assert state["calls"] == ["red", "publish"]
    assert result["status"] == "REVIEW"
    assert result["unverified_obligations"] == ["obl-002"]
    assert "RESPONSE_VERIFIER_MISSING:obl-002" in result["errors"]
    assert manifest["certification_closed"] is False


@pytest.mark.parametrize("gap", ["file_coverage", "generation", "red"])
def test_actual_reconstruction_failures_stay_review(calibrated_case, gap):
    _, state, run = calibrated_case
    if gap == "file_coverage":
        state["audit"]["unverified_obligations"].append("obl-001")
    elif gap == "generation":
        state["candidate"] = None
        state["audit"]["errors"] = ["VERIFIER_SEMANTIC_REPAIR_REQUIRED"]
    else:
        state["calibration"] = "FAIL"
    result, manifest = run()
    assert result["status"] == "REVIEW"
    assert "publish" not in state["calls"]
    assert result["rollout"] == "NOT_RUN"
    assert result["sft_eligible"] is False
    assert manifest["certification_closed"] is False
