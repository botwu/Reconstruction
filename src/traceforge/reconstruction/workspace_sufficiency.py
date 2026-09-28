"""Workspace Sufficiency Agent：只读判断 bE 对 q 是否够用。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents import SUFFICIENCY_ROLE, AgentRuntime, AgentSession
from traceforge.reconstruction.agents.session import workspace_tree_hash
from traceforge.reconstruction.environment_bindings import (
    environment_bindings,
    missing_binding_paths,
    workspace_is_stub_ensemble,
)

from traceforge.reconstruction.workspace_integrity import (
    classify_integrity_issues,
    inspect_workspace_integrity,
)

SUFFICIENCY_SCHEMA = "traceforge.workspace-sufficiency.v1"
SUFFICIENCY_PROMPT_VERSION = "workspace-sufficiency-agent-v9-evidence-context"


def run_workspace_sufficiency(
    *,
    task: dict[str, Any],
    workspace_root: str | Path,
    agent: AgentRuntime,
    output_root: str | Path,
    observed_paths: Iterable[str] = (),
    repair_feedback: dict[str, Any] | None = None,
    reconstruction_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace_root).resolve()
    # The role runtime deletes its sandbox before returning. Inventory is a
    # property of the immutable input, so never query session.sandbox afterwards.
    input_inventory = workspace_tree_hash(workspace)
    context = dict(reconstruction_context or {})
    context["replay_files"] = [
        {
            **row,
            "current_sha256": input_inventory.get(row["path"]),
            "current_matches_replay": input_inventory.get(row["path"]) == row["replay_sha256"],
        }
        for row in context.get("replay_files") or []
    ]
    file_count = sum(not digest.startswith("symlink:") for digest in input_inventory.values())
    session = AgentSession(workspace=workspace, allow_write=False)
    preflight_errors: list[str] = []
    if not workspace.is_dir():
        preflight_errors.append("WORKSPACE_NOT_FOUND")
    objective = task.get("core_objective") or task.get("task_instruction")
    if not isinstance(objective, str) or not objective.strip():
        preflight_errors.append("TASK_NOT_EXECUTABLE")
    missing_paths = missing_binding_paths(workspace, task) if workspace.is_dir() else []
    stub_only = workspace.is_dir() and workspace_is_stub_ensemble(workspace)
    integrity = inspect_workspace_integrity(workspace, task, observed_paths=observed_paths)
    instruction = "\n".join(
        [
            "Inspect the workspace with tools. Do not modify it. Do not solve the task.",
            "This is the task-start environment: the requested feature is expected to be missing.",
            "Do not require acceptance obligations to pass already; that would erase the RED baseline. Judge whether a solver can implement them from the available context.",
            "FILE initial_required_paths are task-start inputs and must be present; output_paths are post-execution targets and must not be pre-created.",
            "Partial excerpts may suffice when they expose the interfaces and structures needed to implement the task.",
            "Return INSUFFICIENT only when required source, execution context, or domain facts are unavailable enough that implementation cannot start; missing target behavior alone is not a blocker.",
            "STATIC_INTEGRITY_REPORT is a read-only syntax/token diagnostic under the stated host Python version, not a completeness proof.",
            "Inspect every issue and classify it with issue_id, exact path, classification, and a concrete task-grounded reason. "
            "Use only issue_id and path values present in STATIC_INTEGRITY_REPORT; never invent additional issue IDs.",
            "BASELINE_TASK_DEFECT: the requested task itself requires fixing this observed defect; preserve it as the unsolved baseline.",
            "RECONSTRUCTION_GAP: required pre-task source/context is missing or damaged independently of the requested change.",
            "IRRELEVANT: the issue is outside the task's necessary execution/analysis path, or arises solely from a supported target Python version mismatch; justify with evidence.",
            "Do not infer these categories from keywords. Explain their relationship to the actual task and inspected source.",
            "Every issue requires one classification. RECONSTRUCTION_GAP or unclassified issues forbid READY.",
            "After judging sufficiency, compare the recovered task with the verified workspace. "
            "Contextual sufficiency and execution readiness are separate decisions: do not "
            "downgrade a review-only context merely because runtime probes are unavailable. "
            "For an executable candidate, however, run_environment_probe must provide load, "
            "reset, and dependency evidence; an empty or partial probe set must be reported "
            "as execution_preflight REVIEW. A probe timeout is execution evidence, not task "
            "conflict. Include any returned "
            "probe_id in environment_checks; later Verifier/Harbor execution decides whether "
            "the candidate is runnable.",
            "根据用户任务选择必要的探测能力。只读源码审查、分析或报告任务应验证必要源码可读、所需分析工具可用、"
            "以及独立临时目录中的报告写入可重复；不应因没有 Cargo.toml 等构建入口而强求整个项目可以编译。"
            "只有任务确实依赖构建、导入或程序运行时才检查相应依赖。每个探针必须说明它与任务的关系。",
            "若提供 PREVIOUS_ENVIRONMENT_FEEDBACK，应复查上轮具体缺口；修复后的环境仍必须独立验证，"
            "不可因为 Completion 声称已修复而直接通过。缺少探针时补充真实探针，探针范围不当时按任务纠正。",
            "PREVIOUS_ENVIRONMENT_FEEDBACK:",
            json.dumps(repair_feedback or {}, ensure_ascii=False),
            "Each task_fit requirement must include obligation_id, SATISFIED|UNSATISFIED|UNKNOWN, "
            "a reason, and grounded evidence_paths or probe_ids when applicable. A probe is not "
            "mandatory when the public workspace path itself is sufficient evidence. TaskFit measures whether the recovered "
            "environment can support implementing and checking the obligation, not whether the "
            "requested change is already present: an absent target behavior is expected pre-task "
            "and should be SATISFIED when its source, interfaces, dependencies, and verifier "
            "surface are available. Use UNSATISFIED only for a concrete intrinsic environment "
            "conflict; use UNKNOWN only when the evidence is genuinely missing. An UNSATISFIED "
            "requirement additionally needs a supported conflict_kind, repairable_within_task=false, "
            "and reproducible task_conflict evidence.",
            "If the workspace is sufficient, return optional task_fit with decision "
            "READY_ORIGINAL|INCOMPATIBLE|REVIEW_TASK_FIT. Do not call an execution failure "
            "a task mismatch. Only return variant_proposal when the environment is sufficient "
            "and the original fitted task is intrinsically incompatible; the proposal must "
            "preserve the core intent and reference only observed paths.",
            "Finish with JSON:",
            '{"label":"SUFFICIENT|INSUFFICIENT|UNKNOWN","reason":"...","missing_context":[],'
            '"confidence":0.0,"decision":"READY|REVIEW",'
            '"integrity_classifications":[{"issue_id":"integrity-001","path":"...",'
            '"classification":"BASELINE_TASK_DEFECT|RECONSTRUCTION_GAP|IRRELEVANT","reason":"..."}],'
            '"task_fit":{"decision":"READY_ORIGINAL|INCOMPATIBLE|REVIEW_TASK_FIT",'
            '"reason":"...","requirements":[]},"variant_proposal":null,',
            '"environment_checks":[{"kind":"load|reset|dependency","probe_ids":[],"reason":"..."}]}',
            "TASK:",
            json.dumps(task, ensure_ascii=False),
            "ENVIRONMENT_BINDINGS:",
            json.dumps(environment_bindings(task), ensure_ascii=False),
            f"WORKSPACE_ROOT: {workspace.as_posix()}",
            "RECONSTRUCTION_CONTEXT 中的范围和 PARTIAL 是历史回放事实，不等于当前候选仍有相同缺口。",
            "结合 current_sha256/current_matches_replay、当前补全 provenance 和 uncertainties 读取任务相关源码。",
            "字节改变不证明缺口已修复；独立判断当前环境。只有具体缺口影响任务时写入 missing_context，交回现有修复；无关 PARTIAL 可以 SUFFICIENT。",
            "RECONSTRUCTION_CONTEXT:",
            json.dumps(context, ensure_ascii=False),
            "STATIC_INTEGRITY_REPORT:",
            json.dumps(integrity, ensure_ascii=False),
        ]
    )
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    ran = agent.run(
        role=SUFFICIENCY_ROLE,
        instruction=instruction,
        session=session,
        output_root=root,
    )
    errors = [*preflight_errors, *ran.errors]
    if not ran.completed:
        errors.append("AGENT_INCOMPLETE")
    if getattr(agent, "backend", "") != "hermes-sandbox":
        errors.append("REAL_PROBE_REQUIRED")
    if session.read_only_probe_blocked is not True:
        errors.append("READ_ONLY_PROBE_NOT_CONFIRMED")
    if session.sandbox_started and not session.sandbox_stopped:
        errors.append("SANDBOX_CLEANUP_NOT_CONFIRMED")
    if session.sandbox_cleanup_error:
        errors.append("SANDBOX_CLEANUP_FAILED")
    inspected = any(
        event.get("name") in {"list_dir", "read_file"} and event.get("ok") is True
        for event in session.tool_events
    )
    if not inspected:
        errors.append("NO_ACTIVE_WORKSPACE_INSPECTION")

    # Probe execution is a deterministic environment-stage responsibility.
    # The model may still judge contextual sufficiency without it, but it must
    # never be reported as execution-ready with zero or partial probes.
    execution_probe_errors: list[str] = []
    probe_kinds = {
        item.get("purpose")
        for item in session.environment_probes
        if isinstance(item, dict) and item.get("status") == "PASS"
    }
    if probe_kinds != {"load", "reset", "dependency"}:
        execution_probe_errors.append("ENVIRONMENT_PROBES_REQUIRED")
    payload = ran.payload if isinstance(ran.payload, dict) else {}
    label = str(payload.get("label", "UNKNOWN"))
    decision = str(payload.get("decision", "REVIEW"))
    integrity, integrity_errors, reconstruction_gap = classify_integrity_issues(
        integrity, payload.get("integrity_classifications")
    )
    errors.extend(integrity_errors)
    if label not in {"SUFFICIENT", "INSUFFICIENT", "UNKNOWN"}:
        errors.append("INVALID_LABEL")
        label = "UNKNOWN"
    if decision not in {"READY", "REVIEW"}:
        errors.append("INVALID_DECISION")
        decision = "REVIEW"
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
        errors.append("INVALID_CONFIDENCE")
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        errors.append("INVALID_CONFIDENCE")
        confidence = 0.0
    missing = payload.get("missing_context")
    if not isinstance(missing, list):
        errors.append("INVALID_MISSING_CONTEXT")
        missing = []
    if missing_paths:
        errors.append("MISSING_BINDING_PATH")
        label = "INSUFFICIENT"
        missing = list(dict.fromkeys([*missing_paths, *[str(item) for item in missing]]))
    if reconstruction_gap:
        label = "INSUFFICIENT"

    # A model-created runtime receipt is execution evidence, not a semantic
    # completeness proof. Only the explicit runtime/sandbox safety errors are
    # removed from semantic_errors; malformed or uninspected judge output
    # remains a real sufficiency failure.
    execution_only_prefixes = (
        "REAL_PROBE_REQUIRED",
        "ENVIRONMENT_PROBES_REQUIRED",
    )
    semantic_errors = [
        error for error in errors
        if not error.startswith(execution_only_prefixes)
    ]
    warnings: list[str] = []
    if stub_only:
        warnings.append("MISSING_PROJECT_SPECIFIC_CONTENT")
    semantic_status = (
        "READY" if label == "SUFFICIENT" and decision == "READY" and not semantic_errors
        else "REVIEW"
    )
    if semantic_status != "READY":
        decision = "REVIEW"
    preflight_errors = [
        error for error in errors if error.startswith(execution_only_prefixes)
    ]
    preflight_errors.extend(execution_probe_errors)
    result_status = semantic_status
    result = {
        "schema_version": SUFFICIENCY_SCHEMA,
        "prompt_version": SUFFICIENCY_PROMPT_VERSION,
        "status": result_status,
        "semantic_status": semantic_status,
        "label": label,
        "decision": decision,
        "reason": str(payload.get("reason", "")),
        "missing_context": [str(item) for item in missing],
        "missing_binding_paths": list(missing_paths),
        "integrity_report": integrity,
        "confidence": confidence,
        "errors": errors,
        "semantic_errors": semantic_errors,
        "warnings": warnings,
        "file_count": file_count,
        "workspace_hashes": input_inventory,
        "reconstruction_context": context,
        "read_only_probe": session.read_only_probe,
        "sandbox_cleanup_confirmed": session.sandbox_stopped and not session.sandbox_cleanup_error,
        "agent": {
            "role": SUFFICIENCY_ROLE.name,
            "backend": ran.backend,
            "turns": len(ran.turns),
            "completed": ran.completed,
        },
    }
    if isinstance(payload.get("task_fit"), dict):
        result["task_fit"] = payload["task_fit"]
    if isinstance(payload.get("variant_proposal"), dict):
        result["variant_proposal"] = payload["variant_proposal"]
    if isinstance(payload.get("environment_checks"), list):
        result["environment_checks"] = payload["environment_checks"]
    if session.environment_probes:
        result["environment_probes"] = session.environment_probes
    result["execution_preflight"] = {
        "status": "READY" if not preflight_errors else "REVIEW",
        "errors": preflight_errors,
        "probe_count": len(session.environment_probes),
    }
    (root / "sufficiency.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    private = root / "private"
    private.mkdir(exist_ok=True)
    (private / "model_exchange.json").write_text(
        json.dumps(
            {
                "schema_version": "traceforge.private-model-exchange.v1",
                "request": {
                    "model": agent.model_name,
                    "prompt_version": SUFFICIENCY_PROMPT_VERSION,
                    "prompt": instruction,
                    "prompt_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
                    "system": SUFFICIENCY_ROLE.identity,
                },
                "response": {"text": ran.final_text, "receipt": None},
                "credentials_embedded": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return result
