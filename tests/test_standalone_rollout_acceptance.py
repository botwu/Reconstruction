"""独立 rollout 验收的合成 fixture；不执行模型、沙盒或真实 rollout。"""

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from test_harbor_ags_rollout import _bundle, _harbor_root
from test_harbor_rollout_results import _write, _write_ledger, _write_valid_hermes_artifacts
from test_response_receipt import acceptance_contract, trajectory
from traceforge.cli import main
from traceforge.harbor_ags.rollout import HarborRolloutConfig, build_rollout_plan


pytestmark = pytest.mark.usefixtures("harbor_cleanup")


def _fixture(tmp_path, monkeypatch, *, trials=1, contract=True, runtime=False):
    task = _bundle(tmp_path / "task")
    acceptance = {
        "task_id": "synthetic-task",
        "acceptance_obligations": [{"id": "obl-report", "text": "返回 acceptance-report"}],
        "environment_bindings": [{"obligation_id": "obl-report", "verifier_kind": "NON_FILE"}],
        "response_contract": acceptance_contract() if contract else None,
    }
    _write(task / "tests/control/input-manifest.json", {
        "schema_version": "traceforge.control-input-manifest.v1",
        "task_acceptance": acceptance,
    })
    harbor = _harbor_root(tmp_path / "harbor")
    if runtime:
        from traceforge.harbor_ags.adapter import validate_harbor_bundle

        monkeypatch.setattr(
            "traceforge.harbor_ags.rollout.validate_harbor_bundle",
            lambda task_dir, **_: validate_harbor_bundle(task_dir),
        )
        runtime_root = harbor / "src/harbor_ags"
        runtime_root.mkdir(parents=True)
        for filename in ("agent.py", "capture.py", "evidence.py", "validator.py"):
            (runtime_root / filename).write_text("# 合成 runtime fixture，不执行\n", encoding="utf-8")
    plan_dir = build_rollout_plan(HarborRolloutConfig(
        task_dir=task, harbor_root=harbor,
        output_root=tmp_path / "plans", jobs_root=tmp_path / "jobs", trials=trials,
        timeout_seconds=14400, agent_max_iterations=500,
    ))
    plan = json.loads((plan_dir / "rollout_plan.json").read_text())
    receipt_path = plan_dir / "run_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt.update(status="COMPLETED", external_execution=True, returncode=0)
    _write(receipt_path, receipt)
    job = tmp_path / "jobs" / plan["job_name"]
    for index, relative in enumerate(plan["dataset"]["task_relative_paths"]):
        trial = job / f"synthetic-trial-{index}"
        _write(trial / "config.json", {"task": {"path": str(plan_dir / "dataset" / relative)}})
        _write(trial / "result.json", {"verifier_result": {"rewards": {"task": 1.0}}})
        _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
        _write_valid_hermes_artifacts(trial)
        (trial / "agent/trajectory.full.json").write_bytes(trajectory())
        (trial / "reconstruction-certification.json").unlink()
    _write_ledger(job / "_control/ags-sandbox-ledger.jsonl")
    calls = []

    def validate(trial, *, preserve_source_literals):
        assert preserve_source_literals is True
        calls.append(trial)
        return SimpleNamespace(certified=True, status="TASK_PASS")

    # 本测试核对桥接和来源绑定，不依赖部署机安装的外部 Harbor。
    package = ModuleType("harbor_ags")
    package.__path__ = []
    package.artifacts = ModuleType("harbor_ags.artifacts")
    package.artifacts.build_artifact_manifest = lambda trial: None
    package.validator = ModuleType("harbor_ags.validator")
    package.validator.validate_harbor_trial = validate
    package.validator.__file__ = __file__
    for name, module in (
        ("harbor_ags", package), ("harbor_ags.artifacts", package.artifacts),
        ("harbor_ags.validator", package.validator),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return plan_dir, job, calls


def _read(plan, job):
    from traceforge.harbor_ags.acceptance import read_rollout_acceptance
    return read_rollout_acceptance(plan, job_dir=job)


def test_standalone_cli_certifies_and_checks_bound_final_response(tmp_path, monkeypatch, capsys):
    plan, job, calls = _fixture(tmp_path, monkeypatch)
    assert main(["harbor-ags", "read-results", "--job-dir", str(job), "--plan-dir", str(plan)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["acceptance"]["status"] == "PASS"
    assert report["acceptance"]["unverified_obligations"] == []
    assert len(calls) == 1
    receipt = report["acceptance"]["response_receipts"][0]
    raw = (calls[0] / "agent/trajectory.full.json").read_bytes()
    response = json.loads(raw)["messages"][-1]["content"]
    assert receipt["trajectory_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["response_sha256"] == hashlib.sha256(response.encode()).hexdigest()
    assert receipt["assistant_message_index"] == 1
    assert receipt["verified_obligation_ids"] == ["obl-report"]
    assert json.loads((plan / "rollout_results.json").read_text()) == report
    assert sorted(path.name for path in (plan / "dataset").glob("*/workspace/*")) == ["input.txt"]
    assert report["input_binding"]["plan_sha256"] == hashlib.sha256((plan / "rollout_plan.json").read_bytes()).hexdigest()


def test_standalone_cli_requires_plan_for_hermes(tmp_path, monkeypatch, capsys):
    _, job, calls = _fixture(tmp_path, monkeypatch)
    assert main(["harbor-ags", "read-results", "--job-dir", str(job)]) == 2
    assert "--plan-dir" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("status", ["PLAN_ONLY", "EXECUTING", "FAILED", "TIMEOUT", "ABORTED"])
def test_incomplete_execution_never_certifies_or_accepts(tmp_path, monkeypatch, status):
    plan, job, calls = _fixture(tmp_path, monkeypatch)
    receipt = json.loads((plan / "run_receipt.json").read_text())
    receipt["status"] = status
    _write(plan / "run_receipt.json", receipt)
    report = _read(plan, job)
    assert report["acceptance"]["status"] == "REVIEW"
    assert report["acceptance"]["unverified_obligations"] == ["obl-report"]
    assert calls == []
    assert "response_receipts" not in report["acceptance"]


@pytest.mark.parametrize("failure", ["no_trajectory", "no_final", "wrong_contract", "uncertified", "task_failed", "infra_error", "missing_trial"])
def test_standalone_rejects_incomplete_or_unverified_trial(tmp_path, monkeypatch, failure):
    plan, job, _ = _fixture(tmp_path, monkeypatch, trials=2)
    trial = job / "synthetic-trial-1"
    if failure == "no_trajectory":
        (trial / "agent/trajectory.full.json").unlink()
    elif failure == "no_final":
        _write(trial / "agent/trajectory.full.json", {
            "schema_version": "traceforge-lossless-trajectory-v1",
            "messages": [{"role": "user", "content": "合成输入"}],
        })
    elif failure == "wrong_contract":
        (trial / "agent/trajectory.full.json").write_bytes(trajectory('```acceptance-report\n{"summary":"合成无效回复"}\n```'))
    elif failure == "uncertified":
        import harbor_ags.validator
        monkeypatch.setattr(harbor_ags.validator, "validate_harbor_trial", lambda _, **kwargs: SimpleNamespace(certified=False, status="INFRA_CAPTURE"))
    elif failure == "task_failed":
        _write(trial / "result.json", {"verifier_result": {"rewards": {"task": 0.0}}})
        _write(trial / "verifier/verdict.json", {"status": "TASK_FAIL"})
    elif failure == "infra_error":
        _write(trial / "result.json", {"exception_info": {"exception_type": "TrajectoryCaptureError"}})
    else:
        import shutil
        shutil.rmtree(trial)
    report = _read(plan, job)
    assert report["acceptance"]["status"] == "REVIEW"
    assert report["acceptance"]["unverified_obligations"] == ["obl-report"]


def test_missing_contract_cannot_clear_non_file_obligation(tmp_path, monkeypatch):
    plan, job, _ = _fixture(tmp_path, monkeypatch, contract=False)
    report = _read(plan, job)
    assert report["acceptance"]["status"] == "REVIEW"
    assert report["acceptance"]["unverified_obligations"] == ["obl-report"]


@pytest.mark.parametrize("failure", ["other_job", "other_task", "duplicate_task", "changed_input", "stale_receipt"])
def test_plan_job_task_and_input_binding_fail_closed(tmp_path, monkeypatch, failure):
    from traceforge.harbor_ags.results import HarborResultError
    from traceforge.harbor_ags.rollout import HarborRolloutError
    plan, job, calls = _fixture(tmp_path, monkeypatch, trials=2)
    if failure == "other_job":
        job = tmp_path / "other_job"
        job.mkdir()
    elif failure in {"other_task", "duplicate_task"}:
        config = json.loads((job / "synthetic-trial-0/config.json").read_text())
        if failure == "other_task":
            config["task"]["path"] = str(tmp_path / "task")
        _write(job / "synthetic-trial-1/config.json", config)
    elif failure == "changed_input":
        next((plan / "dataset").glob("*/tests/control/input-manifest.json")).write_text("{}")
    else:
        receipt = json.loads((plan / "run_receipt.json").read_text())
        receipt["artifact_manifest_sha256"] = "0" * 64
        _write(plan / "run_receipt.json", receipt)
    with pytest.raises((HarborResultError, HarborRolloutError)):
        _read(plan, job)
    assert calls == []


def test_repeated_read_recomputes_receipts_without_changing_inputs(tmp_path, monkeypatch):
    plan, job, calls = _fixture(tmp_path, monkeypatch)
    before = (plan / "rollout_plan.json").read_bytes()
    first = _read(plan, job)
    assert _read(plan, job) == first
    assert len(calls) == 2
    (job / "synthetic-trial-0/agent/trajectory.full.json").write_bytes(trajectory("缺少约定报告"))
    assert _read(plan, job)["acceptance"]["status"] == "REVIEW"
    assert (plan / "rollout_plan.json").read_bytes() == before


@pytest.mark.parametrize("wrong_source", [False, True])
def test_explicit_recertification_keeps_original_execution_runtime(tmp_path, monkeypatch, wrong_source):
    from traceforge.harbor_ags import acceptance
    from traceforge.harbor_ags.results import HarborResultError

    plan, job, _ = _fixture(tmp_path, monkeypatch, runtime=True)
    import harbor_ags.validator

    before = (plan / "rollout_plan.json").read_bytes()
    certify = acceptance.certify_hermes_job
    selected = []

    def recertify(job_dir, *, harbor_root):
        selected.append(harbor_root)
        certify(job_dir, harbor_root=harbor_root)

    monkeypatch.setattr(acceptance, "certify_hermes_job", recertify)
    upgraded = tmp_path / "upgraded-harbor"
    source = upgraded / "src/harbor_ags/validator.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"# another validator\n" if wrong_source else Path(harbor_ags.validator.__file__).read_bytes())
    if wrong_source:
        with pytest.raises(HarborResultError, match="实际认证器与指定源码不一致"):
            acceptance.read_rollout_acceptance(plan, job_dir=job, certification_harbor_root=upgraded)
        assert (plan / "rollout_plan.json").read_bytes() == before
        return
    report = acceptance.read_rollout_acceptance(plan, job_dir=job, certification_harbor_root=upgraded)
    assert report["acceptance"]["status"] == "PASS"
    assert selected == [upgraded]
    assert report["input_binding"]["certification_harbor_root"] == str(upgraded.resolve())
    assert (plan / "rollout_plan.json").read_bytes() == before


@pytest.mark.parametrize("change", ["file_only", "unsupported", "missing_acceptance"])
def test_contract_scope_and_required_hidden_inputs(tmp_path, monkeypatch, change):
    from traceforge.harbor_ags.results import HarborResultError
    from traceforge.harbor_ags.rollout import HarborRolloutConfig, build_rollout_plan

    old_plan, old_job, _ = _fixture(tmp_path, monkeypatch)
    task = tmp_path / "task"
    manifest_path = task / "tests/control/input-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if change == "file_only":
        manifest["task_acceptance"].update(
            acceptance_obligations=[], environment_bindings=[], response_contract=None,
        )
    elif change == "unsupported":
        manifest["task_acceptance"]["response_contract"]["checks"][0]["kind"] = "semantic_correctness"
    else:
        manifest.pop("task_acceptance")
    _write(manifest_path, manifest)
    plan = build_rollout_plan(HarborRolloutConfig(
        task_dir=task, harbor_root=tmp_path / "harbor", output_root=tmp_path / "new-plans",
        jobs_root=tmp_path / "new-jobs", timeout_seconds=14400, agent_max_iterations=500,
    ))
    payload = json.loads((plan / "rollout_plan.json").read_text())
    receipt = json.loads((plan / "run_receipt.json").read_text())
    receipt.update(status="COMPLETED", external_execution=True, returncode=0)
    _write(plan / "run_receipt.json", receipt)
    import shutil
    job = tmp_path / "new-jobs" / payload["job_name"]
    shutil.copytree(old_job, job)
    trial = job / "synthetic-trial-0"
    _write(trial / "config.json", {"task": {"path": str(plan / "dataset" / payload["dataset"]["task_relative_paths"][0])}})
    if change == "missing_acceptance":
        with pytest.raises(HarborResultError, match="task_acceptance"):
            _read(plan, job)
        return
    (trial / "agent/trajectory.full.json").write_bytes(trajectory("合成最终回复"))
    result = _read(plan, job)
    if change == "file_only":
        assert result["acceptance"]["status"] == "PASS"
        assert result["acceptance"]["response_receipts"][0]["response_sha256"]
        assert result["acceptance"]["response_receipts"][0]["acceptance_report_sha256"] is None
    else:
        assert result["acceptance"]["status"] == "REVIEW"
        assert result["acceptance"]["unverified_obligations"] == ["obl-report"]


@pytest.mark.parametrize("field,value", [("returncode", 1), ("returncode", False), ("external_execution", False)])
def test_completed_label_requires_real_success_receipt(tmp_path, monkeypatch, field, value):
    plan, job, calls = _fixture(tmp_path, monkeypatch)
    receipt = json.loads((plan / "run_receipt.json").read_text())
    receipt[field] = value
    _write(plan / "run_receipt.json", receipt)
    assert _read(plan, job)["acceptance"]["status"] == "REVIEW"
    assert calls == []


def test_cli_infers_job_and_refuses_mode_override(tmp_path, monkeypatch, capsys):
    plan, _, _ = _fixture(tmp_path, monkeypatch)
    assert main(["harbor-ags", "read-results", "--plan-dir", str(plan)]) == 0
    capsys.readouterr()
    assert main(["harbor-ags", "read-results", "--plan-dir", str(plan), "--agent-mode", "oracle"]) == 2
    assert "agent-mode" in capsys.readouterr().err



def test_standalone_accepts_unchanged_pinned_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan, job, calls = _fixture(tmp_path, monkeypatch, runtime=True)
    payload = json.loads((plan / "rollout_plan.json").read_text())
    assert len(payload["harbor_runtime"]["files"]) == 4
    assert _read(plan, job)["acceptance"]["status"] == "PASS"
    assert len(calls) == 1


@pytest.mark.parametrize("filename", ["agent.py", "capture.py", "evidence.py", "validator.py"])
@pytest.mark.parametrize("deleted", [False, True])
def test_standalone_rejects_runtime_change_before_certification_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, deleted: bool,
) -> None:
    from traceforge.harbor_ags import acceptance
    from traceforge.harbor_ags.rollout import HarborRolloutError

    plan, job, calls = _fixture(tmp_path, monkeypatch, runtime=True)
    assert _read(plan, job)["acceptance"]["status"] == "PASS"
    before = {path: path.read_bytes() for root in (plan, job) for path in root.rglob("*") if path.is_file()}
    runtime = tmp_path / "harbor/src/harbor_ags" / filename
    if deleted:
        runtime.unlink()
    else:
        runtime.write_text("# 已修改的合成 runtime fixture\n", encoding="utf-8")

    def unexpected_certification(*args: object, **kwargs: object) -> None:
        pytest.fail("runtime 校验失败前不得进入认证")

    monkeypatch.setattr(acceptance, "certify_hermes_job", unexpected_certification)
    with pytest.raises(HarborRolloutError, match="Harbor runtime changed after plan creation"):
        _read(plan, job)
    after = {path: path.read_bytes() for root in (plan, job) for path in root.rglob("*") if path.is_file()}
    assert after == before
    assert len(calls) == 1


def test_legacy_plan_without_runtime_metadata_keeps_read_and_execute_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    from traceforge.harbor_ags.rollout import execute_rollout_plan

    plan, job, calls = _fixture(tmp_path, monkeypatch, runtime=True)
    # 合成旧计划重新绑定其元数据；不修改任何真实执行记录。
    plan_path = plan / "rollout_plan.json"
    payload = json.loads(plan_path.read_text())
    payload.pop("harbor_runtime")
    _write(plan_path, payload)
    manifest_path = plan / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["files"]:
        if entry["relative_path"] == "rollout_plan.json":
            entry["sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    _write(manifest_path, manifest)
    receipt_path = plan / "run_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["artifact_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    _write(receipt_path, receipt)
    (tmp_path / "harbor/src/harbor_ags/validator.py").unlink()
    monkeypatch.setenv("AGS_API_KEY", "synthetic-key")
    monkeypatch.setenv("TOKENHUB_KEY", "synthetic-key")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert execute_rollout_plan(plan)["status"] == "COMPLETED"
    assert _read(plan, job)["acceptance"]["status"] == "PASS"
    assert len(calls) == 1


@pytest.mark.parametrize("deleted", [False, True])
def test_bundle_export_remains_independent_of_current_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deleted: bool,
) -> None:
    from traceforge.harbor_ags.rollout import publish_rollout_bundle

    plan, _, calls = _fixture(tmp_path, monkeypatch, runtime=True)
    runtime = tmp_path / "harbor/src/harbor_ags/validator.py"
    if deleted:
        runtime.unlink()
    else:
        runtime.write_text("# 已修改的合成 runtime fixture\n", encoding="utf-8")
    destination = publish_rollout_bundle(plan, tmp_path / "exported_bundle")
    payload = json.loads((plan / "rollout_plan.json").read_text())
    task = plan / "dataset" / payload["dataset"]["task_relative_paths"][0]
    expected = {path.relative_to(task): path.read_bytes() for path in task.rglob("*") if path.is_file()}
    actual = {path.relative_to(destination / "task"): path.read_bytes()
              for path in (destination / "task").rglob("*") if path.is_file()}
    assert actual == expected
    manifest = json.loads((destination / "artifact_manifest.json").read_text())
    assert manifest["execution_status"] == "NOT_ASSERTED"
    assert calls == []
