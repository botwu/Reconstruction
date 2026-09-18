"""Harbor Job 结果读取和质量门禁测试。"""

import json
import hashlib
from pathlib import Path

from traceforge.harbor_ags.results import read_rollout_results


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_ledger(path: Path, sandbox_id: str = "a") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "event": "created", "sandbox_id": sandbox_id})
        + "\n"
        + json.dumps({"schema_version": 1, "event": "killed", "sandbox_id": sandbox_id})
        + "\n",
        encoding="utf-8",
    )


def _write_valid_hermes_artifacts(trial: Path) -> None:
    trajectory = {"schema_version": "traceforge-lossless-trajectory-v1", "messages": [{"role": "user", "content": "fixture"}, {"role": "assistant", "content": "done"}]}
    _write(trial / "agent/trajectory.full.json", trajectory)
    artifact = trial / "artifacts/output.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("fixture\n", encoding="utf-8")
    _write(
        trial / "artifacts/manifest.json",
        {
            "schema_version": "traceforge-harbor-artifacts/v1",
            "files": [{"path": "output.txt", "size_bytes": 8, "sha256": hashlib.sha256(b"fixture\n").hexdigest()}],
        },
    )
    _write(trial / "reconstruction-certification.json", {
        "schema_version": "traceforge.rollout-certification.v1", "certified": True,
        "trajectory_sha256": hashlib.sha256((trial / "agent/trajectory.full.json").read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256((trial / "artifacts/manifest.json").read_bytes()).hexdigest(),
    })


def test_results_do_not_count_missing_verdict_as_success(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "task--trial-001"
    _write(
        trial / "result.json",
        {
            "verifier_result": {"rewards": {"task": 1.0}},
            "agent_result": {"n_input_tokens": 10, "n_output_tokens": 3},
        },
    )
    report = read_rollout_results(job)
    assert report["metrics"]["pass_rate"] == 0.0
    assert report["trials"][0]["status"] == "INCONCLUSIVE"
    assert report["quality_gate"]["ok"] is False


def test_results_calculate_pass_tokens_duration_and_cleanup(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "task--trial-001"
    _write(
        trial / "result.json",
        {
            "verifier_result": {"rewards": {"task": 1.0}},
            "agent_result": {"n_input_tokens": 10, "n_cache_tokens": 2, "n_output_tokens": 3},
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:05Z",
        },
    )
    _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
    _write_valid_hermes_artifacts(trial)
    _write_ledger(job / "_control/ags-sandbox-ledger.jsonl")
    report = read_rollout_results(job)
    assert report["quality_gate"]["ok"] is True
    assert report["metrics"]["pass_rate"] == 1.0
    assert report["metrics"]["trajectory_capture_rate"] == 1.0
    assert report["metrics"]["total_tokens"] == {"input": 10, "cache": 2, "output": 3}
    assert report["metrics"]["mean_duration_seconds"] == 5.0


def test_results_accept_ags_killed_ledger_without_stopped(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "task--trial-001"
    _write(
        trial / "result.json",
        {
            "verifier_result": {"rewards": {"task": 1.0}},
            "agent_result": {"n_input_tokens": 1, "n_output_tokens": 1},
        },
    )
    _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
    _write_valid_hermes_artifacts(trial)
    _write_ledger(job / "_control/ags-sandbox-ledger.jsonl")
    report = read_rollout_results(job)
    assert report["cleanup"]["ok"] is True
    assert report["quality_gate"]["ok"] is True


def test_results_reject_stopped_only_ledger(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "task--trial-001"
    _write(
        trial / "result.json",
        {"verifier_result": {"rewards": {"task": 1.0}}, "agent_result": {}},
    )
    _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
    _write(trial / "agent/trajectory.full.json", {})
    _write(trial / "artifacts/manifest.json", {"files": []})
    ledger = job / "_control/ags-sandbox-ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"schema_version": 1, "event": "created", "sandbox_id": "a"})
        + "\n"
        + json.dumps({"schema_version": 1, "event": "stopped", "sandbox_id": "a"})
        + "\n",
        encoding="utf-8",
    )
    report = read_rollout_results(job)
    assert report["cleanup"]["ok"] is False
    assert "SANDBOX_CLEANUP_UNCONFIRMED" in report["quality_gate"]["reasons"]


def test_results_reject_pass_without_artifact_manifest(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "task--trial-001"
    _write(
        trial / "result.json",
        {"verifier_result": {"rewards": {"task": 1.0}}, "agent_result": {}},
    )
    _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
    _write(trial / "agent/trajectory.full.json", {})
    _write_ledger(job / "_control/ags-sandbox-ledger.jsonl")
    report = read_rollout_results(job)
    assert report["trials"][0]["status"] == "PASS"
    assert report["quality_gate"]["ok"] is False
    assert "ARTIFACT_MANIFEST_MISSING" in report["quality_gate"]["reasons"]


def test_oracle_quality_gate_does_not_require_artifact_or_trajectory(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "task--trial-001"
    _write(
        trial / "result.json",
        {"verifier_result": {"rewards": {"task": 1.0}}, "agent_result": {}},
    )
    _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
    _write_ledger(job / "_control/ags-sandbox-ledger.jsonl")
    report = read_rollout_results(job, agent_mode="oracle")
    assert report["trials"][0]["status"] == "PASS"
    assert report["quality_gate"]["ok"] is True
    assert report["quality_gate"]["reasons"] == []


def test_results_count_trial_directory_without_result_as_infra_error(tmp_path: Path) -> None:
    job = tmp_path / "job"
    (job / "task--trial-001").mkdir(parents=True)
    report = read_rollout_results(job, expected_trial_count=2)
    assert report["trial_count"] == 2
    assert all(item["status"] == "INFRA_ERROR" for item in report["trials"])
    assert report["quality_gate"]["ok"] is False
    assert "TRIAL_COUNT_MISMATCH" in report["quality_gate"]["reasons"]
    assert "TRIAL_INCOMPLETE_OR_INFRA_ERROR" in report["quality_gate"]["reasons"]


def test_results_reject_extra_trials(tmp_path: Path) -> None:
    job = tmp_path / "job"
    for name in ("task--trial-001", "task--trial-002"):
        trial = job / name
        _write(
            trial / "result.json",
            {"verifier_result": {"rewards": {"task": 1.0}}, "agent_result": {}},
        )
        _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
        _write(trial / "agent/trajectory.full.json", {})
        _write(trial / "artifacts/manifest.json", {"files": []})
    _write_ledger(job / "_control/ags-sandbox-ledger.jsonl")
    report = read_rollout_results(job, expected_trial_count=1)
    assert report["trial_count"] == 2
    assert report["quality_gate"]["ok"] is False
    assert "TRIAL_COUNT_MISMATCH" in report["quality_gate"]["reasons"]


def test_invalid_trajectory_and_stale_certification_are_rejected(tmp_path):
    job = tmp_path / "job"
    trial = job / "trial"
    _write(trial / "result.json", {"verifier_result": {"rewards": {"task": 1.0}}})
    _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
    _write_valid_hermes_artifacts(trial)
    _write_ledger(job / "_control/ags-sandbox-ledger.jsonl")
    (trial / "agent/trajectory.full.json").write_text("NOT VALID JSON")
    report = read_rollout_results(job)
    assert report["quality_gate"]["ok"] is False
    assert "TRAJECTORY_INVALID_JSON" in report["trials"][0]["content_errors"]
    assert "HERMES_CERTIFICATION_STALE" in report["trials"][0]["content_errors"]


def test_artifact_hash_mismatch_blocks_training_gate(tmp_path):
    job = tmp_path / "job"
    trial = job / "trial"
    _write(trial / "result.json", {"verifier_result": {"rewards": {"task": 1.0}}})
    _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
    _write_valid_hermes_artifacts(trial)
    _write_ledger(job / "_control/ags-sandbox-ledger.jsonl")
    (trial / "artifacts/output.txt").write_text("changed")
    report = read_rollout_results(job)
    assert report["quality_gate"]["ok"] is False
    assert "ARTIFACT_MANIFEST_HASH_MISMATCH" in report["trials"][0]["content_errors"]


def test_failed_validator_is_persisted_and_blocks_gate(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from traceforge.harbor_ags.results import certify_hermes_job
    job = tmp_path / "job"
    trial = job / "trial"
    _write(trial / "result.json", {"verifier_result": {"rewards": {"task": 1.0}}})
    _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
    _write_valid_hermes_artifacts(trial)
    _write_ledger(job / "_control/ags-sandbox-ledger.jsonl")
    monkeypatch.setitem(sys.modules, "harbor_ags.artifacts", SimpleNamespace(build_artifact_manifest=lambda path: None))
    monkeypatch.setitem(sys.modules, "harbor_ags.validator", SimpleNamespace(validate_harbor_trial=lambda path: SimpleNamespace(certified=False, status="INFRA_CAPTURE")))
    certify_hermes_job(job)
    receipt = json.loads((trial / "reconstruction-certification.json").read_text())
    assert receipt["certified"] is False
    report = read_rollout_results(job)
    assert report["quality_gate"]["ok"] is False
    assert "HERMES_CERTIFICATION_FAILED" in report["trials"][0]["content_errors"]
