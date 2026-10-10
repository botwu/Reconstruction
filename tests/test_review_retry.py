"""后审重试只消费已完成的原生实跑，不重复重建与解题。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_terminal_rollout_review import _evidence

from traceforge.cli import _parser
from traceforge.harbor_ags.results import read_native_trial
from traceforge.reconstruction import review_retry
from traceforge.reconstruction.agents.session import workspace_tree_hash
from traceforge.reconstruction.session_source import indexed_session
from traceforge.reconstruction.task_fit import build_task_contract
from traceforge.reconstruction.terminal_rollout_review import build_terminal_rollout_evidence


def test_retry_review_has_explicit_read_only_preflight() -> None:
    args = _parser().parse_args(
        [
            "reconstruct",
            "retry-review",
            "--from-manifest",
            "/old/reconstruction_manifest.json",
            "--task-id",
            "task-1",
            "--output",
            "/new",
            "--check-only",
        ]
    )
    assert args.check_only is True
    assert args.from_manifest == Path("/old/reconstruction_manifest.json")
    assert not hasattr(args, "execute_rollout")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def _fixture(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    native, trial_root, _, _ = _evidence(old, monkeypatch)
    harbor = Path(native["receipt"]["input_task"])
    config_path = trial_root / "config.json"
    config = json.loads(config_path.read_text())
    config["task"] = {"path": str(harbor)}
    _write(config_path, config)
    native = read_native_trial(trial_root, expected_task=harbor)
    assert native["completed"], native["errors"]
    task = {
        "task_id": "task-1",
        "task_instruction": "修改 main.py，并说明实际改动。",
        "core_objective": "修改 main.py，并说明实际改动。",
        "acceptance_obligations": [
            {
                "id": "obl-1",
                "text": "修改 main.py，并说明实际改动。",
                "verifier_kind": "FILE",
                "evidence_ref_ids": ["user:0"],
            }
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-1",
                "verifier_kind": "FILE",
                "required_paths": ["main.py"],
                "initial_required_paths": ["main.py"],
                "output_paths": [],
            }
        ],
    }
    source = {
        "input_domain": "terminal",
        "line_sha256": "a" * 64,
        "selected_task_ids": ["task-1"],
        "session_parser": {"status": "READY"},
        "raw_session": {
            "messages": [{"role": "user", "content": "修改 main.py，并说明实际改动。"}],
            "tools": [{"name": "terminal"}],
        },
        "tool_timeline": [],
    }
    _write(old / "reconstruction_source.json", source)
    indexed = indexed_session(source["raw_session"])
    prompt = json.dumps({"session": {**indexed["session_fields"], "messages": indexed["messages"]}})
    attempt = old / "session_parser/parser/attempt-0001"
    _write(attempt / "request.json", {"prompt": prompt})
    _write(
        old / "session_parser/receipt.json",
        {
            "status": "READY",
            "source_sha256": source["line_sha256"],
            "accepted_attempt": str(attempt),
            "input_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        },
    )
    _write(old / "intent/intent.json", {"tasks": [{"status": "READY", "errors": [], "task": task}]})
    plan_root = old / "plan"
    plan = {
        "run_id": "run",
        "agent": {"trials": 1},
        "domain": "terminal",
        "verifier": {"enabled": False},
    }
    _write(plan_root / "rollout_plan.json", plan)
    _write(plan_root / "run_receipt.json", {"status": "COMPLETED", "returncode": 0})
    plan.update(jobs_root=str(trial_root.parent.parent), job_name=trial_root.parent.name)
    _write(plan_root / "rollout_plan.json", plan)
    monkeypatch.setattr(review_retry, "load_verified_rollout_plan", lambda path: plan)

    def read_job(*args, **kwargs):
        current = read_native_trial(trial_root, expected_task=harbor)
        return {
            "execution_completed": current["completed"],
            "quality_gate": {"reasons": current["errors"]},
            "trials": [{"trial_name": trial_root.name, "native_trial": current}],
        }

    monkeypatch.setattr(review_retry, "read_rollout_results", read_job)
    _write(
        old / "native-rollout.json",
        {
            "plan": str(plan_root),
            "execution": {"status": "COMPLETED"},
            "errors": [],
            "results": {
                "execution_completed": True,
                "expected_trial_count": 1,
                "input_binding": {
                    "run_id": "run",
                    "plan_sha256": hashlib.sha256(
                        (plan_root / "rollout_plan.json").read_bytes()
                    ).hexdigest(),
                    "run_receipt_sha256": hashlib.sha256(
                        (plan_root / "run_receipt.json").read_bytes()
                    ).hexdigest(),
                    "trial_task_paths": {trial_root.name: str(harbor)},
                },
                "trials": [
                    {"result_path": str(trial_root / "result.json"), "native_trial": native}
                ],
            },
        },
    )
    workspace = harbor / "workspace"
    _write(
        old / "downstream_environment_review/sufficiency.json",
        {
            "workspace_hashes": workspace_tree_hash(workspace),
            "reconstruction_context": {"replay_files": [], "candidate_completed_files": []},
        },
    )
    _write(old / "rollout-evidence.json", build_terminal_rollout_evidence([native]))
    _write(old / "replay.json", {"files": [], "partial_evidence": []})
    result = {
        "task_id": "task-1",
        "status": "REVIEW",
        "stopped_at": "rollout_review",
        "acceptance": "NOT_ASSESSED",
        "sft_eligible": False,
        "errors": ["MODEL_RATE_LIMIT"],
        "task_contract": build_task_contract(task=task),
        "executed_task": task,
        "rollout_record": str(old / "native-rollout.json"),
        "completion": {"candidates": [{"workspace": str(workspace)}]},
        "selected_index": 0,
        "rollout_review": {
            "sufficiency_path": str(old / "downstream_environment_review/sufficiency.json")
        },
        "rollout_evidence_path": str(old / "rollout-evidence.json"),
    }
    manifest = old / "reconstruction_manifest.json"
    _write(manifest, {"source": {"line_sha256": source["line_sha256"]}, "tasks": [result]})
    return manifest, trial_root, tmp_path / "retry"


@pytest.mark.parametrize(
    "damage",
    [
        "source",
        "task",
        "workspace",
        "capture",
        "missing_trial",
        "outside",
        "symlink",
        "completed",
    ],
)
def test_preflight_rejects_unbound_inputs_without_agent(tmp_path, monkeypatch, damage):
    manifest, trial, output = _fixture(tmp_path, monkeypatch)
    old = manifest.parent
    if damage == "source":
        p = old / "reconstruction_source.json"
        value = json.loads(p.read_text())
        value["raw_session"]["messages"][0]["content"] = "篡改目标"
    elif damage == "task":
        p = old / "intent/intent.json"
        value = json.loads(p.read_text())
        value["tasks"][0]["task"]["core_objective"] = "篡改目标"
    elif damage == "workspace":
        result = json.loads(manifest.read_text())["tasks"][0]
        workspace = Path(result["completion"]["candidates"][0]["workspace"])
        (workspace / "main.py").write_text("changed")
        p, value = None, None
    elif damage == "capture":
        (trial / "agent/trajectory.full.json").unlink()
        p, value = None, None
    elif damage == "missing_trial":
        p = old / "native-rollout.json"
        value = json.loads(p.read_text())
        value["results"]["trials"] = []
    elif damage == "symlink":
        p = old / "session_parser/receipt.json"
        outside = tmp_path / "outside.json"
        outside.write_bytes(p.read_bytes())
        p.unlink()
        p.symlink_to(outside)
        p, value = None, None
    else:
        p = manifest
        value = json.loads(p.read_text())
        if damage == "outside":
            value["tasks"][0]["rollout_record"] = str(tmp_path / "outside.json")
        else:
            value["tasks"][0].update(status="ROLLOUT_COMPLETED", stopped_at=None)
    if p is not None:
        _write(p, value)
    with pytest.raises((ValueError, OSError), match=r"."):
        review_retry.prepare_review_retry(
            manifest_path=manifest, task_id="task-1", output_root=output
        )
    assert not output.exists()


@pytest.mark.parametrize("kind", ["complete", "rate_limit", "environment_gap"])
def test_retry_reuses_original_trials_and_never_rebuilds(tmp_path, monkeypatch, kind):
    manifest, _, output = _fixture(tmp_path, monkeypatch)
    before = workspace_tree_hash(manifest.parent)
    prepared = review_retry.prepare_review_retry(
        manifest_path=manifest, task_id="task-1", output_root=output
    )
    called = []

    def judge(**kwargs):
        called.append(kwargs)
        assert Path(kwargs["workspace_root"]).is_relative_to(output)
        assert kwargs["rollout_evidence"] == prepared["evidence"]
        assert kwargs["reconstruction_context"] == prepared["context"]
        assert kwargs["agent"].conversation.messages == []
        # 任何重建或求解调用均没有配置，替身仅能执行本次后审。
        if kind == "rate_limit":
            return {
                "status": "REVIEW",
                "label": "UNKNOWN",
                "errors": ["MODEL_RATE_LIMIT"],
                "rollout_review": {"status": "REVIEW_INCOMPLETE", "errors": ["MODEL_RATE_LIMIT"]},
            }
        return {
            "status": "READY",
            "label": "SUFFICIENT",
            "rollout_review": {
                "status": "COMPLETE",
                "errors": [],
                "requirements": [
                    {"status": "ENVIRONMENT_GAP" if kind == "environment_gap" else "SOLVER_ERROR"}
                ],
            },
        }

    monkeypatch.setattr(review_retry, "run_workspace_sufficiency", judge)
    monkeypatch.setattr(
        review_retry,
        "build_environment_contract",
        lambda **kw: {"status": "READY", "execution_readiness": "PROBED", "probes": [{}]},
    )
    result_path = review_retry.run_review_retry(
        prepared,
        agent=SimpleNamespace(model_name="test"),
        runtime_factory=lambda: pytest.fail("无探针"),
    )
    result = json.loads(result_path.read_text())
    assert len(called) == 1
    assert result["acceptance"] == "NOT_ASSESSED" and result["sft_eligible"] is False
    assert result["status"] == ("ROLLOUT_COMPLETED" if kind == "complete" else "REVIEW")
    assert result["errors"] == (
        []
        if kind == "complete"
        else ["MODEL_RATE_LIMIT" if kind == "rate_limit" else "ROLLOUT_ENVIRONMENT_GAP"]
    )
    assert workspace_tree_hash(manifest.parent) == before
    assert json.loads((output / "retry-input-audit.json").read_text())["unchanged"] is True


def test_preflight_cli_does_not_construct_clients(tmp_path, monkeypatch):
    import traceforge.cli as cli

    manifest, _, output = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "build_hermes_runtime", lambda **kw: pytest.fail("不得创建模型"))
    monkeypatch.setattr(cli, "build_ags_runtime_factory", lambda **kw: pytest.fail("不得创建 AGS"))
    assert (
        cli.main(
            [
                "reconstruct",
                "retry-review",
                "--from-manifest",
                str(manifest),
                "--task-id",
                "task-1",
                "--output",
                str(output),
                "--check-only",
            ]
        )
        == 0
    )
    assert not output.exists()


@pytest.mark.parametrize("decision", ["COMPLETE", "REPAIR", "BLOCKED"])
def test_search_retry_restores_sources_without_completion_or_rollout(
    tmp_path, monkeypatch, decision
):
    from test_search_checkpoint import recorded_network

    from traceforge.reconstruction import search_environment
    from traceforge.reconstruction.agents.session import AgentConversation, AgentSession

    network, _, _ = recorded_network(tmp_path / "web", monkeypatch)
    source = {"line_sha256": "bound", "raw_session": {"messages": []}, "tool_timeline": []}
    task = {"task_id": "search", "task_instruction": "分析材料"}
    messages = [
        {
            "role": "user",
            "content": json.dumps({"SOURCE_SESSION": indexed_session(source["raw_session"])}),
        }
    ]
    checkpoint = search_environment.save_search_checkpoint(
        source=source,
        task=task,
        session=AgentSession(conversation=AgentConversation(messages=messages)),
        network=network,
        output_root=tmp_path / "original",
    )
    before = workspace_tree_hash(checkpoint.parent)
    trials = [{"trial": "already-completed"}]
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        assert kwargs["native_trials"] == trials
        assert kwargs["session"].conversation.messages == messages
        assert kwargs["network"].pages["https://example.org/paper"]["text"] == "前段正文与未读尾部"
        return {
            "decision": decision,
            **(
                {"failure_kind": "AGENT_FAILURE", "errors": ["MODEL_RATE_LIMIT"]}
                if decision == "BLOCKED"
                else {}
            ),
        }

    monkeypatch.setattr(search_environment, "_review_search_rollouts", review)
    for name in (
        "_complete_search_environment",
        "run_native_unassessed_rollouts",
        "run_search_rollouts",
    ):
        monkeypatch.setattr(search_environment, name, lambda **kw: pytest.fail("不得重建或求解"))
    result = search_environment.retry_search_rollout_review(
        source=source,
        task=task,
        environment={"task": task},
        checkpoint=checkpoint,
        native_trials=trials,
        agent=object(),
        output_root=tmp_path / "retry",
    )
    assert len(calls) == 1
    assert (
        result["status"]
        == {"COMPLETE": "ROLLOUT_COMPLETED", "REPAIR": "REVIEW", "BLOCKED": "BLOCKED"}[decision]
    )
    assert result["acceptance"] == "NOT_ASSESSED" and result["sft_eligible"] is False
    if decision == "BLOCKED":
        assert result["environment_review"] == "REVIEW_INCOMPLETE"
        assert result["errors"] == ["MODEL_RATE_LIMIT"]
    assert workspace_tree_hash(checkpoint.parent) == before


def test_unclosed_native_job_is_not_a_reconstruction_gap(tmp_path, monkeypatch):
    manifest, _, output = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        review_retry,
        "read_rollout_results",
        lambda *args, **kwargs: {
            "execution_completed": False,
            "quality_gate": {"reasons": ["SANDBOX_CLEANUP_UNCONFIRMED"]},
        },
    )
    with pytest.raises(ValueError, match="清理认证失败"):
        review_retry.prepare_review_retry(
            manifest_path=manifest, task_id="task-1", output_root=output
        )
    assert not output.exists()


def test_prepared_input_change_is_rejected_before_review(tmp_path, monkeypatch):
    manifest, _, output = _fixture(tmp_path, monkeypatch)
    prepared = review_retry.prepare_review_retry(
        manifest_path=manifest, task_id="task-1", output_root=output
    )
    source = manifest.parent / "reconstruction_source.json"
    source.write_bytes(source.read_bytes() + b" ")
    monkeypatch.setattr(
        review_retry,
        "run_workspace_sufficiency",
        lambda **kw: pytest.fail("改变的输入不能交给模型"),
    )
    with pytest.raises(ValueError, match="启动前输入改变"):
        review_retry.run_review_retry(
            prepared,
            agent=SimpleNamespace(model_name="test"),
            runtime_factory=lambda: pytest.fail("不得创建 AGS"),
        )
    assert not output.exists()


def test_retry_preserves_original_initial_state_blocker(tmp_path, monkeypatch):
    manifest, _, output = _fixture(tmp_path, monkeypatch)
    _write(
        manifest.parent / "replay.json",
        {
            "files": [],
            "partial_evidence": [
                {
                    "path": "main.py",
                    "reason": "read_after_unparsed_mutation",
                    "source_event_id": "event-1",
                }
            ],
        },
    )
    prepared = review_retry.prepare_review_retry(
        manifest_path=manifest, task_id="task-1", output_root=output
    )
    monkeypatch.setattr(
        review_retry,
        "run_workspace_sufficiency",
        lambda **kw: {
            "status": "READY",
            "label": "SUFFICIENT",
            "task": prepared["task"],
            "errors": [],
            "rollout_review": {"status": "COMPLETE", "errors": [], "requirements": []},
        },
    )
    result_path = review_retry.run_review_retry(
        prepared,
        agent=SimpleNamespace(model_name="test"),
        runtime_factory=lambda: pytest.fail("无探针"),
    )
    result = json.loads(result_path.read_text())
    assert result["status"] == "REVIEW"
    assert result["errors"][0] == "INITIAL_ENVIRONMENT_REVIEW_UNRESOLVED"
    assert result["acceptance"] == "NOT_ASSESSED"
