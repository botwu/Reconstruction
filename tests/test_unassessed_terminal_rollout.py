"""未评分 terminal 复用正式执行边界，不借 domain 生成评分通过。"""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from test_harbor_ags_rollout import _harbor_root
from test_search_harbor_rollout import _native_trial
from traceforge.harbor_task import workspace_snapshot_hook
from traceforge.harbor_ags.adapter import HarborAgsAdapterError
from traceforge.harbor_ags.results import read_rollout_results, rollout_passed
from traceforge.harbor_ags.rollout import (
    HarborRolloutConfig, HarborRolloutError, build_rollout_plan,
    load_verified_rollout_plan, publish_rollout_bundle,
)
from traceforge.workspace_snapshot import collect_workspace


def _terminal_task(root):
    task = root / "task"
    (task / "workspace").mkdir(parents=True)
    (task / "environment").mkdir()
    (task / "workspace/main.py").write_text("print('initial')\n")
    (task / "instruction.md").write_text("修改 main.py，并说明实际改动。\n")
    (task / "task.toml").write_text(
        'schema_version = "1.4"\n[task]\nname = "traceforge/terminal"\n'
        '[metadata]\ndomain = "terminal"\nresponse_acceptance = "NOT_ASSESSED"\n'
        '[environment]\nos = "linux"\n'
    )
    with (task / "task.toml").open("a") as stream:
        stream.write(workspace_snapshot_hook(task))
    _bind_delivery(task)
    return task


def _bind_delivery(task):
    hashes = {str(p.relative_to(task)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in task.rglob("*") if p.is_file()}
    (task.parent / "delivery.json").write_text(json.dumps({
        "schema_version": "traceforge.harbor-delivery.v1", "domain": "terminal",
        "response_acceptance": "NOT_ASSESSED", "task_path": "task",
        "rollout_args": ["--disable-verification"], "task_file_sha256": hashes,
    }))


def _plan(tmp_path):
    return build_rollout_plan(HarborRolloutConfig(
        task_dir=_terminal_task(tmp_path / "source"),
        harbor_root=_harbor_root(tmp_path / "harbor"),
        output_root=tmp_path / "plans", jobs_root=tmp_path / "jobs", trials=2,
    ))


def test_terminal_unassessed_plan_keeps_domain_and_disables_only_grader(tmp_path):
    plan_dir = _plan(tmp_path)
    plan = load_verified_rollout_plan(plan_dir)
    assert plan["domain"] == "terminal"
    assert plan["verifier"]["enabled"] is False
    assert plan["verifier"]["response_acceptance"] == "NOT_ASSESSED"
    config = yaml.safe_load((plan_dir / "harbor-config.yaml").read_text())
    assert config["verifier"]["disable"] is True
    assert "SearchAGS" not in config["environment"].get("import_path", "")
    dataset = Path(plan["dataset"]["dataset_root"])
    for relative in plan["dataset"]["task_relative_paths"]:
        task = dataset / relative
        assert not (task / "tests").exists()
        assert not (task / "solution").exists()
        assert "TraceForge workspace snapshot hook v2" in (task / "task.toml").read_text()
    bundle = publish_rollout_bundle(plan_dir, tmp_path / "published")
    assert (bundle / "delivery.json").read_bytes() == (dataset / "delivery.json").read_bytes()


def test_terminal_delivery_tampering_rejected(tmp_path):
    task = _terminal_task(tmp_path / "source")
    (task / "workspace/main.py").write_text("forged")
    with pytest.raises(HarborAgsAdapterError, match="哈希"):
        build_rollout_plan(HarborRolloutConfig(
            task_dir=task, harbor_root=_harbor_root(tmp_path / "harbor"),
            output_root=tmp_path / "plans", jobs_root=tmp_path / "jobs",
        ))


def _terminal_trial(tmp_path, monkeypatch):
    task = _terminal_task(tmp_path / "source")
    trial, raw = _native_trial(tmp_path, monkeypatch)
    raw["task_input"] = {
        "instruction": {"task_instruction": {"content": (task / "instruction.md").read_text()}},
        "workspace": {"files": [{"path": "main.py", "sha256": hashlib.sha256(
            (task / "workspace/main.py").read_bytes()).hexdigest()}]},
    }
    (trial / "config.json").write_text(json.dumps({"task": {"path": str(task)}}))
    (trial / "agent/trajectory.full.json").write_text(json.dumps(raw))
    (trial / "agent/task-input.json").write_text(json.dumps(raw["task_input"]))
    modified = tmp_path / "modified"
    modified.mkdir()
    (modified / "main.py").write_text("print('changed')\n")
    collect_workspace(modified, trial / "artifacts/logs/artifacts/traceforge/workspace",
                      initial_paths=["main.py"], output_paths=[])
    ledger = trial.parent / "_control/ags-sandbox-ledger.jsonl"
    ledger.parent.mkdir()
    ledger.write_text("{}")
    return task, trial, raw


def test_unassessed_terminal_accepts_real_modification_without_reward(tmp_path, monkeypatch, harbor_cleanup):
    task, trial, _ = _terminal_trial(tmp_path, monkeypatch)
    report = read_rollout_results(
        trial.parent, domain="terminal", verifier_enabled=False,
        expected_task=task, expected_trial_count=1,
    )
    assert report["execution_completed"]
    assert report["acceptance"] == "NOT_ASSESSED"
    assert report["sft_eligible"] is False
    assert report["trials"][0]["status"] == "COMPLETED"
    assert report["trials"][0]["reward"] is None
    assert report["metrics"]["pass_rate"] is None
    assert not rollout_passed({"execution": {"status": "COMPLETED"}, "results": report}, 1)


@pytest.mark.parametrize("failure", ["initial", "collection", "final", "cleanup"])
def test_unassessed_terminal_never_skips_evidence_binding(tmp_path, monkeypatch, harbor_cleanup, failure):
    task, trial, raw = _terminal_trial(tmp_path, monkeypatch)
    if failure == "initial":
        raw["task_input"]["workspace"]["files"][0]["sha256"] = "0" * 64
        (trial / "agent/trajectory.full.json").write_text(json.dumps(raw))
    elif failure == "collection":
        (trial / "artifacts/logs/artifacts/traceforge/workspace-collection.json").unlink()
    elif failure == "final":
        (trial / "artifacts/logs/artifacts/traceforge/workspace/main.py").write_text("forged")
    else:
        harbor_cleanup.ok = False
    report = read_rollout_results(
        trial.parent, domain="terminal", verifier_enabled=False,
        expected_task=task, expected_trial_count=1,
    )
    assert not report["execution_completed"]
    assert not report["quality_gate"]["ok"]
    assert report["trials"][0]["reward"] is None



def test_plan_cannot_relabel_unassessed_delivery_as_scored(tmp_path):
    plan_dir = _plan(tmp_path)
    plan_path = plan_dir / "rollout_plan.json"
    plan = json.loads(plan_path.read_text())
    plan["verifier"]["enabled"] = True
    plan_path.write_text(json.dumps(plan))
    manifest_path = plan_dir / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["files"]:
        if entry["relative_path"] == "rollout_plan.json":
            entry["sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(HarborRolloutError, match="评分状态"):
        load_verified_rollout_plan(plan_dir)


def test_unassessed_trial_with_reward_is_not_accepted(tmp_path, monkeypatch, harbor_cleanup):
    task, trial, _ = _terminal_trial(tmp_path, monkeypatch)
    p = trial / "result.json"
    result = json.loads(p.read_text())
    result["verifier_result"] = {"rewards": {"task": 1.0}}
    p.write_text(json.dumps(result))
    report = read_rollout_results(
        trial.parent, verifier_enabled=False, expected_task=task, expected_trial_count=1,
    )
    assert report["trials"][0]["reward"] is None
    assert not report["execution_completed"]
    assert "NATIVE_UNASSESSED_GRADER_PRESENT" in report["trials"][0]["content_errors"]
