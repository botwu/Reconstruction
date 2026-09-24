"""独立交付已校准的任务；替身只模拟外部校准，不代表真实运行证据。"""

import json
from pathlib import Path

import pytest

from test_harbor_ags_rollout import _harbor_root
from test_reconstruction_verification import FakeVerifierModel, VerifierRuntime, _TASK
from traceforge.reconstruction import verification
from traceforge.verifier.bundle import compile_bundle


def _verify(tmp_path, monkeypatch, *, outcome="PASS", agent=False, rollout=False):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print('task-start')\n")

    def calibrate(self, candidate):
        self.bundle = compile_bundle(
            task=self.task, workspace_root=self.workspace, verifier=candidate,
            output_root=self.root / "fixture-calibration",
        ) / "task"
        self.attempts.append({"status": outcome, "test_fixture": True})
        return {"status": outcome, "feedback": "fixture"}

    def execute(*args, **kwargs):
        if not rollout:
            pytest.fail("仅 RED 校准的交付不得启动 rollout")
        return {"status": "FAILED"}

    monkeypatch.setattr(verification.HarborCalibrationExecutor, "run", calibrate)
    monkeypatch.setattr(verification, "execute_rollout_plan", execute)
    return verification.run_reconstruction_verification(
        task=_TASK, workspace_root=workspace,
        model=None if agent else FakeVerifierModel(),
        agent=VerifierRuntime() if agent else None,
        output_root=tmp_path / "verification",
        config=verification.VerificationConfig(
            harbor_root=_harbor_root(tmp_path / "harbor"), model_name="test-verifier",
            rollout_model="anthropic/test-rollout", execute_red=True,
            execute_rollout=rollout, max_rounds=1,
        ),
    )


@pytest.mark.parametrize("agent", [False, True])
def test_red_only_publishes_real_bundle_without_claiming_rollout(tmp_path, monkeypatch, agent):
    result = _verify(tmp_path, monkeypatch, agent=agent)
    assert result["status"] == "READY"
    bundle = Path(result["harbor_bundle"])
    for relative in (
        "task/task.toml", "task/instruction.md", "task/workspace/main.py",
        "task/solution/solve.sh", "task/tests/test_outputs.py",
        "task/tests/rubric.json", "task/tests/control/input-manifest.json",
        "dataset.toml", "artifact_manifest.json",
    ):
        assert (bundle / relative).is_file(), relative
    assert (bundle / "task/environment").is_dir()
    assert (bundle / "task/tests/control").is_dir()
    input_manifest = json.loads(
        (bundle / "task/tests/control/input-manifest.json").read_text()
    )
    assert input_manifest["schema_version"] == "traceforge.control-input-manifest.v1"
    assert (bundle / "task/instruction.md").read_text().strip() == _TASK["task_instruction"]
    assert (bundle / "task/workspace/main.py").read_text() == "print('task-start')\n"
    assert (bundle / "task/tests/test_outputs.py").read_text() == result["verifier"]["test_outputs_py"]
    assert result["calibration"] == "PASS"
    assert result["rollout"] == "NOT_RUN"
    assert result["sft_eligible"] is False
    manifest = json.loads((tmp_path / "verification/execution_manifest.json").read_text())
    assert manifest["certification_closed"] is False
    assert json.loads((bundle / "artifact_manifest.json").read_text())["execution_status"] == "NOT_ASSERTED"


def test_failed_red_does_not_publish_deliverable(tmp_path, monkeypatch):
    result = _verify(tmp_path, monkeypatch, outcome="FAIL")
    assert result["status"] == "REVIEW"
    assert "harbor_bundle" not in result
    assert not (tmp_path / "verification/deliverables").exists()


def test_publication_failure_cannot_leave_ready(tmp_path, monkeypatch):
    def fail_publication(*args, **kwargs):
        raise verification.HarborRolloutError("测试发布失败")

    monkeypatch.setattr(verification, "publish_rollout_bundle", fail_publication)
    result = _verify(tmp_path, monkeypatch)
    assert result["status"] == "REVIEW"
    assert result["calibration"] == "PASS"
    assert "HARBOR_BUNDLE_PUBLICATION_FAILED" in result["errors"]
    assert result["rollout"] == "NOT_RUN"


def test_rollout_failure_preserves_calibrated_delivery(tmp_path, monkeypatch):
    result = _verify(tmp_path, monkeypatch, rollout=True)
    assert result["status"] == "READY"
    assert Path(result["harbor_bundle"]).is_dir()
    assert result["rollout"]["harbor_bundle"] == result["harbor_bundle"]
    assert result["rollout"]["plan"] == result["rollout_plan"]
    assert len(list((tmp_path / "verification/plans/hermes-replay").glob("*/rollout_plan.json"))) == 1
    assert result["sft_eligible"] is False
    assert "HERMES_REPRODUCIBILITY_FAILED" in result["errors"]
