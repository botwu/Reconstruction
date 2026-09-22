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
SUFFICIENCY_PROMPT_VERSION = "workspace-sufficiency-agent-v7-observed-source-integrity"
# Partial or stub excerpts can be valid evidence for some analytical tasks, but
# the model's explicit INSUFFICIENT decision is authoritative. Never promote
# it to SUFFICIENT based on keyword matching: missing domain context cannot be
# inferred from the existence of files.

def excerpt_or_stub_only_insufficiency(
    label: str, reason: str, missing_context: list[Any]
) -> bool:
    """Compatibility helper; explicit model insufficiency is never overridden."""

    del reason, missing_context
    return False


def run_workspace_sufficiency(
    *,
    task: dict[str, Any],
    workspace_root: str | Path,
    agent: AgentRuntime,
    output_root: str | Path,
    observed_paths: Iterable[str] = (),
) -> dict[str, Any]:
    workspace = Path(workspace_root).resolve()
    # The role runtime deletes its sandbox before returning. Inventory is a
    # property of the immutable input, so never query session.sandbox afterwards.
    input_inventory = workspace_tree_hash(workspace)
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
            "FILE environment_bindings required_paths must exist and must not be an all-stub tree.",
            "Partial excerpts may suffice when they expose the interfaces and structures needed to implement the task.",
            "Return INSUFFICIENT only when required source, execution context, or domain facts are unavailable enough that implementation cannot start; missing target behavior alone is not a blocker.",
            "STATIC_INTEGRITY_REPORT is a read-only syntax/token diagnostic under the stated host Python version, not a completeness proof.",
            "Inspect every issue and classify it with issue_id, exact path, classification, and a concrete task-grounded reason.",
            "BASELINE_TASK_DEFECT: the requested task itself requires fixing this observed defect; preserve it as the unsolved baseline.",
            "RECONSTRUCTION_GAP: required pre-task source/context is missing or damaged independently of the requested change.",
            "IRRELEVANT: the issue is outside the task's necessary execution/analysis path, or arises solely from a supported target Python version mismatch; justify with evidence.",
            "Do not infer these categories from keywords. Explain their relationship to the actual task and inspected source.",
            "Every issue requires one classification. RECONSTRUCTION_GAP or unclassified issues forbid READY.",
            "After judging sufficiency, compare the recovered task with the verified workspace. "
            "When the workspace is sufficient, use run_environment_probe in the same read-only "
            "sandbox for one load, one repeatable reset, and one dependency check. Use "
            "task_conflict only when a concrete fitted task requirement is impossible; a solver "
            "timeout is not task conflict. Include every returned probe_id in environment_checks.",
            "Each task_fit requirement must include obligation_id, SATISFIED|UNSATISFIED|UNKNOWN, "
            "a reason, and evidence_paths or probe_ids. An UNSATISFIED requirement additionally "
            "needs a supported conflict_kind, repairable_within_task=false, and reproducible "
            "task_conflict evidence.",
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
    # Preserve model diagnostics even when the runtime preflight failed; the
    # overall decision remains REVIEW because errors are a hard gate.
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
    if errors or label != "SUFFICIENT":
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
    if stub_only:
        errors.append("MISSING_PROJECT_SPECIFIC_CONTENT")
        label = "INSUFFICIENT"
    # An explicit INSUFFICIENT result is authoritative. A partial or stub
    # workspace may be sufficient for a narrowly scoped analytical task, but
    # that must be established by the model's evidence, never by a keyword
    # based upgrade here.
    # Recompute the decision after every schema and runtime check.  A malformed
    # confidence or an incomplete Hermes turn can never yield READY.
    if errors or label != "SUFFICIENT":
        decision = "REVIEW"
    result = {
        "schema_version": SUFFICIENCY_SCHEMA,
        "prompt_version": SUFFICIENCY_PROMPT_VERSION,
        "status": "READY" if label == "SUFFICIENT" and decision == "READY" else "REVIEW",
        "label": label,
        "decision": decision,
        "reason": str(payload.get("reason", "")),
        "missing_context": [str(item) for item in missing],
        "missing_binding_paths": list(missing_paths),
        "integrity_report": integrity,
        "confidence": confidence,
        "errors": errors,
        "file_count": file_count,
        "workspace_hashes": input_inventory,
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
