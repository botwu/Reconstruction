"""把一个完整 session 编排为一个或多个独立的 Task--Environment 对。"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceforge.curation.sft import write_reconstruction_sft_curation
from traceforge.reconstruction.agents import AgentRuntime, SandboxedAgentRuntime
from traceforge.reconstruction.env_replay import (
    normalize_file_ops,
    replay_from_timeline,
    write_replay_artifacts,
)
from traceforge.reconstruction.environment_bindings import (
    environment_bindings,
    non_file_obligation_ids,
)
from traceforge.reconstruction.intent_recovery import (
    INTENT_SCHEMA,
    IntentRecoveryError,
    run_intent_recovery,
    selected_task_views,
)
from traceforge.reconstruction.model_gateway import ChatModel
from traceforge.reconstruction.session_source import (
    ReconstructionSourceError,
    build_reconstruction_source,
    timeline_has_file_ops,
    write_reconstruction_source,
)
from traceforge.reconstruction.stage_metrics import reconstruction_stage_metrics
from traceforge.reconstruction.task_environment import (
    TaskEnvironmentPairError,
    build_task_environment_pair,
    write_task_environment_pair,
)
from traceforge.reconstruction.task_fit import (
    ENVIRONMENT_UNRECONSTRUCTABLE,
    build_environment_contract,
    build_task_contract,
    build_task_variant,
    fit_task_environment,
    generate_task_variant,
)
from traceforge.reconstruction.terminal_universe_environment import select_sufficient_candidate
from traceforge.reconstruction.verification import (
    VerificationConfig,
    run_reconstruction_verification,
)
from traceforge.reconstruction.workspace_completion import (
    complete_from_default_empty,
    complete_from_replayed,
)
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency

ELIGIBLE_RECONSTRUCTION_SCHEMA = "traceforge.eligible-reconstruction.v2"
RAW_SESSION_RECONSTRUCTION_SCHEMA = "traceforge.raw-session-reconstruction.v1"
ENV_REPLAYED = "REPLAYED"
ENV_DEFAULT_EMPTY = "DEFAULT_EMPTY"
ENV_NONE = "NONE"
_SOURCE_SUFFIXES = frozenset(
    {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".hpp"}
)


def _domain_route(task: dict[str, Any], source: dict[str, Any]) -> str:
    nested = task.get("source_task") if isinstance(task.get("source_task"), dict) else {}
    for obj in (task, nested, source):
        if not isinstance(obj, dict):
            continue
        route = obj.get("domain_route")
        if isinstance(route, str) and route.strip():
            normalized = route.strip()
            return "terminal" if normalized in {"code_file", "terminal"} else normalized
    record_route = str(source.get("route") or "")
    if record_route == "ELIGIBLE_CODE_FILE":
        return "terminal"
    if "RETRIEVAL" in record_route.upper():
        return "retrieval"
    return ""


def _explicit_file_obligation_ids(task: dict[str, Any]) -> list[str]:
    return [
        str(item["obligation_id"])
        for item in environment_bindings(task)
        if item.get("verifier_kind") == "FILE" and item.get("obligation_id")
    ]


def execution_support_route(
    *,
    task: dict[str, Any],
    source: dict[str, Any],
    replay: Any,
) -> dict[str, Any]:
    """把观察、值不值得重建、初始环境和 FILE 验收拆开。"""

    domain = _domain_route(task, source)
    files = list(getattr(replay, "files", ()) or ())
    source_files = [
        item
        for item in files
        if Path(getattr(item, "path", "")).suffix.lower() in _SOURCE_SUFFIXES
    ]
    has_ops = bool(source.get("selected_span_has_file_ops")) or bool(files)
    explicit_file = _explicit_file_obligation_ids(task)
    unverified = non_file_obligation_ids(task)
    payload = {
        "domain_route": domain or None,
        "session_tags": list(source.get("session_tags") or []),
        "task_tags": list(task.get("tags") or []),
        "replay_file_count": len(files),
        "source_file_count": len(source_files),
        "selected_span_has_file_ops": bool(source.get("selected_span_has_file_ops")),
        "terminal_batch_hint": len(files) >= 5 or len(source_files) >= 2,
        "allow_file_verifier": bool(explicit_file),
        "env_origin": ENV_NONE,
        "allow_completion": False,
    }
    if unverified:
        payload["unverified_obligations"] = unverified
    if files:
        return {
            **payload,
            "route": "TERMINAL_FILE",
            "reason_codes": [],
            "env_origin": ENV_REPLAYED,
            "allow_completion": True,
            "selected_span_has_file_ops": has_ops,
        }
    if domain == "retrieval" and not explicit_file:
        return {
            **payload,
            "route": "RETRIEVAL_UNSUPPORTED",
            "reason_codes": ["RETRIEVAL_UNSUPPORTED"],
            "env_origin": ENV_NONE,
            "allow_completion": False,
            "allow_file_verifier": False,
        }
    return {
        **payload,
        "route": "DEFAULT_EMPTY",
        "reason_codes": [],
        "env_origin": ENV_DEFAULT_EMPTY,
        "allow_completion": True,
        "allow_file_verifier": bool(explicit_file),
        "selected_span_has_file_ops": has_ops,
    }


class EligibleReconstructionError(RuntimeError):
    """ELIGIBLE 重建编排无法继续。"""


def _write_eligible_manifest(
    root: Path,
    source: dict[str, Any],
    intent: dict[str, Any],
    results: list[dict[str, Any]],
    prepared: dict[str, tuple[dict[str, Any], Any]],
) -> Path:
    intent_by_id = {
        str((item.get("task") or {}).get("task_id") or item.get("task_id")): item
        for item in intent.get("tasks") or []
    }
    for result in results:
        task_id = result["task_id"]
        screening, replay = prepared[task_id]
        try:
            pair = build_task_environment_pair(
                source_task=screening,
                intent=intent_by_id.get(task_id, {"status": "NOT_RUN"}),
                source=source,
                replay=replay,
                result=result,
                root=root,
            )
            pair_path = write_task_environment_pair(root / "tasks" / task_id, pair)
            result["task_environment_pair_path"] = str(pair_path)
        except TaskEnvironmentPairError as exc:
            # 契约不完整必须停止交付，不能只写旁路错误后继续导出训练样本。
            result["status"] = "REVIEW"
            result["errors"] = [*result.get("errors", []), "TASK_ENVIRONMENT_PAIR_INVALID"]
            result["task_environment_pair_error"] = str(exc)
            result.setdefault("stopped_at", "task_environment_pair")
            if isinstance(result.get("verification"), dict):
                result["verification"]["sft_eligible"] = False
    metrics = reconstruction_stage_metrics(source, intent, results)
    (root / "stage_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    statuses = [x.get("status") for x in results]
    ready_statuses = {"READY", "READY_VARIANT"}
    if statuses and all(x in ready_statuses for x in statuses):
        status = "READY_VARIANT" if any(x == "READY_VARIANT" for x in statuses) else "READY"
    elif statuses and all(x == ENVIRONMENT_UNRECONSTRUCTABLE for x in statuses):
        status = ENVIRONMENT_UNRECONSTRUCTABLE
    elif any(x == "PENDING_EXECUTION" for x in statuses) and not any(
        x == "REVIEW" for x in statuses
    ):
        status = "PENDING_EXECUTION"
    else:
        status = "REVIEW"
    stopped_stages = {
        str(item.get("stopped_at"))
        for item in results
        if item.get("status") not in ready_statuses and item.get("stopped_at")
    }
    if status in ready_statuses:
        stopped_at = None
    elif status == ENVIRONMENT_UNRECONSTRUCTABLE:
        stopped_at = "sufficiency"
    elif len(stopped_stages) == 1:
        stopped_at = next(iter(stopped_stages))
    elif stopped_stages:
        stopped_at = "mixed"
    else:
        stopped_at = "tasks"
    manifest: dict[str, Any] = {
        "schema_version": (
            RAW_SESSION_RECONSTRUCTION_SCHEMA
            if source.get("entry_mode") == "RAW_SESSION"
            else ELIGIBLE_RECONSTRUCTION_SCHEMA
        ),
        "status": status,
        "stopped_at": stopped_at,
        "source": {
            "entry_mode": source.get("entry_mode", "SCREENED_ELIGIBLE"),
            "source_ref": source.get("source_ref"),
            "line_number": source.get("line_number"),
            "line_sha256": source.get("line_sha256"),
            "label_status": source.get("label_status"),
            "selected_task_ids": source.get("selected_task_ids"),
            "raw_session_preserved": True,
        },
        "intent": intent,
        "tasks": results,
        "task_count": len(results),
        "ready_count": sum(x in ready_statuses for x in statuses),
        "review_count": sum(x == "REVIEW" for x in statuses),
        "stage_metrics": metrics,
    }
    if len(results) == 1:
        manifest.update(
            {
                k: results[0].get(k)
                for k in (
                    "workspace", "env_root", "verification", "sufficiency_audit",
                    "environment_contract", "task_contract", "task_fit", "variant",
                )
                if k in results[0]
            }
        )
        manifest["task"] = results[0].get("source_task")
    return _write_manifest(root, manifest)


def _write_manifest(root: Path, payload: dict[str, Any]) -> Path:
    path = root / "reconstruction_manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _task_relations(source: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    anchor = task.get("source_task") if isinstance(task.get("source_task"), dict) else task
    task_id = task.get("task_id")
    spans = {str(item) for item in anchor.get("span_ids") or []}
    return [
        relation
        for relation in source.get("relations") or []
        if (
            relation.get("from_task_id") == task_id
            or relation.get("to_task_id") == task_id
            or relation.get("from_span_id") in spans
            or relation.get("to_span_id") in spans
        )
    ]


def _task_source(source: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """给每个 task 保留完整 session 工具上下文；task 标签只作证据锚点。"""
    anchor = task.get("source_task") if isinstance(task.get("source_task"), dict) else task
    if anchor.get("task_id") != task.get("task_id"):
        raise EligibleReconstructionError("Intent task_id 与筛选任务标签不一致")
    spans = set(str(x) for x in anchor.get("span_ids") or [])
    if not spans:
        raise EligibleReconstructionError("Intent 缺少原始任务的 span_ids")
    out = copy.deepcopy(source)
    out["tasks"] = [copy.deepcopy(anchor)]
    out["relations"] = _task_relations(source, task)
    out["selected_task_ids"] = [task.get("task_id")]
    out["selected_span_ids"] = sorted(spans)
    out["selected_tool_timeline"] = [
        x
        for x in source.get("tool_timeline") or []
        if x.get("span_id") is None or x.get("span_id") in spans
    ]
    out["tool_timeline"] = copy.deepcopy(source.get("tool_timeline") or [])
    out["session_timeline_scope"] = "FULL_SESSION"
    out["selected_span_has_file_ops"] = timeline_has_file_ops(out["selected_tool_timeline"])
    if isinstance(anchor.get("domain_route"), str) and anchor.get("domain_route"):
        out["domain_route"] = anchor["domain_route"]
    return out


def _sandbox_init_errors(errors: Any) -> list[str]:
    return [
        str(item)
        for item in (errors or [])
        if str(item).startswith("SANDBOX_INIT")
    ]


def _write_stage_json(root: Path, name: str, payload: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _replay_and_route(
    *,
    task: dict[str, Any],
    root: Path,
    source: dict[str, Any],
) -> tuple[dict[str, Any], Any, dict[str, Any], Path]:
    task_id = str(task["task_id"])
    task_root = root / "tasks" / task_id
    task_root.mkdir(parents=True, exist_ok=True)
    task_source = _task_source(source, task)
    replay = replay_from_timeline(
        list(task_source.get("tool_timeline") or []),
        task_root / "initial_workspace",
    )
    write_replay_artifacts(replay, task_root)
    support = execution_support_route(task=task, source=task_source, replay=replay)
    return task_source, replay, support, task_root


def _support_route_result(
    *,
    task: dict[str, Any],
    support: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task_id": str(task["task_id"]),
        "source_task": task,
        "status": "REVIEW",
        "stopped_at": "support_route",
        "errors": list(support.get("reason_codes") or ["SUPPORT_ROUTE"]),
        "execution_support_route": support,
        "env_origin": support.get("env_origin") or ENV_NONE,
    }


def _task_result(
    *,
    task: dict[str, Any],
    root: Path,
    source: dict[str, Any],
    agent: AgentRuntime,
    verification_model: ChatModel | None,
    verification_config: VerificationConfig | None,
    verifier_agent: AgentRuntime | None = None,
    replay: Any | None = None,
    support: dict[str, Any] | None = None,
    task_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = str(task["task_id"])
    if replay is None or support is None or task_source is None:
        task_source, replay, support, task_root = _replay_and_route(
            task=task, root=root, source=source
        )
    else:
        task_root = root / "tasks" / task_id
    # Intent 的义务比粗筛领域标签更具体，不能沿用 Intent 前的执行许可。
    support = execution_support_route(task=task, source=task_source, replay=replay)
    if not support.get("allow_completion"):
        return _support_route_result(task=task, support=support)
    timeline = list(task_source.get("tool_timeline") or [])
    origin = str(support.get("env_origin") or ENV_REPLAYED)
    if origin == ENV_DEFAULT_EMPTY:
        workspace = task_root / "initial_workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        completion = complete_from_default_empty(
            task=task,
            replay=replay,
            timeline=timeline,
            source=task_source,
            agent=agent,
            output_root=task_root / "completion",
            workspace_root=task_root / "initial_workspace",
        )
    else:
        completion = complete_from_replayed(
            task=task,
            replay=replay,
            timeline=timeline,
            source=task_source,
            agent=agent,
            output_root=task_root / "completion",
            workspace_root=task_root / "initial_workspace",
        )
    result: dict[str, Any] = {
        "task_id": task_id,
        "source_task": task,
        "status": "REVIEW",
        "completion": completion,
        "execution_support_route": support,
        "env_origin": origin,
    }
    if completion.get("status") != "READY":
        sandbox_errors = _sandbox_init_errors(completion.get("errors"))
        if sandbox_errors:
            result["stopped_at"] = "sandbox_init"
            result["errors"] = sandbox_errors
            return result
        result["stopped_at"] = "completion"
        result["errors"] = list(completion.get("errors") or ["COMPLETION_REVIEW"])
        result["env_origin"] = origin
        return result
    judges: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    environment_contracts: dict[int, dict[str, Any]] = {}
    for candidate in completion.get("candidates") or []:
        if (
            not isinstance(candidate, dict)
            or candidate.get("decision") != "READY"
            or not candidate.get("workspace")
        ):
            judges.append({"label": "UNKNOWN", "decision": "REVIEW"})
            rows.append({"decision": "REVIEW"})
            continue
        timeline_observed = {
            str(op.get("path"))
            for op in normalize_file_ops(task_source.get("tool_timeline") or [])
            if op.get("kind") == "read" and isinstance(op.get("path"), str) and op.get("path")
        }
        replay_observed = {str(item.path) for item in replay.files if getattr(item, "path", None)}
        judge = run_workspace_sufficiency(
            task=task,
            observed_paths=sorted(replay_observed | timeline_observed),
            workspace_root=candidate["workspace"],
            agent=agent,
            output_root=task_root / "sufficiency" / f"{int(candidate.get('index', len(rows))):03d}",
        )
        sandbox_errors = _sandbox_init_errors(judge.get("errors"))
        if sandbox_errors:
            result["sufficiency"] = judge
            result["stopped_at"] = "sandbox_init"
            result["errors"] = sandbox_errors
            return result
        judges.append(judge)
        index = len(rows)
        environment = build_environment_contract(
            workspace_root=candidate["workspace"],
            env_root=candidate.get("env_root"),
            sufficiency=judge,
            replay=replay,
        )
        environment_contracts[index] = environment
        _write_stage_json(
            task_root / "environment" / f"{index:03d}",
            "environment_contract.json",
            environment,
        )
        environment_status = environment.get("status")
        if environment_status == ENVIRONMENT_UNRECONSTRUCTABLE:
            rows.append(
                {
                    "decision": ENVIRONMENT_UNRECONSTRUCTABLE,
                    "reason_codes": [item.get("code") for item in environment.get("blockers", [])],
                }
            )
        elif environment_status in {"INFRA_ERROR", "PIPELINE_ERROR"}:
            # Context sufficiency and execution preflight are separate. Keep
            # confirmed infra/pipeline failures out; ordinary REVIEW receipts
            # remain auditable and continue to the downstream verifier.
            rows.append(
                {
                    "decision": "ENVIRONMENT_NOT_READY",
                    "environment_status": environment_status,
                    "reason_codes": list(environment.get("errors") or [])
                    or ["ENVIRONMENT_CONTRACT_NOT_READY"],
                }
            )
        else:
            rows.append(
                {
                    "decision": "READY",
                    "environment_status": environment_status,
                    "confidence": judge.get("confidence", 0.0),
                }
            )
    selected, audit = select_sufficient_candidate(rows, judges)
    result["sufficiency_audit"] = audit
    if selected is None:
        result["stopped_at"] = "sufficiency"
        skipped = [row for row in rows if row.get("decision") == ENVIRONMENT_UNRECONSTRUCTABLE]
        if rows and skipped and len(skipped) == len(rows):
            result["status"] = ENVIRONMENT_UNRECONSTRUCTABLE
            result["errors"] = sorted(
                {
                    str(code)
                    for row in skipped
                    for code in row.get("reason_codes") or []
                    if code
                }
            ) or [ENVIRONMENT_UNRECONSTRUCTABLE]
        else:
            result["errors"] = ["NO_SUFFICIENT_CANDIDATE"]
        return result
    chosen = (completion.get("candidates") or [])[selected]
    result.update(
        {
            "selected_index": selected,
            "workspace": chosen.get("workspace"),
            "env_root": chosen.get("env_root"),
            "sufficiency": judges[selected],
        }
    )
    environment = environment_contracts.get(selected)
    if environment is None:
        result["status"] = "REVIEW"
        result["stopped_at"] = "task_fit"
        result["errors"] = ["ENVIRONMENT_CONTRACT_MISSING"]
        return result
    _write_stage_json(task_root, "environment_contract.json", environment)
    try:
        task_contract = build_task_contract(task=task)
        _write_stage_json(task_root, "task_contract.json", task_contract)
        fit = fit_task_environment(
            environment=environment,
            task=task_contract,
            agent_fit=(judges[selected].get("task_fit") if isinstance(judges[selected], dict) else None),
        )
    except Exception as exc:
        result["status"] = "REVIEW"
        result["stopped_at"] = "task_fit"
        result["errors"] = ["TASK_FIT_CONTRACT_ERROR", str(exc)]
        return result
    _write_stage_json(task_root, "task_fit.json", fit)
    result["environment_contract"] = environment
    result["task_contract"] = task_contract
    result["task_fit"] = fit
    task_for_verification = task
    variant = None
    if fit.get("decision") in {
        ENVIRONMENT_UNRECONSTRUCTABLE,
        "INFRA_ERROR",
        "PIPELINE_ERROR",
    }:
        result["status"] = fit["decision"]
        result["stopped_at"] = "task_fit"
        result["errors"] = list(fit.get("errors") or [fit["decision"]])
        return result
    if fit.get("decision") == "REVIEW_TASK_FIT":
        # TaskFit is an audit receipt. A review remains visible in the
        # manifest, but it is not an execution gate; the behavior verifier
        # decides whether the task actually passes.
        fit["execution_policy"] = "PROCEED_ORIGINAL"
        _write_stage_json(task_root, "task_fit.json", fit)
    if fit.get("decision") == "INCOMPATIBLE":
        # Only confirmed reproducible conflicts may create a variant. An
        # unresolved/static incompatibility remains an audit warning.
        if fit.get("variant_eligible") is True:
            proposal = judges[selected].get("variant_proposal") if isinstance(judges[selected], dict) else None
            if proposal is not None:
                try:
                    variant = build_task_variant(
                        parent_task=task_contract,
                        environment=environment,
                        fit=fit,
                        proposal=proposal,
                    )
                except Exception as exc:
                    result["status"] = "PIPELINE_ERROR"
                    result["stopped_at"] = "task_fit"
                    result["errors"] = ["VARIANT_CONTRACT_INVALID", str(exc)]
                    return result
            else:
                variant = generate_task_variant(
                    task=task_contract,
                    environment=environment,
                    fit=fit,
                    workspace_root=Path(chosen["workspace"]),
                    agent=agent,
                    output_root=task_root / "variant",
                )
            if variant is None:
                result["status"] = "SKIPPED_TASK_INCOMPATIBLE"
                result["stopped_at"] = "task_fit"
                result["errors"] = ["TASK_ENVIRONMENT_INCOMPATIBLE"]
                return result
            _write_stage_json(task_root, "variant_proposal.json", variant)
            result["variant"] = variant
            if not isinstance(variant.get("task"), dict):
                result["status"] = "PIPELINE_ERROR"
                result["stopped_at"] = "task_fit"
                result["errors"] = ["VARIANT_TASK_MISSING"]
                return result
            task_for_verification = variant["task"]
        else:
            fit["execution_policy"] = "PROCEED_ORIGINAL"
    _write_stage_json(task_root, "task_fit.json", fit)
    result["executed_task"] = task_for_verification
    if not support.get("allow_file_verifier"):
        result["verification"] = {
            "schema_version": "traceforge.reconstruction-verification.v1",
            "status": "NOT_APPLICABLE",
            "errors": ["NO_FILE_ACCEPTANCE"],
            "sft_eligible": False,
            "unverified_obligations": list(support.get("unverified_obligations") or []),
        }
        result["status"] = "REVIEW"
        result["stopped_at"] = "verification"
        result["errors"] = ["NO_FILE_ACCEPTANCE"]
        return result
    if verification_config is None:
        result.update(
            {
                "status": "PENDING_EXECUTION",
                "stopped_at": "verification",
                "errors": ["VERIFICATION_NOT_CONFIGURED"],
            }
        )
        return result
    verification = run_reconstruction_verification(
        task=task_for_verification,
        workspace_root=chosen["workspace"],
        model=verification_model,
        agent=verifier_agent or agent,
        output_root=task_root / "verification",
        config=verification_config,
        source=task_source,
        env_root=chosen.get("env_root"),
    )
    result["verification"] = verification
    sandbox_errors = _sandbox_init_errors(verification.get("errors"))
    if sandbox_errors:
        result["status"] = "REVIEW"
        result["stopped_at"] = "sandbox_init"
        result["errors"] = sandbox_errors
        return result
    result["status"] = verification.get("status", "REVIEW")
    if result["status"] == "READY" and variant is not None:
        result["status"] = "READY_VARIANT"
    result["stopped_at"] = None if result["status"] in {"READY", "READY_VARIANT"} else "verification"
    if result["status"] == "REVIEW":
        result["errors"] = list(verification.get("errors") or ["VERIFICATION_REVIEW"])
    return result


def run_eligible_reconstruction(
    *,
    raw_line: str,
    record: dict[str, Any] | None,
    agent: AgentRuntime,
    output_root: str | Path,
    verification_model: ChatModel | None = None,
    verifier_agent: AgentRuntime | None = None,
    verification_config: VerificationConfig | None = None,
    container_runtime_factory: Callable[[], Any] | None = None,
    source_override: dict[str, Any] | None = None,
) -> Path:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    if container_runtime_factory is not None:
        agent = SandboxedAgentRuntime(agent, container_runtime_factory)
        if verifier_agent is not None:
            verifier_agent = SandboxedAgentRuntime(verifier_agent, container_runtime_factory)
    if source_override is not None:
        source = copy.deepcopy(source_override)
    else:
        if record is None:
            raise EligibleReconstructionError("screened reconstruction requires a screening record")
        try:
            source = build_reconstruction_source(raw_line=raw_line, record=record)
        except ReconstructionSourceError as exc:
            raise EligibleReconstructionError(str(exc)) from exc
    write_reconstruction_source(source, root)
    screening_tasks = selected_task_views(source)
    if not screening_tasks:
        return _write_manifest(
            root,
            {
                "schema_version": ELIGIBLE_RECONSTRUCTION_SCHEMA,
                "status": "REVIEW",
                "stopped_at": "intent",
                "errors": ["筛选记录没有可重建的真实任务标签"],
            },
        )
    routed: list[tuple[dict[str, Any], dict[str, Any], Any, dict[str, Any]]] = []
    early: dict[str, dict[str, Any]] = {}
    prepared: dict[str, tuple[dict[str, Any], Any]] = {}
    for screening in screening_tasks:
        task_source, replay, support, _task_root = _replay_and_route(
            task=screening, root=root, source=source
        )
        prepared[str(screening["task_id"])] = (screening, replay)
        if not support.get("allow_completion"):
            early[str(screening["task_id"])] = _support_route_result(
                task=screening, support=support
            )
            continue
        routed.append((screening, task_source, replay, support))
    if not routed:
        intent: dict[str, Any] = {
            "schema_version": INTENT_SCHEMA,
            "status": "SKIPPED",
            "reason": "no file workspace path",
            "tasks": [],
        }
        results = [
            early[str(task["task_id"])]
            for task in screening_tasks
            if str(task["task_id"]) in early
        ]
        manifest = _write_eligible_manifest(root, source, intent, results, prepared)
        write_reconstruction_sft_curation(root, results)
        return manifest
    intent_source = copy.deepcopy(source)
    intent_source["tasks"] = []
    for screening, _task_source_value, _replay, _support in routed:
        intent_task = copy.deepcopy(screening)
        intent_task["relations"] = _task_relations(source, screening)
        intent_source["tasks"].append(intent_task)
    replay_files_by_task = {
        str(screening.get("task_id")): [
            str(item.path)
            for item in replay.files
            if getattr(item, "completeness", "COMPLETE") == "COMPLETE"
        ]
        for screening, _task_source_value, replay, _support in routed
    }
    try:
        intent = run_intent_recovery(
            source=intent_source,
            agent=agent,
            output_root=root / "intent",
            replay_files_by_task=replay_files_by_task,
        )
    except IntentRecoveryError as exc:
        intent = {
            "schema_version": INTENT_SCHEMA,
            "status": "REVIEW",
            "tasks": [{
                "task_id": item[0]["task_id"],
                "status": "REVIEW",
                "errors": [str(exc)],
            } for item in routed],
        }
    intent_by_id = {
        str((item.get("task") or {}).get("task_id") or item.get("task_id")): item
        for item in intent.get("tasks") or []
    }
    results = []
    for screening in screening_tasks:
        task_id = str(screening["task_id"])
        if task_id in early:
            results.append(early[task_id])
            continue
        routed_task = next((item for item in routed if str(item[0]["task_id"]) == task_id), None)
        item = intent_by_id.get(task_id)
        if item is None or item.get("status") != "READY":
            results.append(
                {
                    "task_id": task_id,
                    "status": "REVIEW",
                    "stopped_at": "intent",
                    "errors": list((item or {}).get("errors") or ["INTENT_REVIEW"]),
                    "execution_support_route": routed_task[3] if routed_task else None,
                }
            )
            continue
        assert routed_task is not None
        _screening, task_source, replay, support = routed_task
        results.append(
            _task_result(
                task=item["task"],
                root=root,
                source=source,
                agent=agent,
                verification_model=verification_model,
                verification_config=verification_config,
                verifier_agent=verifier_agent,
                replay=replay,
                support=support,
                task_source=task_source,
            )
        )
    manifest = _write_eligible_manifest(root, source, intent, results, prepared)
    write_reconstruction_sft_curation(root, results)
    return manifest


def run_raw_session_reconstruction(
    *,
    raw_line: str,
    line_number: int,
    source_ref: str,
    agent: AgentRuntime,
    output_root: str | Path,
    verification_model: ChatModel | None = None,
    verifier_agent: AgentRuntime | None = None,
    verification_config: VerificationConfig | None = None,
    container_runtime_factory: Callable[[], Any] | None = None,
) -> Path:
    """对一条完整原始 session 直入重建管线，不读取 screening records。"""
    from traceforge.reconstruction.raw_session import RawSessionSourceError, build_raw_session_source

    root = Path(output_root)
    try:
        source = build_raw_session_source(
            raw_line=raw_line,
            line_number=line_number,
            source_ref=source_ref,
            agent=agent,
            output_root=root / "session_segmentation",
        )
    except RawSessionSourceError as exc:
        raise EligibleReconstructionError(str(exc)) from exc
    return run_eligible_reconstruction(
        raw_line=raw_line,
        record=None,
        source_override=source,
        agent=agent,
        verifier_agent=verifier_agent,
        verification_model=verification_model,
        verification_config=verification_config,
        output_root=root,
        container_runtime_factory=container_runtime_factory,
    )
