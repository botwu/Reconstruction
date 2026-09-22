"""记录论文中的任务 q 与环境 E；未通过 Intent 的请求保留为待恢复状态。"""

from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath
from typing import Any

from traceforge.reconstruction.environment_bindings import environment_bindings
from traceforge.reconstruction.terminal_universe_environment import ReplayResult

TASK_ENVIRONMENT_PAIR_SCHEMA = "traceforge.task-environment-pair.v1"


class TaskEnvironmentPairError(ValueError):
    """任务或环境证据无法形成可追溯契约。"""


def _artifact_ref(value: str | Path | None, root: Path) -> str | None:
    if value is None:
        return None
    try:
        return Path(value).resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise TaskEnvironmentPairError(f"产物位于重建目录之外：{value}") from exc


def build_task_environment_pair(
    *,
    source_task: dict[str, Any],
    intent: dict[str, Any],
    source: dict[str, Any],
    replay: ReplayResult,
    result: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    """只记录实际阶段结果；只有 READY Intent 的指令可以成为执行任务。"""

    task_id = source_task["task_id"]
    task_root = root / "tasks" / task_id
    intent_status = intent.get("status", "NOT_RUN")
    task = intent.get("task") if intent_status == "READY" else None
    if intent_status == "READY" and not isinstance(task, dict):
        raise TaskEnvironmentPairError(f"任务 {task_id} 的 READY Intent 缺少 task")
    task = task or {}
    if task and task.get("task_id") != task_id:
        raise TaskEnvironmentPairError("Intent 与筛选任务 ID 不一致")
    source_texts = copy.deepcopy(source_task.get("user_texts") or [])
    instruction = task.get("task_instruction")
    obligations = copy.deepcopy(task.get("acceptance_obligations") or [])
    bindings = copy.deepcopy(environment_bindings(task))
    support = result.get("execution_support_route") or {}
    origin = support.get("env_origin", "NONE")
    completion = result.get("completion") or {}
    candidates = completion.get("candidates") or []
    sufficiency = result.get("sufficiency") or {}
    # 没有候选通过时，编排层只记录 selection audit；不能误记为未运行。
    sufficiency_status = sufficiency.get("decision") or sufficiency.get("status")
    if sufficiency_status is None and "sufficiency_audit" in result:
        sufficiency_status = "REVIEW"
    verification = result.get("verification") or {}
    verification_status = verification.get("status") or (
        "PENDING_EXECUTION" if result.get("status") == "PENDING_EXECUTION" else "NOT_RUN"
    )
    pair = {
        "schema_version": TASK_ENVIRONMENT_PAIR_SCHEMA,
        "task_id": task_id,
        "status": result.get("status", "REVIEW"),
        "stopped_at": result.get("stopped_at"),
        "errors": list(result.get("errors") or []),
        "q": {
            "source_task_id": task_id,
            "source_user_texts": source_texts,
            "source_instruction": "\n\n".join(source_texts),
            "source_message_indices": list(source_task.get("message_indices") or []),
            "intent_status": intent_status,
            "execution_instruction": instruction,
            "core_objective": task.get("core_objective"),
            "projection_mode": "intent_recovery_same_goal" if task else "unrecovered",
            "user_evidence_ref_ids": sorted({
                ref for item in obligations for ref in item.get("evidence_ref_ids", [])
            }),
            "acceptance_obligations": obligations,
            "environment_bindings": bindings,
            "success_criteria": list(task.get("success_criteria") or []),
            "mandatory_constraints": list(task.get("mandatory_constraints") or []),
            "prohibitions": list(task.get("prohibitions") or []),
            "task_contract_ref": _artifact_ref(task_root / "task_contract.json", root),
            "task_fit_ref": _artifact_ref(task_root / "task_fit.json", root),
            "variant_proposal_ref": _artifact_ref(task_root / "variant_proposal.json", root),
        },
        "environment": {
            "origin": origin,
            "initial_workspace_ref": _artifact_ref(task_root / "initial_workspace", root)
            if origin != "NONE" else None,
            "replay_manifest_ref": _artifact_ref(task_root / "replay.json", root),
            "replayed_file_count": len(replay.files),
            "partial_file_count": sum(item.completeness == "PARTIAL" for item in replay.files),
            "partial_evidence_count": len(replay.partial_evidence),
            "withheld_change_count": len(replay.withheld_changes),
            "unknown_mutation_barrier_count": len(replay.unknown_mutation_barriers),
            "completion_status": completion.get("status", "NOT_RUN"),
            "completion_candidate_count": len(candidates),
            "completion_ready_count": sum(
                item.get("decision") == "READY" and bool(item.get("workspace"))
                for item in candidates
            ),
            "completed_workspace_ref": _artifact_ref(result.get("workspace"), root),
            "env_root_ref": _artifact_ref(result.get("env_root"), root),
            "selected_candidate_index": result.get("selected_index"),
            "sufficiency_status": sufficiency_status or "NOT_RUN",
            "sufficiency_label": sufficiency.get("label"),
            "verification_status": verification_status,
            "sft_eligible": verification.get("sft_eligible") is True,
            "contract_status": (result.get("environment_contract") or {}).get("status"),
            "execution_readiness": (
                (result.get("environment_contract") or {}).get("evidence") or {}
            ).get("execution_readiness"),
        },
        "provenance": {
            "source_schema_version": source.get("schema_version"),
            "source_ref": source.get("source_ref"),
            "line_sha256": source.get("line_sha256"),
            "source_artifact_ref": "reconstruction_source.json",
            "intent_prompt_version": intent.get("prompt_version"),
            "completion_prompt_version": completion.get("prompt_version"),
            "sufficiency_prompt_version": sufficiency.get("prompt_version"),
            "support_route": support.get("route"),
        },
    }
    validate_task_environment_pair(pair)
    return pair


def validate_task_environment_pair(pair: dict[str, Any]) -> None:
    """校验结构与引用；同目标语义由 Intent 门禁负责，不能靠字符串相似度证明。"""

    if pair.get("schema_version") != TASK_ENVIRONMENT_PAIR_SCHEMA:
        raise TaskEnvironmentPairError("任务环境契约 schema_version 不匹配")
    task_id = pair.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise TaskEnvironmentPairError("任务环境契约缺少 task_id")
    q, environment = pair.get("q"), pair.get("environment")
    if not isinstance(q, dict) or not isinstance(environment, dict):
        raise TaskEnvironmentPairError("任务环境契约必须包含 q 和 environment")
    if q.get("source_task_id") != task_id:
        raise TaskEnvironmentPairError("任务与来源 ID 不一致")
    if q.get("intent_status") == "READY":
        for key in ("source_instruction", "execution_instruction", "core_objective"):
            if not isinstance(q.get(key), str) or not q[key].strip():
                raise TaskEnvironmentPairError(f"READY 任务缺少 q.{key}")
        obligations = q.get("acceptance_obligations")
        if not isinstance(obligations, list) or not obligations:
            raise TaskEnvironmentPairError("READY 任务缺少验收义务")
        ids = []
        for item in obligations:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise TaskEnvironmentPairError("验收义务缺少 ID")
            ids.append(item["id"])
            refs = item.get("evidence_ref_ids")
            if not isinstance(refs, list) or not refs or any(
                not isinstance(ref, str) or not ref.startswith("user:") for ref in refs
            ):
                raise TaskEnvironmentPairError("验收义务必须引用用户证据")
        if len(set(ids)) != len(ids) or any(not value.strip() for value in ids):
            raise TaskEnvironmentPairError("验收义务 ID 必须非空且唯一")
    elif q.get("execution_instruction") is not None:
        raise TaskEnvironmentPairError("未通过 Intent 的任务不得提供执行指令")
    if environment.get("origin") not in {"REPLAYED", "DEFAULT_EMPTY", "NONE"}:
        raise TaskEnvironmentPairError("环境来源不受支持")
    for key, value in environment.items():
        if key.endswith("_count") and (type(value) is not int or value < 0):
            raise TaskEnvironmentPairError(f"environment.{key} 必须为非负整数")
        if key.endswith("_ref") and value is not None and (
            not isinstance(value, str) or not value or "\\" in value
            or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts
        ):
                raise TaskEnvironmentPairError(f"environment.{key} 必须为安全的相对引用")
    if environment["origin"] == "NONE" and any(
        environment.get(key) is not None
        for key in ("initial_workspace_ref", "completed_workspace_ref", "env_root_ref")
    ):
        raise TaskEnvironmentPairError("NONE 环境不得记录工作区")


def write_task_environment_pair(root: Path, pair: dict[str, Any]) -> Path:
    """持久化契约；只存引用与计数，不复制 withheld 代码或私有模型输出。"""

    validate_task_environment_pair(pair)
    path = root / "task_environment_pair.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pair, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
