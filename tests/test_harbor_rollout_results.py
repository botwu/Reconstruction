"""Harbor Job 结果读取和质量门禁测试。"""

import json
from pathlib import Path

from traceforge.harbor_ags.results import read_rollout_results


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


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
    _write(trial / "agent/trajectory.full.json", {})
    _write(trial / "artifacts/manifest.json", {"files": []})
    _write(job / "_control/ags-sandbox-ledger.jsonl", {})
    ledger = job / "_control/ags-sandbox-ledger.jsonl"
    ledger.write_text(
        '{"event":"created","sandbox_id":"a"}\n'
        '{"event":"stopped","sandbox_id":"a"}\n',
        encoding="utf-8",
    )
    report = read_rollout_results(job)
    assert report["quality_gate"]["ok"] is True
    assert report["metrics"]["pass_rate"] == 1.0
    assert report["metrics"]["trajectory_capture_rate"] == 1.0
    assert report["metrics"]["total_tokens"] == {"input": 10, "cache": 2, "output": 3}
    assert report["metrics"]["mean_duration_seconds"] == 5.0


def test_results_reject_pass_without_artifact_manifest(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "task--trial-001"
    _write(
        trial / "result.json",
        {"verifier_result": {"rewards": {"task": 1.0}}, "agent_result": {}},
    )
    _write(trial / "verifier/verdict.json", {"status": "TASK_PASS"})
    _write(trial / "agent/trajectory.full.json", {})
    ledger = job / "_control/ags-sandbox-ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        '{"event":"created","sandbox_id":"a"}\n'
        '{"event":"stopped","sandbox_id":"a"}\n',
        encoding="utf-8",
    )
    report = read_rollout_results(job)
    assert report["trials"][0]["status"] == "PASS"
    assert report["quality_gate"]["ok"] is False
    assert "ARTIFACT_MANIFEST_MISSING" in report["quality_gate"]["reasons"]
