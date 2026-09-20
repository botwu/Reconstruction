
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).parents[1] / "scripts" / "check_reconstruct_e2e.py"
_SPEC = importlib.util.spec_from_file_location("check_reconstruct_e2e", _SCRIPT)
assert _SPEC and _SPEC.loader
_CHECK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CHECK)

_SCHEMA = "traceforge.harbor-ags-rollout-results.v1"
_TEST_HASH = "a" * 64


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _red_attempt() -> dict:
    def run(status: str, reward: float) -> dict:
        return {
            "verifier_test_sha256": _TEST_HASH,
            "results": {
                "schema_version": _SCHEMA,
                "trials": [{"status": status, "reward": reward}],
                "quality_gate": {"ok": True},
            },
        }

    return {
        "status": "PASS",
        "verifier_test_sha256": _TEST_HASH,
        "initial_red": {"passed": True, "errors": [], "verdict_count": 1},
        "failed_cases": [],
        "runs": {
            "round-01-nop": run("FAIL", 0.0),
            "round-01-oracle-00": run("PASS", 1.0),
            "round-01-mutation-00": run("FAIL", 0.0),
        },
    }


def _rollout_payload(count: int, quality: bool = True) -> dict:
    return {
        "schema_version": _SCHEMA,
        "trial_count": count,
        "trials": [{"status": "PASS", "reward": 1.0} for _ in range(count)],
        "cleanup": {"ok": True},
        "quality_gate": {"ok": quality, "reasons": [] if quality else ["bad"]},
    }


def _make_root(
    tmp_path: Path,
    *,
    red: bool = True,
    rollout_count: int = 2,
    nested_rollout: bool = False,
) -> Path:
    root = tmp_path / "run"
    task_id = "task_terminal"
    verification = {
        "status": "READY",
        "calibration_runs": [_red_attempt()] if red else [],
    }
    task = {
        "task_id": task_id,
        "selected_index": 0,
        "execution_support_route": {"route": "TERMINAL_FILE"},
        "verification": verification,
        "completion": {"status": "READY"},
    }
    manifest = {
        "status": "READY",
        "tasks": [task],
        "intent": {
            "status": "READY",
            "tasks": [
                {
                    "status": "READY",
                    "task": {
                        "task_id": task_id,
                        "task_instruction": "完成终端任务",
                        "acceptance_obligations": [],
                    },
                    "agent": {"backend": "hermes", "completed": True},
                    "errors": [],
                }
            ],
            "errors": [],
        },
    }
    _write_json(root / "reconstruction_manifest.json", manifest)
    _write_json(root / "tasks" / task_id / "replay.json", {"files": [{"path": "main.c"}]})
    (root / "tasks" / task_id / "initial_workspace").mkdir(parents=True)
    _write_json(
        root / "tasks" / task_id / "completion" / "completion.json",
        {
            "status": "READY",
            "agent": {
                "backend": "hermes-sandbox",
                "completed": True,
                "skipped": False,
            },
            "candidates": [],
            "errors": [],
        },
    )
    _write_json(
        root / "tasks" / task_id / "sufficiency" / "000" / "sufficiency.json",
        {
            "status": "READY",
            "label": "SUFFICIENT",
            "decision": "READY",
            "errors": [],
        },
    )
    _write_json(
        root / "tasks" / task_id / "verification" / "verification.json",
        verification,
    )
    rollout = _rollout_payload(rollout_count)
    if nested_rollout:
        verification["rollout"] = {"results": rollout}
        _write_json(
            root / "tasks" / task_id / "verification" / "verification.json",
            verification,
        )
        manifest["tasks"][0]["verification"] = verification
        _write_json(root / "reconstruction_manifest.json", manifest)
    else:
        _write_json(root / "rollout" / "results.json", rollout)
    return root


def _task(root: Path) -> dict:
    return json.loads((root / "reconstruction_manifest.json").read_text())["tasks"][0]


def test_terminal_single_file_and_real_red_can_be_ready(tmp_path: Path) -> None:
    report = _CHECK.grade(
        _make_root(tmp_path), config=None, channel="claude", expected_task_id=None
    )

    assert report["pipeline_ok"] is True
    assert report["delivery_ready"] is True
    assert report["end_to_end_ready"] is True
    assert report["stages"][0]["replay_file_count"] == 1


def test_nested_rollout_in_verification_is_accepted(tmp_path: Path) -> None:
    report = _CHECK.grade(
        _make_root(tmp_path, nested_rollout=True),
        config=None,
        channel="claude",
        expected_task_id=None,
    )

    assert report["end_to_end_ready"] is True


def test_ready_verdict_without_red_evidence_is_not_delivery_ready(tmp_path: Path) -> None:
    report = _CHECK.grade(
        _make_root(tmp_path, red=False),
        config=None,
        channel="claude",
        expected_task_id=None,
    )

    assert report["pipeline_ok"] is False
    assert report["delivery_ready"] is False
    red = next(stage for stage in report["stages"] if stage["stage"] == "red")
    assert red["detail"] == "RED_EVIDENCE_MISSING"


def test_fake_red_run_shape_is_rejected(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    task = _task(root)
    task["verification"]["calibration_runs"][0]["runs"] = {"fake": {}}
    _write_json(root / "reconstruction_manifest.json", {
        **json.loads((root / "reconstruction_manifest.json").read_text()),
        "tasks": [task],
    })
    _write_json(
        root / "tasks" / task["task_id"] / "verification" / "verification.json",
        task["verification"],
    )

    report = _CHECK.grade(root, config=None, channel="claude", expected_task_id=None)

    assert report["delivery_ready"] is False


def test_completion_does_not_inherit_task_errors(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    task = _task(root)
    task["errors"] = ["AGENT_INCOMPLETE"]
    _write_json(root / "reconstruction_manifest.json", {
        **json.loads((root / "reconstruction_manifest.json").read_text()),
        "tasks": [task],
    })

    stage = _CHECK._check_completion(root, task["task_id"], task)

    assert stage["ok"] is True
    assert "AGENT_INCOMPLETE" not in stage["detail"]


def test_ready_completion_with_errors_is_rejected(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    task_id = _task(root)["task_id"]
    completion_path = root / "tasks" / task_id / "completion" / "completion.json"
    completion = json.loads(completion_path.read_text())
    completion["errors"] = ["unexpected"]
    _write_json(completion_path, completion)

    report = _CHECK.grade(root, config=None, channel="claude", expected_task_id=None)

    assert report["pipeline_ok"] is False


def test_review_selected_sufficiency_is_rejected(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    task_id = _task(root)["task_id"]
    path = root / "tasks" / task_id / "sufficiency" / "000" / "sufficiency.json"
    payload = json.loads(path.read_text())
    payload.update(status="REVIEW", label="INSUFFICIENT", decision="REVIEW")
    _write_json(path, payload)

    report = _CHECK.grade(root, config=None, channel="claude", expected_task_id=None)

    assert report["pipeline_ok"] is False


def test_end_to_end_requires_two_rollout_passes_and_quality(tmp_path: Path) -> None:
    report = _CHECK.grade(
        _make_root(tmp_path, rollout_count=1),
        config=None,
        channel="claude",
        expected_task_id=None,
    )

    assert report["pipeline_ok"] is True
    assert report["delivery_ready"] is True
    assert report["end_to_end_ready"] is False


def test_review_sufficiency_artifact_without_selected_index_is_reported(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    manifest = json.loads((root / "reconstruction_manifest.json").read_text())
    task = manifest["tasks"][0]
    task.pop("selected_index")
    path = root / "tasks" / task["task_id"] / "sufficiency" / "000" / "sufficiency.json"
    payload = json.loads(path.read_text())
    payload.update(status="REVIEW", label="INSUFFICIENT", decision="REVIEW")
    _write_json(path, payload)
    _write_json(root / "reconstruction_manifest.json", manifest)

    report = _CHECK.grade(root, config=None, channel="claude", expected_task_id=None)

    sufficiency = next(stage for stage in report["stages"] if stage["stage"] == "sufficiency")
    assert sufficiency["status"] == "REVIEW"
    assert sufficiency["detail"] == "SUFFICIENCY_NOT_READY"
