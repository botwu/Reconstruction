"""区分环境证据缺口、未决诊断和运行故障，不将模型失败当作不可重建。"""

from __future__ import annotations

from typing import Any, TypedDict

from traceforge.reconstruction.environment_bindings import file_required_paths

RECONSTRUCTABILITY_SCHEMA = "traceforge.reconstructability.v1"
RECONSTRUCTABILITY_STATUSES = frozenset(
    {"READY", "REVIEW", "SKIPPED_UNRECONSTRUCTABLE", "INFRA_ERROR", "PIPELINE_ERROR"}
)
_MISSING_ISSUES = frozenset(
    {
        "REFERENCED_ASSET_MISSING",
        "OBSERVED_PYTHON_SOURCE_MISSING",
        "PYTHON_SOURCE_READ_ERROR",
    }
)
_INFRA_PREFIXES = (
    "SANDBOX_INIT", "SANDBOX_CLEANUP", "CONTAINER_CLEANUP_ERROR",
    "MODEL_CONNECTION_ERROR", "MODEL_TIMEOUT", "MODEL_API_FAILED",
    "HERMES_API_CALL_STALE_TIMEOUT", "HERMES_STREAM_STALE_TIMEOUT",
    "READ_ONLY_SETUP_FAILED", "INFRA_ERROR",
)
_PIPELINE_PREFIXES = (
    "INVALID_", "INTEGRITY_ISSUE_UNKNOWN", "INTEGRITY_CLASSIFICATION_INVALID",
    "INTEGRITY_CLASSIFICATIONS_INVALID", "INTEGRITY_CLASSIFICATION_DUPLICATE",
    "INTEGRITY_ISSUE_UNCLASSIFIED", "INVALID_JSON", "AGENT_INCOMPLETE",
    "MODEL_OUTPUT_INVALID", "MALFORMED_",
    "JUDGE_WORKSPACE_WRITEABLE", "TASK_NOT_EXECUTABLE", "PROTECTED_FILE_OVERWRITE",
    "VERIFIER_INPUT_PROTECTION_FAILED", "TOOL_NOT_ALLOWED", "REAL_PROBE_REQUIRED",
)
_DETERMINISTIC_MISSING_ERRORS = frozenset({"WORKSPACE_NOT_FOUND", "MISSING_BINDING_PATH"})


class ReconstructionBlocker(TypedDict, total=False):
    """可定位的阻塞证据；确定性缺口与未决分类分开保留。"""

    code: str
    path: str | None
    reason: str
    reference: str
    issue_id: str
    source_event_id: str
    confirmed: bool


class ReconstructabilityAssessment(TypedDict):
    """READY 仅表示没有已知重建缺口，不代替沙盒加载及 reset 证明。"""

    schema_version: str
    status: str
    blockers: list[ReconstructionBlocker]
    errors: list[str]


def _task_evidence(sufficiency: dict[str, Any]) -> set[str]:
    refs = sufficiency.get("task_evidence_ref_ids") or []
    known = {ref for ref in refs if isinstance(ref, str) and ref}
    task = sufficiency.get("task")
    if isinstance(task, dict):
        for obligation in task.get("acceptance_obligations") or []:
            if isinstance(obligation, dict):
                known.update(
                    ref for ref in obligation.get("evidence_ref_ids") or []
                    if isinstance(ref, str) and ref
                )
    return known


def _grounded_exception(issue: dict[str, Any], known: set[str]) -> bool:
    """豁免必须引用上游已验证的任务证据；解释文本本身不构成证据。"""

    refs = issue.get("classification_evidence_ref_ids")
    reason = issue.get("classification_reason")
    return (
        isinstance(refs, list) and bool(refs)
        and all(isinstance(ref, str) and ref in known for ref in refs)
        and isinstance(reason, str) and bool(reason.strip())
    )


def _necessary_paths(sufficiency: dict[str, Any]) -> set[str]:
    task = sufficiency.get("task")
    paths = set(file_required_paths(task if isinstance(task, dict) else None))
    paths.update(
        path for path in sufficiency.get("required_paths") or []
        if isinstance(path, str) and path
    )
    return paths


def _unknown_initial_state(
    sufficiency: dict[str, Any], replay: Any,
) -> list[ReconstructionBlocker]:
    """未知写入只阻塞必要且没有可信完整初态的路径，不能污染整个 session。"""

    if replay is None:
        return []
    required = _necessary_paths(sufficiency)
    trusted = {
        item.path for item in getattr(replay, "files", ()) or ()
        if getattr(item, "completeness", None) == "COMPLETE"
    }
    blockers: list[ReconstructionBlocker] = []
    for item in getattr(replay, "partial_evidence", ()) or ():
        if not isinstance(item, dict) or item.get("reason") not in {
            "read_after_unparsed_mutation", "unparsed_mutation_scope",
            "unparsed_mutation_unscoped",
        }:
            continue
        path = item.get("path")
        if path in trusted:
            continue
        if not isinstance(path, str):
            # An unscoped unknown command cannot prove which file was changed.
            # Keep that uncertainty at REVIEW when the task has required files,
            # but never turn it into a global skip.
            if required:
                blockers.append({
                    "code": "UNKNOWN_INITIAL_STATE_UNSCOPED", "path": None,
                    "reason": "未知写入没有可定位路径，不能证明必要文件的任务初态。",
                    "source_event_id": str(item.get("source_event_id") or ""),
                    "confirmed": False,
                })
            continue
        if not any(path == req or (req.endswith("/") and path.startswith(req)) for req in required):
            continue
        blockers.append({
            "code": "UNKNOWN_INITIAL_STATE", "path": path,
            "reason": "必要路径只有未知写入影响后的证据，缺少可信的完整任务初态。",
            "source_event_id": str(item.get("source_event_id") or ""),
            "confirmed": True,
        })
    return blockers


def assess_reconstructability(
    sufficiency: dict[str, Any], replay: Any = None,
) -> ReconstructabilityAssessment:
    """消费真实阶段诊断，返回可重建边界；不执行、修改或补造环境。

    故障优先级为模型/沙盒故障、协议错误、确定性缺口、未决判断。
    即使高优先级错误存在，缺失资产证据仍保留在 blockers，便于修复后复核。
    """

    # Execution preflight receipts are intentionally kept out of the
    # reconstruction decision. If a sufficiency result exposes the separated
    # semantic channel, consume that; otherwise retain compatibility with old
    # artifacts.
    raw_errors = sufficiency.get("semantic_errors")
    if raw_errors is None:
        raw_errors = sufficiency.get("errors") or []
    malformed = not isinstance(raw_errors, list) or any(
        not isinstance(error, str) for error in raw_errors
    )
    errors = [str(error) for error in raw_errors] if isinstance(raw_errors, list) else []
    if malformed:
        errors.append("INVALID_SUFFICIENCY_ERRORS")
    known = _task_evidence(sufficiency)
    blockers: list[ReconstructionBlocker] = []
    report = sufficiency.get("integrity_report") or {}
    issues = report.get("issues", []) if isinstance(report, dict) else []
    if not isinstance(report, dict) or not isinstance(issues, list):
        errors.append("INVALID_INTEGRITY_REPORT")
        issues = []
    pending = False
    for issue in issues:
        if not isinstance(issue, dict):
            errors.append("INVALID_INTEGRITY_ISSUE")
            continue
        code = str(issue.get("code") or "")
        if code not in _MISSING_ISSUES:
            continue
        exception = issue.get("classification") in {"BASELINE_TASK_DEFECT", "IRRELEVANT"}
        if exception and _grounded_exception(issue, known):
            continue
        blocker: ReconstructionBlocker = {
            "code": code, "path": issue.get("path"),
            "reason": str(issue.get("reason") or code), "confirmed": not exception,
        }
        for key in ("reference", "source_event_id"):
            if isinstance(issue.get(key), str):
                blocker[key] = issue[key]
        if isinstance(issue.get("id"), str):
            blocker["issue_id"] = issue["id"]
        blockers.append(blocker)
        if exception:
            pending = True
            errors.append(f"RECONSTRUCTABILITY_EXCEPTION_UNGROUNDED:{issue.get('id') or code}")
    for error in errors[:]:
        code = error.split(":", 1)[0]
        if code not in _DETERMINISTIC_MISSING_ERRORS:
            continue
        paths = sufficiency.get("missing_binding_paths") or [] if code == "MISSING_BINDING_PATH" else []
        if code == "MISSING_BINDING_PATH" and not paths:
            pending = True
            continue
        for path in paths or [None]:
            blockers.append({"code": code, "path": path, "reason": code, "confirmed": True})
    # missing_binding_paths 来自确定性文件盘点，即使模型忘记相应错误码也不能放行。
    if "MISSING_BINDING_PATH" not in errors:
        for path in sufficiency.get("missing_binding_paths") or []:
            blockers.append({
                "code": "MISSING_BINDING_PATH", "path": str(path),
                "reason": "任务要求的已有环境路径未出现在补全工作区中。", "confirmed": True,
            })
    blockers.extend(_unknown_initial_state(sufficiency, replay))
    if any(error.startswith(_INFRA_PREFIXES) for error in errors):
        status = "INFRA_ERROR"
    elif any(blocker.get("confirmed") for blocker in blockers):
        status = "SKIPPED_UNRECONSTRUCTABLE"
    elif any(error.startswith(_PIPELINE_PREFIXES) for error in errors):
        status = "PIPELINE_ERROR"
    elif (
        pending
        or any(blocker.get("code") == "UNKNOWN_INITIAL_STATE_UNSCOPED" for blocker in blockers)
        or errors
        or sufficiency.get("label") in {"INSUFFICIENT", "UNKNOWN"}
    ):
        status = "REVIEW"
    else:
        status = "READY"
    return {
        "schema_version": RECONSTRUCTABILITY_SCHEMA, "status": status,
        "blockers": blockers, "errors": list(dict.fromkeys(errors)),
    }
