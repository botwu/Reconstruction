"""完整诊断进入返修，真实探针进展与随机说明噪声分开判断。"""

import copy
import json
from pathlib import Path

import pytest
from test_completion_feedback import RepairAgent, repair_seed

from traceforge.reconstruction import pipeline as pipeline
from traceforge.reconstruction.workspace_completion import repair_workspace_completion


def _probe(purpose: str, round_index: int, *, status: str = "PASS") -> dict:
    return {
        "probe_id": f"{purpose}-{round_index}", "purpose": purpose, "status": status,
        "code_sha256": f"code-{purpose}", "reason": f"本轮说明 {round_index}",
        "executions": [{"exit_code": 0 if status == "PASS" else 1,
                        "stdout": '{"exists": false}', "stderr": ""}],
    }


@pytest.mark.parametrize("with_failed_probe", [False, True])
def test_complete_feedback_reaches_completion_without_reinterpreting_receipts(
    tmp_path: Path, with_failed_probe: bool,
):
    seed, replay = repair_seed(tmp_path)
    agent = RepairAgent()
    probes = [_probe("load", 0)]
    if with_failed_probe:
        probes.append(_probe("reset", 0, status="FAIL"))
    judge = {
        "status": "REVIEW", "semantic_errors": ["JUDGE_DIAGNOSIS"],
        "environment_probes": probes,
        "environment_checks": [{"kind": "load", "probe_ids": ["load-0"],
                                "reason": "退出码为零但输出显示路径不存在，仍需复查"}],
        "integrity_report": {"issues": [{"id": "integrity-001", "path": "support.py",
                                        "classification": "IRRELEVANT",
                                        "classification_reason": "任务相关性需要进一步核实"}]},
    }
    environment = {
        "status": "REVIEW", "errors": ["RECONSTRUCTABILITY_EXCEPTION_UNGROUNDED:integrity-001"],
        "execution_readiness": "FAILED", "execution_errors": ["ENVIRONMENT_PROBES_REQUIRED"],
    }
    original = copy.deepcopy((judge, environment))
    feedback = pipeline._environment_feedback(judge, environment)
    repair_workspace_completion(
        task={"task_instruction": "审查 original.py", "core_objective": "源码审查"},
        replay=replay, timeline=[{"call_id": "c1", "result_text": "import original"}],
        agent=agent, output_root=tmp_path / "repair", candidate=seed, feedback=feedback,
    )
    received = json.loads(agent.instruction.rsplit("REPAIR_FEEDBACK:\n", 1)[1])
    assert received["context_errors"] == environment["errors"]
    assert received["semantic_errors"] == ["JUDGE_DIAGNOSIS"]
    assert received["environment_probes"] == probes
    assert received["environment_checks"] == judge["environment_checks"]
    assert received["integrity_report"] == judge["integrity_report"]
    assert received["failed_probes"] == ([probes[1]] if with_failed_probe else [])
    assert received["environment_probes"][0]["status"] == "PASS"
    assert received["context_status"] == "REVIEW"
    assert received["execution_readiness"] == "FAILED"
    assert (judge, environment) == original
    assert (Path(seed["workspace"]) / "support.py").read_text() == "import original\n"


@pytest.mark.parametrize("mode,expected_calls,expected_repairs,stop_reason", [
    ("probe_progress", 3, 0, "READY"),
    ("probe_noise", 2, 0, "NO_PROGRESS"),
    ("context_progress", 3, 2, "REPAIR_LIMIT_REACHED"),
])
def test_repair_loop_distinguishes_new_diagnostics_from_probe_noise(
    tmp_path: Path, monkeypatch, mode: str,
    expected_calls: int, expected_repairs: int, stop_reason: str,
):
    seed, replay = repair_seed(tmp_path)
    calls = {"judge": [], "repair": []}

    def judge(**kwargs):
        calls["judge"].append(kwargs)
        round_index = len(calls["judge"]) - 1
        purposes = ["load", "reset", "dependency"]
        purposes = purposes[:round_index + 1] if mode == "probe_progress" else ["load"]
        return {
            "status": "REVIEW" if mode == "context_progress" else "READY",
            "label": "INSUFFICIENT" if mode == "context_progress" else "SUFFICIENT", "errors": [],
            "environment_probes": [_probe(purpose, round_index) for purpose in purposes],
        }

    def contract(**kwargs):
        round_index = len(calls["judge"]) - 1
        complete = mode == "probe_progress" and round_index == 2
        return {
            "status": "REVIEW" if mode == "context_progress" else "READY",
            "execution_readiness": "PROBED" if complete else "FAILED",
            "execution_errors": [] if complete else ["ENVIRONMENT_PROBES_REQUIRED"],
            "errors": [f"CONTEXT_ISSUE:{round_index}"] if mode == "context_progress" else [],
            "workspace_sha256": "same-workspace",
        }

    def repair(**kwargs):
        calls["repair"].append(kwargs)
        return {"status": "READY", "candidates": [seed], "errors": []}

    monkeypatch.setattr(pipeline, "run_workspace_sufficiency", judge)
    monkeypatch.setattr(pipeline, "build_environment_contract", contract)
    monkeypatch.setattr(pipeline, "repair_workspace_completion", repair)
    _, _, environment, audit = pipeline._judge_and_repair_candidate(
        task={"task_id": "one", "task_instruction": "审查 original.py"},
        candidate=seed, replay=replay, timeline=[], task_source={},
        agent=RepairAgent(), task_root=tmp_path / "task", index=0, origin="REPLAYED",
    )
    assert len(calls["judge"]) == expected_calls
    assert len(calls["repair"]) == expected_repairs
    assert audit["stop_reason"] == stop_reason
    if mode == "probe_progress":
        prior = calls["judge"][2]["repair_feedback"]["environment_probes"]
        assert [probe["purpose"] for probe in prior] == ["load", "reset"]
        assert environment["execution_readiness"] == "PROBED"
    else:
        assert environment["execution_readiness"] == "FAILED"


def test_progress_state_ignores_probe_order_duplicates_ids_and_explanations():
    environment = {"workspace_sha256": "same", "status": "READY",
                   "execution_errors": ["ENVIRONMENT_PROBES_REQUIRED"], "errors": []}
    first = pipeline._environment_feedback(
        {"environment_probes": [_probe("load", 0), _probe("reset", 0)]}, environment,
    )
    repeated = pipeline._environment_feedback(
        {"environment_probes": [_probe("reset", 1), _probe("load", 1), _probe("load", 2)]},
        environment,
    )
    assert pipeline._repair_state(environment, first) == pipeline._repair_state(
        environment, repeated,
    )
