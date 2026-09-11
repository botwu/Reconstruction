"""单条失败轨迹的完整重建闭环编排。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.harbor_ags.rollout import HarborRolloutConfig, build_rollout_plan
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.verifier.bundle import compile_bundle
from traceforge.verifier.synthesis import synthesize_verifier

from .environment_completion import run_environment_completion
from .sufficiency_judge import run_sufficiency_judge
from .task_recovery import run_task_recovery

WORKFLOW_SCHEMA = "traceforge.reconstruction-workflow.v1"


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
) -> Path:
    """执行 Task→Environment→Verifier→Bundle→Rollout plan；真实 rollout 默认关闭。"""
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
    # synthesize_verifier 返回候选内存对象；将公开 spec 与私有模型审计分开发布。
    verifier_root.staging_path.mkdir(parents=True, exist_ok=True)
    write_json_artifact(verifier_root.staging_path, "verifier_candidate.json", verifier.to_dict())
    write_json_artifact(verifier_root.staging_path, "private/model_exchange.json", verifier_audit)
    verifier_published = verifier_root.publish()
    bundle_root = compile_bundle(
        task=task,
        workspace_root=completed_workspace,
        verifier=verifier,
        output_root=stages / "bundle",
    )
    bundle = bundle_root / "task"
    rollout_plan = build_rollout_plan(
        HarborRolloutConfig(
            task_dir=bundle,
            harbor_root=Path(harbor_root),
            output_root=stages / "rollout",
            jobs_root=stages / "harbor-jobs",
            model=f"anthropic/{model_name}",
            trials=1,
            concurrency=1,
        )
    )
    execution = {"status": "PLAN_ONLY", "external_execution": False}
    if execute_rollout:
        from traceforge.harbor_ags.rollout import execute_rollout_plan

        execution = execute_rollout_plan(rollout_plan)
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
                },
                "execute_rollout": execute_rollout,
                "model": model_name,
            },
        )
    ]
    entries.append(
        write_json_artifact(
            final.staging_path,
            "metrics.json",
            {
                "schema_version": "traceforge.reconstruction-workflow-metrics.v1",
                "task_candidate_count": len(task_payload.get("candidates", [])),
                "environment_candidate_count": len(environment_payload.get("candidates", [])),
                "sufficiency_label": sufficiency.get("label"),
                "verifier_status": verifier.status,
                "external_execution": execution.get("external_execution", execute_rollout),
            },
        )
    )
    write_json_artifact(
        final.staging_path,
        "artifact_manifest.json",
        {
            "schema_version": "traceforge.reconstruction-workflow-artifacts.v1",
            "files": artifact_entry_dicts(entries),
        },
    )
    return final.publish()


__all__ = ["WORKFLOW_SCHEMA", "ReconstructionWorkflowError", "run_reconstruction_workflow"]
