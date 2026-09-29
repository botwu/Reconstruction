"""验证有界修复只补足任务初态，并保留真实的失败与原始证据。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction import pipeline as pipeline
from traceforge.reconstruction.terminal_universe_environment import ReplayResult, ReplayedFile
from traceforge.reconstruction.workspace_completion import repair_workspace_completion


class RepairAgent:
    backend = "hermes-sandbox"
    model_name = "fixture"

    def __init__(self, files=None):
        self.files = files or []
        self.instruction = ""
        self.session = None

    def run(self, *, role, instruction, session, output_root):
        self.instruction = instruction
        self.session = session
        return AgentResult(
            role=role.name, backend=self.backend, completed=True,
            payload={"candidates": [{
                "files": self.files,
                "uncertainties": [], "decision": "READY",
            }], "open_questions": []},
        )


def repair_seed(tmp_path):
    workspace = tmp_path / "previous" / "workspace"
    workspace.mkdir(parents=True)
    original = "def value():\n    return 1\n"
    support = "import original\n"
    (workspace / "original.py").write_text(original)
    (workspace / "support.py").write_text(support)
    seed = {
        "index": 0, "decision": "READY", "workspace": str(workspace),
        "env_root": str(workspace.parent), "dependencies": ["python"],
        "runtime_constraints": [], "uncertainties": [],
        "file_provenance": [{
            "path": "support.py", "provenance": "MODEL_COMPLETED",
            "evidence_ref_ids": ["c1"],
        }],
        "manifest": {"provenance": {
            "original.py": {"kind": "REPLAYED", "content_sha256": hashlib.sha256(original.encode()).hexdigest()},
            "support.py": {"kind": "MODEL_COMPLETED", "evidence_ref_ids": ["c1"],
                           "content_sha256": hashlib.sha256(support.encode()).hexdigest()},
        }},
    }
    replay = ReplayResult((ReplayedFile(
        path="original.py", content=original, first_observation_event_id="c1",
        completeness="COMPLETE",
    ),), (), (), ())
    return seed, replay


def repair(tmp_path, agent, seed, replay):
    return repair_workspace_completion(
        task={"task_instruction": "审查 original.py 并输出结论", "core_objective": "审查源码"},
        replay=replay, timeline=[{"call_id": "c1", "result_text": "import original"}],
        agent=agent, output_root=tmp_path / "repair", candidate=seed,
        feedback={"missing_context": ["缺少支持文件的上下文"]}, env_origin="REPLAYED",
    )


def test_repair_preserves_prior_files_provenance_and_original_observations(tmp_path):
    seed, replay = repair_seed(tmp_path)
    agent = RepairAgent([{
        "path": "support.py", "content": "import original\nCONFIG = 1\n",
        "evidence_ref_ids": ["c1"], "provenance": "MODEL_COMPLETED",
    }])
    result = repair(tmp_path, agent, seed, replay)
    assert result["status"] == "READY"
    workspace = Path(result["candidates"][0]["workspace"])
    assert (workspace / "support.py").read_text().endswith("CONFIG = 1\n")
    assert (workspace / "original.py").read_text() == replay.files[0].content
    assert (Path(seed["workspace"]) / "support.py").read_text() == "import original\n"
    assert result["candidates"][0]["dependencies"] == ["python"]
    assert agent.session.replay_files["support.py"] == "import original\n"
    assert agent.session.protected_paths == {"original.py"}
    assert "缺少支持文件的上下文" in agent.instruction
    assert result["candidates"][0]["manifest"]["provenance"]["support.py"]["kind"] == "MODEL_COMPLETED"


@pytest.mark.parametrize("changed", ["original.py", "support.py"])
def test_repair_refuses_changed_seed_snapshot(tmp_path, changed):
    seed, replay = repair_seed(tmp_path)
    (Path(seed["workspace"]) / changed).write_text("被外部篡改\n")
    agent = RepairAgent()
    result = repair(tmp_path, agent, seed, replay)
    assert result["status"] == "REVIEW"
    assert any("COMPLETION_SEED_CHANGED" in code for code in result["errors"])
    assert agent.session is None


@pytest.mark.parametrize("path,refs,error", [
    ("original.py", ["c1"], "PROTECTED_FILE_OVERWRITE"),
    ("support.py", ["unknown"], "EVIDENCE_REF_UNKNOWN"),
])
def test_repair_never_relaxes_original_evidence_policy(tmp_path, path, refs, error):
    seed, replay = repair_seed(tmp_path)
    result = repair(tmp_path, RepairAgent([{
        "path": path, "content": "VALUE = 2\n", "evidence_ref_ids": refs,
        "provenance": "MODEL_COMPLETED",
    }]), seed, replay)
    assert result["status"] == "REVIEW"
    assert any(error in code for code in result["errors"])


def test_repair_keeps_untouched_completed_context(tmp_path):
    seed, replay = repair_seed(tmp_path)
    result = repair(tmp_path, RepairAgent(), seed, replay)
    workspace = Path(result["candidates"][0]["workspace"])
    assert (workspace / "support.py").read_text() == "import original\n"
    assert result["candidates"][0]["file_provenance"] == seed["file_provenance"]


def _feedback_case(tmp_path, monkeypatch, *, context="REVIEW", execute="FAILED",
                   repair="READY", failed_probe=False, progressing=False,
                   ready_after=1, max_repair_rounds=2):
    seed, replay = repair_seed(tmp_path)
    calls = {"judge": [], "repair": []}
    task = {"task_id": "one", "task_instruction": "审查 original.py", "core_objective": "源码审查"}
    initial = {"status": context, "execution_readiness": execute,
               "execution_errors": ["ENVIRONMENT_PROBES_REQUIRED"],
               "workspace_sha256": "before", "blockers": [], "errors": []}
    settled = {"status": "READY", "execution_readiness": "PROBED",
               "workspace_sha256": "after", "execution_errors": [], "blockers": [], "errors": []}

    def judge(**kwargs):
        calls["judge"].append(kwargs)
        success = len(calls["judge"]) > ready_after and repair == "READY"
        return {
            "label": "SUFFICIENT" if success or context == "READY" else "INSUFFICIENT",
            "decision": "READY" if success or context == "READY" else "REVIEW",
            "status": "READY" if success or context == "READY" else "REVIEW",
            "missing_context": [] if success or context == "READY" else ["缺少支持配置"],
            "environment_probes": [{
                "purpose": "load", "status": "FAIL", "code_sha256": "load-code",
                "executions": [{"exit_code": 1, "stderr": "ModuleNotFoundError: support"}],
            }] if failed_probe and not success else [],
            "reason": "需要 task-start 配置", "errors": [],
        }

    def contract(**kwargs):
        if len(calls["judge"]) > ready_after and repair == "READY":
            return settled
        return {**initial, "workspace_sha256": str(len(calls["judge"]))} if progressing else initial

    def fix(**kwargs):
        calls["repair"].append(kwargs)
        if repair == "REVIEW":
            return {"status": "REVIEW", "candidates": [], "errors": ["EVIDENCE_REF_UNKNOWN:support.py"],
                    "open_questions": ["没有依据可以恢复必要配置"]}
        return {"status": "READY", "candidates": [seed], "errors": []}

    monkeypatch.setattr(pipeline, "run_workspace_sufficiency", judge)
    monkeypatch.setattr(pipeline, "build_environment_contract", contract)
    monkeypatch.setattr(pipeline, "repair_workspace_completion", fix)
    result = pipeline._judge_and_repair_candidate(
        task=task, candidate=seed, replay=replay, timeline=[], task_source={},
        agent=RepairAgent(), task_root=tmp_path / "task", index=0, origin="REPLAYED",
        max_repair_rounds=max_repair_rounds,
    )
    return result, calls, seed


def test_sufficiency_gap_returns_to_completion_then_rejudges(tmp_path, monkeypatch):
    (candidate, judge, env, audit), calls, seed = _feedback_case(tmp_path, monkeypatch)
    assert len(calls["repair"]) == 1
    assert len(calls["judge"]) == 2
    assert calls["repair"][0]["candidate"] == seed
    assert calls["repair"][0]["feedback"]["missing_context"] == ["缺少支持配置"]
    assert candidate["workspace"] == seed["workspace"]
    assert env["execution_readiness"] == "PROBED"
    assert audit["stop_reason"] == "READY"


def test_researcher_can_continue_past_two_repairs_while_making_progress(tmp_path, monkeypatch):
    (_, _, environment, audit), calls, _ = _feedback_case(
        tmp_path, monkeypatch, ready_after=4, progressing=True, max_repair_rounds=None,
    )
    assert len(calls["repair"]) == 4 and len(calls["judge"]) == 5
    assert environment["execution_readiness"] == "PROBED"
    assert audit["stop_reason"] == "READY"


def test_ready_candidate_never_enters_repair(tmp_path, monkeypatch):
    (_, _, _, audit), calls, _ = _feedback_case(
        tmp_path, monkeypatch, context="READY", execute="PROBED",
    )
    assert len(calls["judge"]) == 1
    assert not calls["repair"]
    assert audit["stop_reason"] == "READY"


def test_missing_probes_only_rejudges_without_rewriting_workspace(tmp_path, monkeypatch):
    (_, _, env, audit), calls, _ = _feedback_case(
        tmp_path, monkeypatch, context="READY", execute="FAILED",
    )
    assert len(calls["judge"]) == 2
    assert not calls["repair"]
    assert calls["judge"][1]["repair_feedback"]["execution_errors"]
    assert env["execution_readiness"] == "PROBED"
    assert audit["rounds"][1]["action"] == "RECHECK"


def test_unrepairable_candidate_exits_with_original_and_completion_reason(tmp_path, monkeypatch):
    (_, judge, env, audit), calls, seed = _feedback_case(tmp_path, monkeypatch, repair="REVIEW")
    assert len(calls["repair"]) == 1
    assert len(calls["judge"]) == 1
    assert env["status"] == "REVIEW"
    assert judge["missing_context"] == ["缺少支持配置"]
    assert audit["stop_reason"] == "COMPLETION_REVIEW"
    assert audit["rounds"][-1]["completion_errors"] == ["EVIDENCE_REF_UNKNOWN:support.py"]


def test_infrastructure_failure_never_rewrites_environment(tmp_path, monkeypatch):
    (_, _, env, audit), calls, _ = _feedback_case(tmp_path, monkeypatch, context="INFRA_ERROR")
    assert len(calls["judge"]) == 1
    assert not calls["repair"]
    assert env["status"] == "INFRA_ERROR"
    assert audit["stop_reason"] == "INFRA_ERROR"


def test_unchanged_diagnosis_stops_without_exhausting_all_retries(tmp_path, monkeypatch):
    (_, _, env, audit), calls, _ = _feedback_case(tmp_path, monkeypatch, repair="UNCHANGED")
    assert len(calls["judge"]) == 2
    assert len(calls["repair"]) == 1
    assert env["status"] == "REVIEW"
    assert audit["stop_reason"] == "NO_PROGRESS"


@pytest.mark.parametrize("confirmed_gap", [False, True])
def test_failed_probe_requires_a_confirmed_gap_before_rewriting(tmp_path, monkeypatch, confirmed_gap):
    (_, _, env, audit), calls, _ = _feedback_case(
        tmp_path, monkeypatch, context="REVIEW" if confirmed_gap else "READY", failed_probe=True,
    )
    assert len(calls["repair"]) == int(confirmed_gap)
    feedback = calls["repair"][0]["feedback"] if confirmed_gap else calls["judge"][1]["repair_feedback"]
    assert feedback["failed_probes"][0]["executions"][0]["stderr"] == (
        "ModuleNotFoundError: support"
    )
    assert audit["rounds"][1]["action"] == ("REPAIR" if confirmed_gap else "RECHECK")
    assert len(calls["judge"]) == 2
    assert env["execution_readiness"] == "PROBED"
    assert audit["stop_reason"] == "READY"


def test_changing_but_insufficient_candidates_stop_at_two_repair_rounds(tmp_path, monkeypatch):
    (_, _, env, audit), calls, _ = _feedback_case(
        tmp_path, monkeypatch, repair="UNCHANGED", progressing=True,
    )
    assert len(calls["repair"]) == 2
    assert len(calls["judge"]) == 3
    assert env["status"] == "REVIEW"
    assert audit["stop_reason"] == "REPAIR_LIMIT_REACHED"


def test_repair_retains_original_partial_excerpt_guard(tmp_path):
    seed, original = repair_seed(tmp_path)
    replay = ReplayResult((ReplayedFile(
        path="original.py", content="def value():", first_observation_event_id="c1",
        completeness="PARTIAL",
    ),), (), (), ())
    seed["file_provenance"].append({
        "path": "original.py", "evidence_ref_ids": ["c1"], "provenance": "MODEL_COMPLETED",
    })
    agent = RepairAgent([{
        "path": "original.py", "content": "def unrelated(): return 4",
        "evidence_ref_ids": ["c1"], "provenance": "MODEL_COMPLETED",
    }])
    result = repair(tmp_path, agent, seed, replay)
    assert agent.session.replay_files["original.py"] == original.files[0].content
    assert agent.session.partial_files["original.py"] == "def value():"
    assert result["status"] == "REVIEW"
    assert any("PARTIAL_OBSERVED_CONTENT_LOST" in code for code in result["errors"])


def test_malformed_repair_path_is_review_not_uncaught_exception(tmp_path):
    seed, replay = repair_seed(tmp_path)
    result = repair(tmp_path, RepairAgent([{
        "path": [], "content": "VALUE = 2",
        "evidence_ref_ids": ["c1"], "provenance": "MODEL_COMPLETED",
    }]), seed, replay)
    assert result["status"] == "REVIEW"
    assert result["errors"]


@pytest.mark.parametrize("key,old,new", [
    ("dependencies", ["numpy==1.26.4"], ["numpy==2.0.0"]),
    ("runtime_constraints", ["Python 3.11"], ["Python 3.12"]),
])
@pytest.mark.parametrize("mode", ["replace", "clear", "inherit"])
def test_repair_runtime_declarations_can_be_corrected(tmp_path, key, old, new, mode):
    seed, replay = repair_seed(tmp_path)
    seed[key] = old

    class DeclarationAgent(RepairAgent):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            candidate = result.payload["candidates"][0]
            if mode == "inherit":
                candidate.pop(key, None)
            else:
                candidate[key] = new if mode == "replace" else []
            return result

    result = repair(tmp_path, DeclarationAgent(), seed, replay)
    assert result["status"] == "READY"
    expected = old if mode == "inherit" else new if mode == "replace" else []
    assert result["candidates"][0][key] == expected
    assert (Path(seed["workspace"]) / "original.py").read_text() == replay.files[0].content
