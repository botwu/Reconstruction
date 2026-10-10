"""原生 search 复用 Harbor 冻结与执行，不能伪造文件评分通过。"""

import copy
import hashlib
import json
import subprocess
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_harbor_ags_rollout import _harbor_root

from traceforge.harbor_ags.adapter import HarborAgsAdapterError
from traceforge.harbor_ags.results import read_rollout_results, read_native_trial, rollout_passed
from traceforge.harbor_ags.rollout import (
    HarborRolloutConfig,
    HarborRolloutError,
    build_rollout_plan,
    execute_rollout_plan,
    load_verified_rollout_plan,
    publish_rollout_bundle,
)
from traceforge.harbor_task import export_search_task


def _task(root):
    return export_search_task({
        "schema_version": "traceforge.search-environment.v5", "status": "READY",
        "errors": [], "missing_inputs": [], "requires_live_web": False,
        "task": {"task_id": "search-1", "task_instruction": "比较原始资料。",
                 "source_task": {"user_texts": ["比较原始资料。"]}},
        "captures": [{"evidence_ref_id": "captured:0", "result_text": "原始正文"}],
        "live_references": [], "context_messages": [], "limitations": [],
    }, root)


def _plan(tmp_path):
    return build_rollout_plan(HarborRolloutConfig(
        task_dir=_task(tmp_path / "source"),
        harbor_root=_harbor_root(tmp_path / "harbor"),
        output_root=tmp_path / "plans", jobs_root=tmp_path / "jobs", trials=2,
    ))


def test_search_plan_preserves_delivery_and_disables_only_grading(tmp_path):
    plan_dir = _plan(tmp_path)
    plan = load_verified_rollout_plan(plan_dir)
    assert plan["domain"] == "search"
    assert plan["command"][-1] == "--yes"
    assert plan["verifier"]["enabled"] is False
    assert plan["verifier"]["response_acceptance"] == "NOT_ASSESSED"
    config = yaml.safe_load((plan_dir / "harbor-config.yaml").read_text())
    assert config["verifier"]["disable"] is True
    assert config["agents"][0]["import_path"] == "traceforge.harbor_ags.agent:GatewayHermesAgent"
    assert set(plan["harbor_runtime"]["gateway_files"]) == {"agent.py", "gateway_harness.py"}
    assert config["environment"]["import_path"].endswith(":SearchAGSEnvironment")
    dataset = Path(plan["dataset"]["dataset_root"])
    delivery = json.loads((dataset / "delivery.json").read_text())
    for relative in plan["dataset"]["task_relative_paths"]:
        task = dataset / relative
        assert not (task / "solution").exists()
        assert hashlib.sha256((task / "task.toml").read_bytes()).hexdigest() == (
            delivery["task_file_sha256"]["task.toml"])
    published = publish_rollout_bundle(plan_dir, tmp_path / "published")
    assert (published / "delivery.json").read_bytes() == (dataset / "delivery.json").read_bytes()



@pytest.mark.parametrize("suffix, valid", [([], True), (["--yes"], True), (["--yes", "--quiet"], False)])
def test_plan_accepts_only_exact_legacy_or_noninteractive_command(tmp_path, suffix, valid):
    plan_dir = _plan(tmp_path)
    plan_path = plan_dir / "rollout_plan.json"
    plan = json.loads(plan_path.read_text())
    plan["command"] = [*plan["command"][:-1], *suffix]
    plan_path.write_text(json.dumps(plan))
    manifest_path = plan_dir / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["files"]:
        if item["relative_path"] == "rollout_plan.json":
            item["sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    if valid:
        assert load_verified_rollout_plan(plan_dir)["command"] == plan["command"]
    else:
        with pytest.raises(HarborRolloutError, match="绑定"):
            load_verified_rollout_plan(plan_dir)


def test_search_delivery_tampering_is_rejected_before_plan(tmp_path):
    task = _task(tmp_path / "source")
    (task / "workspace/evidence.json").write_text("{}")
    with pytest.raises(HarborAgsAdapterError, match="哈希"):
        build_rollout_plan(HarborRolloutConfig(
            task_dir=task, harbor_root=_harbor_root(tmp_path / "harbor"),
            output_root=tmp_path / "plans", jobs_root=tmp_path / "jobs",
        ))


def test_frozen_search_delivery_receipt_cannot_change(tmp_path):
    plan_dir = _plan(tmp_path)
    plan = load_verified_rollout_plan(plan_dir)
    (Path(plan["dataset"]["dataset_root"]) / "delivery.json").write_text("{}")
    with pytest.raises(HarborRolloutError, match="delivery.json"):
        load_verified_rollout_plan(plan_dir)


def test_search_credentials_are_only_in_execution_environment(tmp_path, monkeypatch):
    plan_dir = _plan(tmp_path)
    private = tmp_path / "search.json"
    private.write_text(json.dumps({"serper_api_key": "fixture-serper", "jina_api_key": "fixture-jina"}))
    monkeypatch.setenv("TRACEFORGE_SEARCH_CONFIG", str(private))
    monkeypatch.setenv("AGS_API_KEY", "fixture-ags")
    monkeypatch.setenv("TOKENHUB_KEY", "fixture-model")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)

    def run(command, **kwargs):
        assert kwargs["env"]["SERPER_API_KEY"] == "fixture-serper"
        assert kwargs["env"]["JINA_API_KEY"] == "fixture-jina"
        assert "fixture-serper" not in " ".join(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("traceforge.harbor_ags.rollout.subprocess.run", run)
    assert execute_rollout_plan(plan_dir)["status"] == "COMPLETED"
    for path in plan_dir.rglob("*"):
        if path.is_file():
            assert b"fixture-serper" not in path.read_bytes()
            assert b"fixture-jina" not in path.read_bytes()


def _native_trial(tmp_path, monkeypatch):
    trial = tmp_path / "job/trial"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    raw = {
        "schema_version": "traceforge-lossless-trajectory-v1", "model": "fixture-model",
        "messages": [], "metadata": {}, "anthropic_calls": [
            {"response": {"content": [
                {"type": "tool_use", "id": "t1", "name": "terminal", "input": {"command": "read"}}]},
             "request": {"messages": []}},
            {"response": {"content": [{"type": "text", "text": "有出处的回答。"}]},
             "request": {"messages": [{"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1", "content": "正文" * 1000}]}]}},
        ],
    }
    (trial / "result.json").write_text(json.dumps({"finished_at": "2026-10-02T00:00:00Z"}))
    (agent / "trajectory.full.json").write_text(json.dumps(raw))
    for name in ("trajectory.json", "task-input.json", "workspace-initial-manifest.json"):
        (agent / name).write_text("{}")
    for name in ("anthropic-exchanges.jsonl", "anthropic-sse.jsonl", "hermes-session.jsonl"):
        (agent / name).write_text("{}\n")
    monkeypatch.setitem(sys.modules, "harbor_ags.evidence", SimpleNamespace(
        build_full_trajectory=lambda *args, **kwargs: copy.deepcopy(raw),
        reconcile_evidence=lambda *args, **kwargs: {"ok": True},
    ))
    return trial, raw


def test_native_reader_preserves_complete_actual_results(tmp_path, monkeypatch):
    trial, raw = _native_trial(tmp_path, monkeypatch)
    read = read_native_trial(trial, domain="search")
    assert read["completed"] and read["errors"] == []
    assert read["answer"] == "有出处的回答。"
    assert read["tool_events"][0]["result"] == "正文" * 1000
    assert read["tool_events"][0]["name"] == "terminal"
    assert read["receipt"]["acceptance"] == "NOT_ASSESSED"
    assert read["receipt"]["source_binding"] is False
    assert "agent/anthropic-exchanges.jsonl" in read["receipt"]["evidence_files"]
    changed = copy.deepcopy(raw)
    changed["anthropic_calls"][0]["response"]["content"][0]["input"] = {"command": "forged"}
    (trial / "agent/trajectory.full.json").write_text(json.dumps(changed))
    rejected = read_native_trial(trial, domain="search")
    assert not rejected["completed"]
    assert rejected["errors"] == ["NATIVE_CAPTURE_BINDING_MISMATCH:anthropic_calls"]


def test_native_reader_rejects_missing_actual_tool_result(tmp_path, monkeypatch):
    trial, raw = _native_trial(tmp_path, monkeypatch)
    raw["anthropic_calls"][1]["request"]["messages"] = []
    (trial / "agent/trajectory.full.json").write_text(json.dumps(raw))
    assert read_native_trial(trial, domain="search")["errors"] == ["NATIVE_TOOL_RESULT_MISSING:t1"]



@pytest.mark.parametrize("stop_reason", ["max_tokens", "length", "model_context_window_exceeded"])
def test_native_reader_rejects_truncated_answer_and_keeps_evidence(tmp_path, monkeypatch, stop_reason):
    trial, raw = _native_trial(tmp_path, monkeypatch)
    raw["anthropic_calls"][-1]["response"]["stop_reason"] = stop_reason
    (trial / "agent/trajectory.full.json").write_text(json.dumps(raw))
    read = read_native_trial(trial, domain="search")
    assert not read["completed"]
    assert read["errors"] == ["NATIVE_FINAL_RESPONSE_TRUNCATED"]
    assert read["answer"] == "有出处的回答。"
    assert read["final_stop_reason"] == stop_reason
    assert len(read["tool_events"]) == 1
    assert "agent/anthropic-exchanges.jsonl" in read["receipt"]["evidence_files"]


def test_search_result_is_complete_but_never_scored_as_pass(tmp_path, monkeypatch, harbor_cleanup):
    trial, raw = _native_trial(tmp_path, monkeypatch)
    task = _task(tmp_path / "source")
    raw["task_input"] = {
        "instruction": {"task_instruction": {"content": (task / "instruction.md").read_text()}},
        "workspace": {"files": [
            {"path": str(p.relative_to(task / "workspace")),
             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in (task / "workspace").rglob("*") if p.is_file()
        ]},
    }
    (trial / "config.json").write_text(json.dumps({"task": {"path": str(task)}}))
    (trial / "agent/trajectory.full.json").write_text(json.dumps(raw))
    final = trial / "artifacts/home/user/workspace"
    shutil.copytree(task / "workspace", final)
    ledger = trial.parent / "_control/ags-sandbox-ledger.jsonl"
    ledger.parent.mkdir()
    ledger.write_text("{}")
    report = read_rollout_results(trial.parent, domain="search", verifier_enabled=False, expected_trial_count=1)
    assert report["execution_completed"] is True
    assert report["quality_gate"] == {"scope": "EXECUTION", "ok": True, "reasons": []}
    assert report["trials"][0]["status"] == "COMPLETED"
    assert report["trials"][0]["reward"] is None
    assert report["metrics"]["pass_rate"] is None
    assert report["acceptance"] == "NOT_ASSESSED"
    assert report["trials"][0]["native_trial"]["receipt"]["source_binding"]
    assert report["trials"][0]["native_trial"]["receipt"]["collection"] == "NOT_PRESENT"
    assert not rollout_passed({"execution": {"status": "COMPLETED"}, "results": report}, 1)
    harbor_cleanup.ok = False
    incomplete = read_rollout_results(trial.parent, domain="search", verifier_enabled=False, expected_trial_count=1)
    assert not incomplete["execution_completed"]
    assert "SANDBOX_CLEANUP_UNCONFIRMED" in incomplete["quality_gate"]["reasons"]
    harbor_cleanup.ok = True
    (final / "evidence.json").write_text("{}")
    changed = read_rollout_results(
        trial.parent, domain="search", verifier_enabled=False, expected_trial_count=1,
    )
    assert not changed["execution_completed"]
    assert "NATIVE_SEARCH_MATERIAL_CHANGED" in changed["trials"][0]["content_errors"]


def test_gateway_runtime_is_bound_but_old_plans_remain_readable(tmp_path):
    from traceforge.harbor_ags.rollout import validate_rollout_runtime

    plan = load_verified_rollout_plan(_plan(tmp_path))
    validate_rollout_runtime(plan)
    plan["harbor_runtime"]["gateway_files"]["gateway_harness.py"] = "0" * 64
    with pytest.raises(HarborRolloutError, match="Gateway Hermes"):
        validate_rollout_runtime(plan)
    del plan["harbor_runtime"]["gateway_files"]
    validate_rollout_runtime(plan)


@pytest.mark.parametrize("cache_state", ["missing", "present", "outside"])
def test_native_reader_only_exposes_recovered_web_cache(tmp_path, monkeypatch, cache_state):
    trial, _ = _native_trial(tmp_path, monkeypatch)
    cache = trial / "artifacts/logs/artifacts/search"
    if cache_state != "missing":
        cache.parent.mkdir(parents=True)
        if cache_state == "outside":
            outside = tmp_path / "unrelated-cache"
            outside.mkdir()
            cache.symlink_to(outside, target_is_directory=True)
        else:
            cache.mkdir()
            (cache / "calls.jsonl").write_text("{}\n")
            _web_collection(trial)
    result = read_native_trial(trial, domain="search")
    assert result["web_cache_root"] == (str(cache) if cache_state == "present" else None)
    assert result["completed"] is (cache_state != "outside")
    if cache_state == "outside":
        assert result["errors"] == ["NATIVE_WEB_CACHE_PATH_INVALID"]


def _web_collection(trial):
    manifest = trial / "artifacts/manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps([{
        "source": "/logs/artifacts", "destination": "artifacts/logs/artifacts",
        "type": "directory", "status": "ok", "service": None,
    }]))
    return manifest


def _web_cache(trial):
    cache = trial / "artifacts/logs/artifacts/search"
    cache.mkdir(parents=True)
    (cache / "calls.jsonl").write_text('{"tool":"web_open","text":"完整缓存正文"}\n')
    (cache / "page.raw").write_bytes(b"complete source")
    (cache / "page.pdf").write_bytes(b"%PDF-source")
    return cache, _web_collection(trial)


def test_native_reader_binds_collected_cache_without_replacing_visible_result(
    tmp_path, monkeypatch,
):
    trial, _ = _native_trial(tmp_path, monkeypatch)
    cache, manifest = _web_cache(trial)
    before = {p.relative_to(trial).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in trial.rglob("*") if p.is_file()}
    result = read_native_trial(trial, domain="search")
    assert result["completed"], result["errors"]
    assert result["web_cache_root"] == str(cache)
    assert result["tool_events"][0]["result"] == "正文" * 1000
    files = result["receipt"]["evidence_files"]
    for path in (manifest, *cache.iterdir()):
        name = path.relative_to(trial).as_posix()
        assert files[name] == before[name]
    assert {p.relative_to(trial).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in trial.rglob("*") if p.is_file()} == before
    (cache / "page.raw").write_bytes(b"changed source")
    reread = read_native_trial(trial, domain="search")
    name = "artifacts/logs/artifacts/search/page.raw"
    assert reread["receipt"]["evidence_files"][name] != files[name]
    assert reread["tool_events"] == result["tool_events"]


@pytest.mark.parametrize("failure", [
    "missing", "failed", "destination", "source", "duplicate", "service", "object",
])
def test_native_reader_rejects_unbound_cache_collection(tmp_path, monkeypatch, failure):
    trial, _ = _native_trial(tmp_path, monkeypatch)
    _, manifest = _web_cache(trial)
    rows = json.loads(manifest.read_text())
    if failure == "missing":
        manifest.unlink()
    else:
        if failure == "failed":
            rows[0]["status"] = "failed"
        elif failure == "destination":
            rows[0]["destination"] = "artifacts/other"
        elif failure == "source":
            rows[0]["source"] = "/other"
        elif failure == "duplicate":
            rows.append(dict(rows[0]))
        elif failure == "service":
            rows[0]["service"] = "other"
        else:
            rows = {"files": []}
        manifest.write_text(json.dumps(rows))
    result = read_native_trial(trial, domain="search")
    assert not result["completed"]
    assert result["web_cache_root"] is None
    assert result["errors"] == [
        "NATIVE_WEB_CACHE_COLLECTION_MISSING" if failure == "missing"
        else "NATIVE_WEB_CACHE_COLLECTION_INVALID"
    ]


@pytest.mark.parametrize("failure", ["file_symlink", "parent_symlink", "manifest_symlink", "fifo"])
def test_native_reader_rejects_unsafe_collected_cache(tmp_path, monkeypatch, failure):
    trial, _ = _native_trial(tmp_path, monkeypatch)
    cache, manifest = _web_cache(trial)
    if failure == "file_symlink":
        (cache / "alias.raw").symlink_to(cache / "page.raw")
    elif failure == "parent_symlink":
        original = cache.parent
        moved = trial / "moved"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    elif failure == "manifest_symlink":
        target = tmp_path / "manifest.json"
        manifest.rename(target)
        manifest.symlink_to(target)
    else:
        import os

        os.mkfifo(cache / "unsafe.pipe")
    result = read_native_trial(trial, domain="search")
    assert not result["completed"]
    assert result["web_cache_root"] is None
    assert result["errors"][0].startswith(("NATIVE_WEB_CACHE_", "FILE_SNAPSHOT_UNSAFE"))


def test_native_reader_preserves_ordered_conversation_and_capture_boundary(tmp_path, monkeypatch):
    trial, raw = _native_trial(tmp_path, monkeypatch)
    messages = [
        {"role": "user", "content": "原任务"},
        {"role": "assistant", "content": "中间中文计划", "reasoning": "保留原有推理字段",
         "_anthropic_call_index": 0,
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "terminal", "arguments": '{"command":"read"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "原工具正文"},
        {"role": "assistant", "content": "原页笔记", "_anthropic_call_index": 1},
        {"role": "assistant", "content": "有出处的回答。"},
    ]
    raw.update(messages=messages, system_prompt="原系统", tools=[{"name": "terminal"}])
    warnings = [{"code": "CAPTURE_CALLS_COMPACTED", "count": 1, "tool_call_ids": ["older"]}]
    reconciliation = {"ok": True, "issues": [], "warnings": warnings}
    monkeypatch.setattr(sys.modules["harbor_ags.evidence"], "reconcile_evidence",
                        lambda *a, **k: copy.deepcopy(reconciliation))
    path = trial / "agent/trajectory.full.json"
    path.write_text(json.dumps(raw))
    result = read_native_trial(trial, domain="search")
    assert result["completed"], result["errors"]
    trajectory = result["trajectory"]
    assert trajectory["messages"] == messages
    assert trajectory["system_prompt"] == raw["system_prompt"]
    assert trajectory["tools"] == raw["tools"]
    assert trajectory["reconciliation"] == reconciliation
    assert trajectory["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert trajectory["sha256"] == result["receipt"]["evidence_files"]["agent/trajectory.full.json"]
    changed = copy.deepcopy(raw)
    changed["messages"][1]["content"] = "伪造计划"
    path.write_text(json.dumps(changed))
    rejected = read_native_trial(trial, domain="search")
    assert not rejected["completed"]
    assert rejected["errors"] == ["NATIVE_CAPTURE_BINDING_MISMATCH:messages"]

@pytest.mark.parametrize("system_prompt", ["", None, []])
def test_native_reader_preserves_legal_empty_conversation_fields(
    tmp_path, monkeypatch, system_prompt,
):
    trial, raw = _native_trial(tmp_path, monkeypatch)
    raw.update(messages=[], system_prompt=system_prompt, tools=[])
    (trial / "agent/trajectory.full.json").write_text(json.dumps(raw))
    result = read_native_trial(trial, domain="search")
    assert result["completed"], result["errors"]
    for field in ("messages", "system_prompt", "tools"):
        assert result["trajectory"][field] == raw[field]


def test_native_reader_is_stable_across_revalidation_times(tmp_path, monkeypatch):
    trial, raw = _native_trial(tmp_path, monkeypatch)
    raw.update(messages=[], system_prompt="", tools=[])
    (trial / "agent/trajectory.full.json").write_text(json.dumps(raw))
    times = iter(["2026-10-10T12:43:30Z", "2026-10-10T12:45:00Z"])
    monkeypatch.setattr(
        sys.modules["harbor_ags.evidence"], "reconcile_evidence",
        lambda *args, **kwargs: {
            "ok": True, "issues": [], "warnings": [], "generated_at": next(times),
        },
    )
    first = read_native_trial(trial, domain="search")
    second = read_native_trial(trial, domain="search")
    assert first["completed"] and second["completed"]
    assert first == second
