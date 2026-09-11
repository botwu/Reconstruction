"""单条失败轨迹的完整重建闭环编排。

该模块只编排已经独立实现并测试过的阶段：任务恢复、环境补全、充分性
判定、验证器合成、Harbor bundle、RED-check 对照运行、Hermes rollout
以及 SFT 质量门禁。任何模型/沙盒调用都通过显式参数触发。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.curation.sft import CurationThresholds, curate_candidate
from traceforge.harbor_ags.results import HarborResultError, read_rollout_results
from traceforge.harbor_ags.rollout import (
    HarborRolloutConfig,
    build_rollout_plan,
    execute_rollout_plan,
)
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.verifier.bundle import compile_bundle
from traceforge.verifier.red_check import RedCheckCase, evaluate_red_check
from traceforge.verifier.synthesis import synthesize_verifier

from .environment_completion import run_environment_completion
from .sufficiency_judge import run_sufficiency_judge
from .task_recovery import run_task_recovery

WORKFLOW_SCHEMA = "traceforge.reconstruction-workflow.v2"


class ReconstructionWorkflowError(RuntimeError):
    """单条重建无法安全进入下一阶段。"""


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReconstructionWorkflowError(f"artifact 根节点必须是对象：{path}")
    return value


def _ready_candidate(payload: dict[str, Any], key: str) -> dict[str, Any]:
    candidates = payload.get(key, [])
    if not isinstance(candidates, list):
        raise ReconstructionWorkflowError(f"{key} 不是数组")
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("decision") == "READY":
            return candidate
    raise ReconstructionWorkflowError(f"{key} 没有 READY 候选")


def _latest_job_dir(jobs_root: Path, before: set[Path]) -> Path | None:
    """从 Harbor jobs root 找到本次新建、包含 result.json 的 job。"""
    candidates: list[Path] = []
    if not jobs_root.is_dir():
        return None
    for result in jobs_root.rglob("result.json"):
        job = result.parent.parent
        if job not in before:
            candidates.append(job)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime_ns)


def _run_plan_and_read(plan: Path, jobs_root: Path, *, agent_mode: str) -> dict[str, Any]:
    before = {p for p in jobs_root.iterdir()} if jobs_root.is_dir() else set()
    execution = execute_rollout_plan(plan)
    if execution.get("status") != "COMPLETED":
        return {"execution": execution, "results": None, "job_dir": None}
    job_dir = _latest_job_dir(jobs_root, before)
    if job_dir is None:
        return {
            "execution": execution,
            "results": None,
            "job_dir": None,
            "error": "HARBOR_JOB_DIR_NOT_FOUND",
        }
    try:
        results = read_rollout_results(job_dir, agent_mode=agent_mode)
    except HarborResultError as exc:
        return {
            "execution": execution,
            "results": None,
            "job_dir": str(job_dir),
            "error": f"RESULT_READ_ERROR:{exc}",
        }
    return {"execution": execution, "results": results, "job_dir": str(job_dir)}


def _red_case(
    label: str, run: dict[str, Any], expected_status: str, expected_reward: float
) -> RedCheckCase:
    results = run.get("results") or {}
    trials = results.get("trials") if isinstance(results, dict) else []
    first = trials[0] if isinstance(trials, list) and trials else {}
    status = str(first.get("status", "INCONCLUSIVE"))
    reward = first.get("reward")
    reward_value = float(reward) if isinstance(reward, (int, float)) else None
    return RedCheckCase(
        case_id=label,
        kind=label,
        status=status,
        reward=reward_value,
        expected_status=expected_status,
        expected_reward=expected_reward,
        detail=str(run.get("job_dir") or run.get("error") or ""),
    )


def run_reconstruction_workflow(
    *,
    attempt_ref: str,
    source_report_id: str,
    report: dict[str, Any],
    evidence: list[dict[str, Any]],
    replay_workspace: str | Path,
    replay_files: list[dict[str, Any]],
    model: Any,
    output_root: str | Path,
    harbor_root: str | Path,
    execute_rollout: bool = False,
    model_name: str = "claude-opus-4-8",
    rollout_trials: int = 1,
    curation_thresholds: CurationThresholds | None = None,
) -> Path:
    """执行 Task→Environment→Verifier→Bundle→RED-check→Rollout→SFT。

    `execute_rollout=False` 时只物化所有计划与待执行报告；不会访问模型服务
    以外的外部沙盒。真实执行要求 Harbor/AGS 凭据由当前进程环境提供。
    """
    if rollout_trials < 1:
        raise ReconstructionWorkflowError("rollout_trials 必须大于 0")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stages = root / "stages"

    task_root = run_task_recovery(
        attempt_ref=attempt_ref,
        source_report_id=source_report_id,
        report=report,
        evidence=evidence,
        model=model,
        output_root=stages / "task",
        model_name=model_name,
    )
    task_payload = _json(task_root / "task_recovery.json")
    task = _ready_candidate(task_payload, "candidates")

    environment_root = run_environment_completion(
        task=task,
        attempt_ref=attempt_ref,
        replay_workspace=replay_workspace,
        replay_files=replay_files,
        evidence=evidence,
        model=model,
        output_root=stages / "environment",
        model_name=model_name,
    )
    environment_payload = _json(environment_root / "environment_completion.json")
    environment = _ready_candidate(environment_payload, "candidates")
    workspace_rel = environment.get("workspace")
    if not isinstance(workspace_rel, str):
        raise ReconstructionWorkflowError("环境候选缺少 workspace")
    completed_workspace = environment_root / workspace_rel
    if not completed_workspace.is_dir():
        raise ReconstructionWorkflowError(f"补全 workspace 不存在：{completed_workspace}")

    sufficiency_root = run_sufficiency_judge(
        task=task,
        workspace_root=completed_workspace,
        evidence=evidence,
        model=model,
        output_root=stages / "sufficiency",
        model_name=model_name,
    )
    sufficiency = _json(sufficiency_root / "sufficiency_judgement.json")
    if sufficiency.get("decision") != "READY" or sufficiency.get("label") != "SUFFICIENT":
        raise ReconstructionWorkflowError("workspace 未通过充分性判定")

    workspace_files = {
        path.relative_to(completed_workspace).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(completed_workspace.rglob("*"))
        if path.is_file()
    }
    verifier, verifier_audit = synthesize_verifier(
        task=task,
        workspace_files=workspace_files,
        model=model,
        model_name=model_name,
    )
    if verifier is None:
        raise ReconstructionWorkflowError(
            "Verifier 需要人工复核：" + ",".join(verifier_audit.get("open_questions", []))
        )

    verifier_root = ArtifactWorkspace(
        stages / "verifier", hashlib.sha256(verifier.candidate_id.encode()).hexdigest()
    )
    verifier_root.staging_path.mkdir(parents=True, exist_ok=True)
    verifier_entries = [
        write_json_artifact(
            verifier_root.staging_path, "verifier_candidate.json", verifier.to_dict()
        ),
        write_json_artifact(
            verifier_root.staging_path, "private/model_exchange.json", verifier_audit
        ),
    ]
    write_json_artifact(
        verifier_root.staging_path,
        "artifact_manifest.json",
        {
            "schema_version": "traceforge.verifier-artifacts.v1",
            "files": artifact_entry_dicts(verifier_entries),
        },
    )
    verifier_published = verifier_root.publish()

    bundle_root = compile_bundle(
        task=task,
        workspace_root=completed_workspace,
        verifier=verifier,
        output_root=stages / "bundle",
    )
    oracle_bundle_root = compile_bundle(
        task=task,
        workspace_root=completed_workspace,
        verifier=verifier,
        output_root=stages / "red-check-bundles",
        mutation_index=None,
        solution_index=0,
    )
    mutation_bundle_root = compile_bundle(
        task=task,
        workspace_root=completed_workspace,
        verifier=verifier,
        output_root=stages / "red-check-bundles",
        mutation_index=0,
    )
    bundle = bundle_root / "task"
    oracle_bundle = oracle_bundle_root / "task"
    mutation_bundle = mutation_bundle_root / "task"
    jobs_root = root / "harbor-jobs"

    def plan(bundle_path: Path, mode: str, name: str) -> Path:
        return build_rollout_plan(
            HarborRolloutConfig(
                task_dir=bundle_path,
                harbor_root=Path(harbor_root),
                output_root=stages / "rollout" / name,
                jobs_root=jobs_root / name,
                agent_mode=mode,
                model=f"anthropic/{model_name}",
                trials=rollout_trials,
                concurrency=1,
            )
        )

    rollout_plan = plan(bundle, "hermes", "hermes")
    red_plans = {
        "oracle_pass": plan(oracle_bundle, "oracle", "oracle-pass"),
        "nop_fail": plan(bundle, "nop", "nop-fail"),
        "mutation_fail": plan(mutation_bundle, "oracle", "mutation-fail"),
    }

    execution: dict[str, Any] = {
        "status": "PLAN_ONLY",
        "external_execution": False,
        "hermes": None,
        "red_check": {"status": "PENDING_EXECUTION"},
    }
    sft_result: dict[str, Any] = {"status": "PENDING_EXECUTION", "candidates": []}
    if execute_rollout:
        hermes_run = _run_plan_and_read(
            rollout_plan, jobs_root / "hermes", agent_mode="hermes"
        )
        red_runs = {
            "oracle_pass": _run_plan_and_read(
                red_plans["oracle_pass"], jobs_root / "oracle-pass", agent_mode="oracle"
            ),
            "nop_fail": _run_plan_and_read(
                red_plans["nop_fail"], jobs_root / "nop-fail", agent_mode="nop"
            ),
            "mutation_fail": _run_plan_and_read(
                red_plans["mutation_fail"], jobs_root / "mutation-fail", agent_mode="oracle"
            ),
        }
        red_report = evaluate_red_check(
            (
                _red_case("oracle_pass", red_runs["oracle_pass"], "PASS", 1.0),
                _red_case("nop_fail", red_runs["nop_fail"], "FAIL", 0.0),
                _red_case("mutation_fail", red_runs["mutation_fail"], "FAIL", 0.0),
            )
        )
        execution = {
            "status": "COMPLETED" if hermes_run.get("results") else "FAILED",
            "external_execution": True,
            "hermes": hermes_run,
            "red_check": {
                "status": "PASS" if red_report.passed else "FAIL",
                "report": {
                    "passed": red_report.passed,
                    "checked_count": red_report.checked_count,
                    "failed_case_ids": list(red_report.failed_case_ids),
                    "metrics": red_report.metrics,
                },
                "runs": red_runs,
            },
        }
        result = hermes_run.get("results")
        trials = result.get("trials", []) if isinstance(result, dict) else []
        rows = []
        for index, trial in enumerate(trials):
            rows.append(
                {
                    "candidate_id": task.get("recovery_id", attempt_ref),
                    "bundle_id": bundle_root.name,
                    "rollout_id": rollout_plan.name,
                    "trial_id": trial.get("trial_name")
                    or trial.get("trial_id")
                    or f"trial-{index:03d}",
                    "verifier_status": "PASS" if trial.get("status") == "PASS" else "FAIL",
                    "reward": trial.get("reward"),
                    "task_recovery_confidence": task.get("confidence", 0.0),
                    "environment_recovery_confidence": environment.get("confidence", 0.0),
                    "trajectory_quality": 1.0 if trial.get("trajectory_present") else 0.0,
                    "solution_leakage": not red_report.passed,
                    "reproducible": rollout_trials >= 2 and trial.get("status") == "PASS",
                    "trajectory_artifact": trial.get("result_path"),
                }
            )
        curated = []
        for row in rows:
            candidate = curate_candidate(row, curation_thresholds or CurationThresholds())
            curated.append(candidate.to_dict())
        sft_result = {
            "status": "READY" if red_report.passed else "REVIEW",
            "candidates": curated,
        }

    final = ArtifactWorkspace(
        root / "result", hashlib.sha256((attempt_ref + source_report_id).encode()).hexdigest()
    )
    entries = [
        write_json_artifact(
            final.staging_path,
            "workflow_manifest.json",
            {
                "schema_version": WORKFLOW_SCHEMA,
                "attempt_ref": attempt_ref,
                "source_report_id": source_report_id,
                "status": execution["status"],
                "stages": {
                    "task": str(task_root),
                    "environment": str(environment_root),
                    "sufficiency": str(sufficiency_root),
                    "verifier": str(verifier_published),
                    "bundle": str(bundle_root),
                    "rollout": str(rollout_plan),
                    "red_check": {key: str(value) for key, value in red_plans.items()},
                },
                "execute_rollout": execute_rollout,
                "model": model_name,
            },
        ),
        write_json_artifact(
            final.staging_path,
            "metrics.json",
            {
                "schema_version": "traceforge.reconstruction-workflow-metrics.v2",
                "task_candidate_count": len(task_payload.get("candidates", [])),
                "environment_candidate_count": len(environment_payload.get("candidates", [])),
                "sufficiency_label": sufficiency.get("label"),
                "verifier_status": verifier.status,
                "external_execution": execution.get("external_execution", False),
                "red_check": execution.get("red_check"),
                "sft_status": sft_result.get("status"),
            },
        ),
        write_json_artifact(final.staging_path, "execution.json", execution),
        write_json_artifact(final.staging_path, "sft_curation.json", sft_result),
    ]
    write_json_artifact(
        final.staging_path,
        "artifact_manifest.json",
        {
            "schema_version": "traceforge.reconstruction-workflow-artifacts.v2",
            "files": artifact_entry_dicts(entries),
        },
    )
    return final.publish()


__all__ = ["WORKFLOW_SCHEMA", "ReconstructionWorkflowError", "run_reconstruction_workflow"]

