"""把一个完整 session 编排为一个或多个独立的 Task--Environment 对。"""

from __future__ import annotations

import copy
import json
from itertools import count
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceforge.curation.sft import write_reconstruction_sft_curation
from traceforge.reconstruction.agents import AgentRuntime, SandboxedAgentRuntime
from traceforge.reconstruction.env_replay import (
    replay_task_workspace,
    write_replay_artifacts,
)
from traceforge.reconstruction.environment_bindings import (
    environment_bindings,
    non_file_obligation_ids,
    observed_body_paths,
    task_start_message_index,
)
from traceforge.reconstruction.intent_recovery import (
    INTENT_SCHEMA,
    IntentRecoveryError,
    run_intent_recovery,
    selected_task_views,
)
from traceforge.reconstruction.model_gateway import ChatModel
from traceforge.reconstruction.researcher import ReconstructionRuntime
from traceforge.reconstruction.session_parser import PARSER_MODEL, parse_session_tools
from traceforge.reconstruction.session_source import (
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
    environment_execution_blockers,
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
    completion_evidence_context,
    repair_workspace_completion,
)
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency

RAW_SESSION_RECONSTRUCTION_SCHEMA = "traceforge.raw-session-reconstruction.v1"
ENV_REPLAYED = "REPLAYED"
ENV_DEFAULT_EMPTY = "DEFAULT_EMPTY"
ENV_NONE = "NONE"
_SOURCE_SUFFIXES = frozenset(
    {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".hpp"}
)


def _domain_route(task: dict[str, Any], source: dict[str, Any]) -> str:
    nested = task.get("source_task") if isinstance(task.get("source_task"), dict) else {}
    for obj in (source, task, nested):
        if not isinstance(obj, dict):
            continue
        route = obj.get("domain_route")
        if isinstance(route, str) and route.strip():
            normalized = route.strip()
            return "terminal" if normalized in {"code_file", "terminal"} else normalized
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
    """根据已知领域和环境证据确定执行路径。"""

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
    if domain == "retrieval":
        return {
            **payload,
            "route": "RETRIEVAL_UNSUPPORTED",
            "reason_codes": ["RETRIEVAL_UNSUPPORTED"],
            "env_origin": ENV_NONE,
            "allow_completion": False,
            "allow_file_verifier": False,
        }
    if files:
        return {
            **payload,
            "route": "TERMINAL_FILE",
            "reason_codes": [],
            "env_origin": ENV_REPLAYED,
            "allow_completion": True,
            "selected_span_has_file_ops": has_ops,
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


class ReconstructionError(RuntimeError):
    """重建编排无法继续。"""


def _write_reconstruction_manifest(
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
        source_task, replay = prepared[task_id]
        try:
            pair = build_task_environment_pair(
                source_task=source_task,
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
        "schema_version": RAW_SESSION_RECONSTRUCTION_SCHEMA,
        "status": status,
        "stopped_at": stopped_at,
        "source": {
            "entry_mode": source.get("entry_mode", "RAW_SESSION"),
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
        raise ReconstructionError("Intent task_id 与会话任务分组不一致")
    spans = set(str(x) for x in anchor.get("span_ids") or [])
    if not spans:
        raise ReconstructionError("Intent 缺少原始任务的 span_ids")
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
    if not out.get("domain_route") and anchor.get("domain_route"):
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
    replay = replay_task_workspace(
        list(task_source.get("tool_timeline") or []),
        task_root / "initial_workspace",
        task_start=task_start_message_index(task),
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


MAX_ENVIRONMENT_REPAIR_ROUNDS = 2


def _environment_feedback(
    judge: dict[str, Any], environment: dict[str, Any],
) -> dict[str, Any]:
    """反馈只传诊断与真实探针，不将判断文本加入原始证据集合。"""
    return {
        "reason": judge.get("reason"),
        "missing_context": list(judge.get("missing_context") or []),
        "missing_binding_paths": list(judge.get("missing_binding_paths") or []),
        "integrity_report": judge.get("integrity_report") or {},
        "semantic_errors": list(judge.get("semantic_errors", judge.get("errors")) or []),
        "context_errors": list(environment.get("errors") or []),
        "context_status": environment.get("status"),
        "blockers": list(environment.get("blockers") or []),
        "execution_readiness": environment.get("execution_readiness"),
        "execution_errors": list(environment.get("execution_errors") or []),
        # PASS 只说明探针进程收据通过，stdout 与检查解释仍需供返修和复查使用。
        "environment_probes": list(judge.get("environment_probes") or []),
        "environment_checks": list(judge.get("environment_checks") or []),
        "failed_probes": [
            probe for probe in judge.get("environment_probes") or []
            if isinstance(probe, dict) and probe.get("status") != "PASS"
        ],
    }


def _repair_state(environment: dict[str, Any], feedback: dict[str, Any]) -> str:
    """忽略随机探针编号和说明文案，识别工作区与诊断均无变化的重复尝试。"""
    issues = (feedback.get("integrity_report") or {}).get("issues") or []
    # 同一探针重试、更换编号或输出顺序不算进展；新增成功探针属于有效进展。
    probes = {
        json.dumps(
            {key: probe.get(key) for key in ("purpose", "code_sha256", "status", "error_code")},
            sort_keys=True,
        )
        for probe in feedback["environment_probes"] if isinstance(probe, dict)
    }
    return json.dumps({
        "workspace": environment.get("workspace_sha256"),
        "context_status": feedback["context_status"],
        "context_errors": sorted(set(feedback["context_errors"])),
        "missing": sorted(feedback["missing_context"] + feedback["missing_binding_paths"]),
        "integrity": [
            {key: issue.get(key) for key in ("code", "path", "classification")}
            for issue in issues if isinstance(issue, dict)
        ],
        "execution_errors": sorted(feedback["execution_errors"]),
        "environment_probes": sorted(probes),
    }, ensure_ascii=False, sort_keys=True)


def _judge_and_repair_candidate(
    *, task: dict[str, Any], candidate: dict[str, Any], replay: Any,
    timeline: list[dict[str, Any]], task_source: dict[str, Any], agent: AgentRuntime,
    task_root: Path, index: int, origin: str,
    max_repair_rounds: int | None = MAX_ENVIRONMENT_REPAIR_ROUNDS,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """评估与具体执行反馈驱动返修；可由持续研究者取消固定返修轮数。"""
    observed = {str(item.path) for item in replay.files if getattr(item, "path", None)}
    observed.update(observed_body_paths(task_source))
    audit: dict[str, Any] = {
        "candidate_index": index, "max_repair_rounds": max_repair_rounds,
        "rounds": [], "stop_reason": None,
    }
    feedback = None
    previous_state = None
    action = "INITIAL"
    for round_index in count():
        round_root = task_root / "environment_repairs" / f"{index:03d}" / f"{round_index:03d}"
        judge_root = (
            task_root / "sufficiency" / f"{index:03d}" if round_index == 0
            else round_root / "sufficiency"
        )
        kwargs = {"repair_feedback": feedback} if feedback is not None else {}
        judge = run_workspace_sufficiency(
            task=task, observed_paths=sorted(observed), workspace_root=candidate["workspace"],
            agent=agent, output_root=judge_root,
            reconstruction_context=completion_evidence_context(replay, candidate), **kwargs,
        )
        environment = build_environment_contract(
            workspace_root=candidate["workspace"], env_root=candidate.get("env_root"),
            sufficiency=judge, replay=replay,
        )
        _write_stage_json(judge_root, "environment_contract.json", environment)
        audit["rounds"].append({
            "round": round_index, "action": action, "workspace": candidate["workspace"],
            "sufficiency_path": str(judge_root / "sufficiency.json"),
            "context_status": environment.get("status"),
            "execution_readiness": environment.get("execution_readiness"),
        })
        if round_index:
            audit["rounds"][-1]["feedback_path"] = str(round_root / "feedback.json")
            if action == "REPAIR":
                audit["rounds"][-1]["completion_path"] = str(
                    round_root / "completion" / "completion.json"
                )
        feedback = _environment_feedback(judge, environment)
        if environment.get("status") in {"INFRA_ERROR", "PIPELINE_ERROR"}:
            audit["stop_reason"] = environment["status"]
            break
        if any(probe.get("status") == "INFRA_ERROR" for probe in feedback["failed_probes"]):
            audit["stop_reason"] = "PROBE_INFRA_ERROR"
            break
        if (
            judge.get("status") == "READY" and environment.get("status") == "READY"
            and environment.get("execution_readiness") == "PROBED"
        ):
            audit["stop_reason"] = "READY"
            break
        state = _repair_state(environment, feedback)
        if state == previous_state:
            audit["stop_reason"] = "NO_PROGRESS"
            break
        previous_state = state
        if max_repair_rounds is not None and round_index >= max_repair_rounds:
            audit["stop_reason"] = "REPAIR_LIMIT_REACHED"
            break
        next_root = task_root / "environment_repairs" / f"{index:03d}" / f"{round_index + 1:03d}"
        _write_stage_json(next_root, "feedback.json", feedback)
        # 检查不完整或范围不当仍由检查员处理；确认初态缺口后才改源码。
        if (
            judge.get("label") != "INSUFFICIENT"
            and not feedback["missing_binding_paths"]
            and not any(issue.get("classification") == "RECONSTRUCTION_GAP"
                        for issue in feedback["integrity_report"].get("issues", []))
        ):
            action = "RECHECK"
            continue
        action = "REPAIR"
        repaired = repair_workspace_completion(
            task=task, candidate=candidate, replay=replay, timeline=timeline,
            source=task_source, agent=agent, output_root=next_root / "completion",
            feedback=feedback, env_origin=origin,
        )
        replacement = next((
            item for item in repaired.get("candidates") or []
            if item.get("decision") == "READY" and item.get("workspace")
        ), None)
        if repaired.get("status") != "READY" or replacement is None:
            audit["rounds"].append({
                "round": round_index + 1, "action": action,
                "completion_path": str(next_root / "completion" / "completion.json"),
                "completion_errors": list(repaired.get("errors") or []),
                "open_questions": list(repaired.get("open_questions") or []),
            })
            audit["stop_reason"] = "COMPLETION_REVIEW"
            break
        candidate = {**replacement, "index": index}
    audit["last_feedback"] = feedback
    _write_stage_json(task_root / "environment_repairs" / f"{index:03d}", "repair_audit.json", audit)
    return candidate, judge, environment, audit


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
    max_environment_repair_rounds: int | None = MAX_ENVIRONMENT_REPAIR_ROUNDS,
    completion_seed: dict[str, Any] | None = None,
    completion_feedback: dict[str, Any] | None = None,
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
    if completion_seed is not None:
        completion = repair_workspace_completion(
            task=task, candidate=completion_seed, replay=replay, timeline=timeline,
            source=task_source, agent=agent, output_root=task_root / "completion",
            feedback=completion_feedback or {}, env_origin=origin,
        )
    elif origin == ENV_DEFAULT_EMPTY:
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
    repair_audits = []
    for candidate_index, candidate in enumerate(completion.get("candidates") or []):
        if (
            not isinstance(candidate, dict)
            or candidate.get("decision") != "READY"
            or not candidate.get("workspace")
        ):
            judges.append({"label": "UNKNOWN", "decision": "REVIEW"})
            rows.append({"decision": "REVIEW"})
            continue
        candidate, judge, environment, repair_audit = _judge_and_repair_candidate(
            task=task, candidate=candidate, replay=replay, timeline=timeline,
            task_source=task_source, agent=agent, task_root=task_root,
            index=candidate_index, origin=origin,
            max_repair_rounds=max_environment_repair_rounds,
        )
        completion["candidates"][candidate_index] = candidate
        repair_audits.append(repair_audit)
        result["environment_repair_audit"] = repair_audits
        sandbox_errors = _sandbox_init_errors(judge.get("errors"))
        if sandbox_errors:
            result["sufficiency"] = judge
            result["stopped_at"] = "sandbox_init"
            result["errors"] = sandbox_errors
            return result
        judges.append(judge)
        index = len(rows)
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
    audit["environment_repairs"] = repair_audits
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
    # A context-ready snapshot may still be review-only. Do not let an
    # explicit Hermes request turn missing execution probes into a real run.
    if verification_config.execute_rollout:
        execution_blockers = environment_execution_blockers(environment)
        if execution_blockers:
            verification = {
                "schema_version": "traceforge.reconstruction-verification.v1",
                "status": "REVIEW",
                "errors": execution_blockers,
                "execution_gate": {
                    "status": "BLOCKED",
                    "blockers": execution_blockers,
                    "execution_readiness": environment.get("execution_readiness"),
                },
                "rollout": "SKIPPED",
                "sft_eligible": False,
                "unverified_obligations": [],
            }
            _write_stage_json(
                task_root / "verification", "execution_gate.json", verification["execution_gate"]
            )
            result["verification"] = verification
            result["status"] = "REVIEW"
            result["stopped_at"] = "verification"
            result["errors"] = execution_blockers
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


def run_prepared_task(
    *, source: dict[str, Any], task: dict[str, Any], agent: AgentRuntime,
    output_root: str | Path, verification_model: ChatModel | None = None,
    verifier_agent: AgentRuntime | None = None,
    verification_config: VerificationConfig | None = None,
    search_rollout_agent: AgentRuntime | None = None,
    max_environment_repair_rounds: int | None = MAX_ENVIRONMENT_REPAIR_ROUNDS,
    completion_seed: dict[str, Any] | None = None,
    completion_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """消费已解析的真实任务；外部构建入口复用正式后续流程，不重复解析。"""
    if (completion_seed is None) != (completion_feedback is None):
        raise ReconstructionError("恢复作者检查点必须同时提供候选与返修反馈")
    if (source.get("session_parser") or {}).get("status") != "READY":
        raise ReconstructionError("准备输入缺少成功的 Session Parser 产物")
    if task.get("task_id") not in {item.get("task_id") for item in source.get("tasks", [])}:
        raise ReconstructionError("任务不属于当前原始 session")
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    write_reconstruction_source(source, root)
    manifest = {"schema_version": RAW_SESSION_RECONSTRUCTION_SCHEMA, "status": "RUNNING",
                "source_sha256": source.get("line_sha256"), "tasks": []}
    _write_manifest(root, manifest)
    if _domain_route(task, source) == "retrieval":
        from traceforge.reconstruction.search_environment import run_search_task

        result = run_search_task(
            source=source, task=task, agent=agent,
            output_root=root / "tasks" / task["task_id"],
            rollout_agent=search_rollout_agent,
            rollout_trials=verification_config.rollout_trials if verification_config else 2,
            rollout_max_iterations=(
                verification_config.rollout_max_iterations if verification_config else 80
            ),
        )
    else:
        result = _run_task_loop(
            task=task, root=root, source=source, agent=agent,
            verification_model=verification_model, verifier_agent=verifier_agent,
            verification_config=verification_config,
            max_environment_repair_rounds=max_environment_repair_rounds,
            completion_seed=completion_seed, completion_feedback=completion_feedback,
        )
    _write_manifest(root, {**manifest, "status": result["status"], "tasks": [result]})
    return result


def _run_task_loop(
    *, task: dict[str, Any], root: Path, source: dict[str, Any], agent: AgentRuntime,
    verification_model: ChatModel | None, verification_config: VerificationConfig | None,
    verifier_agent: AgentRuntime | None = None,
    replay: Any | None = None, support: dict[str, Any] | None = None,
    task_source: dict[str, Any] | None = None,
    max_environment_repair_rounds: int | None = None,
    completion_seed: dict[str, Any] | None = None,
    completion_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """下游失败先独立复查初态，只把实证重建缺口返回原作者。"""
    audit: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attempt in count():
        attempt_root = root if attempt == 0 else root / "attempts" / f"{attempt:03d}"
        result = _task_result(
            task=task, root=attempt_root, source=source, agent=agent,
            verification_model=verification_model, verification_config=verification_config,
            verifier_agent=verifier_agent, replay=replay if attempt == 0 else None,
            support=support if attempt == 0 else None,
            task_source=task_source if attempt == 0 else None,
            max_environment_repair_rounds=max_environment_repair_rounds,
            completion_seed=completion_seed, completion_feedback=completion_feedback,
        )
        row = {"attempt": attempt, "root": str(attempt_root), "status": result["status"],
               "errors": result.get("errors", []), "action": "STOP"}
        audit.append(row)
        verification = result.get("verification") or {}
        rollout = verification.get("rollout")
        if (not isinstance(agent, ReconstructionRuntime)
                or result.get("stopped_at") != "verification" or not result.get("workspace")
                or result["status"] != "REVIEW" or verification.get("unverified_obligations")
                or "VERIFIER_CALIBRATION_INFRA_ERROR" in verification.get("errors", [])
                or _sandbox_init_errors(verification.get("errors"))):
            break
        if isinstance(rollout, dict) and (
            (rollout.get("execution") or {}).get("status") != "COMPLETED"
            or (rollout.get("results") or {}).get("quality_gate", {}).get("ok") is False
        ):
            row["stop_reason"] = "DOWNSTREAM_EXECUTION_INCOMPLETE"
            break
        candidate = result["completion"]["candidates"][result["selected_index"]]
        task_root = attempt_root / "tasks" / str(task["task_id"])
        feedback = {"stage": "verification", "verification": verification, "verdicts": []}
        if isinstance(rollout, dict):
            for trial in (rollout.get("results") or {}).get("trials", []):
                if trial.get("status") == "PASS" or not trial.get("verdict_path"):
                    continue
                path = Path(trial["verdict_path"]).resolve()
                if path.is_relative_to(task_root.resolve()) and path.is_file():
                    feedback["verdicts"].append(json.loads(path.read_text()))
        if replay is None:
            replay = replay_task_workspace(
                list(source.get("tool_timeline") or []), task_root / "initial_workspace",
                task_start=task_start_message_index(task),
            )
        diagnosis_root = task_root / "downstream_environment_review"
        judge = run_workspace_sufficiency(
            task=task, workspace_root=candidate["workspace"], agent=agent,
            output_root=diagnosis_root, observed_paths=observed_body_paths(source),
            reconstruction_context=completion_evidence_context(replay, candidate),
            repair_feedback={
                "downstream_failure": feedback,
                "instruction": "在原始初态复现下游暴露的问题，区分重建缺口、验证器问题和 solver 解题失败。"
                "只有任务目标之外的初态缺口才返回 INSUFFICIENT；用户要求修复的原缺陷必须保留。"
                "不要在 solver 修改后的工作区检查，不修改验收目标，不靠降低测试要求让它通过。",
            },
        )
        environment = build_environment_contract(
            workspace_root=candidate["workspace"], env_root=candidate.get("env_root"),
            sufficiency=judge, replay=replay,
        )
        diagnosis = _environment_feedback(judge, environment)
        row["diagnosis_path"] = str(diagnosis_root / "sufficiency.json")
        if (judge.get("label") != "INSUFFICIENT"
                or environment.get("status") in {"INFRA_ERROR", "PIPELINE_ERROR", ENVIRONMENT_UNRECONSTRUCTABLE}
                or any(p.get("status") == "INFRA_ERROR" for p in diagnosis["failed_probes"])
                or not (any(p.get("status") == "FAIL" for p in diagnosis["failed_probes"])
                        or diagnosis["missing_binding_paths"]
                        or any(i.get("classification") == "RECONSTRUCTION_GAP"
                               for i in diagnosis["integrity_report"].get("issues", [])))):
            row["stop_reason"] = "NO_CONFIRMED_INITIAL_ENVIRONMENT_GAP"
            break
        state = _repair_state(environment, diagnosis)
        if state in seen:
            row["stop_reason"] = "NO_PROGRESS"
            break
        seen.add(state)
        row["action"] = "REPAIR_INITIAL_ENVIRONMENT"
        completion_seed = candidate
        completion_feedback = {**diagnosis, "downstream_failure": feedback}
        _write_stage_json(diagnosis_root, "repair_feedback.json", completion_feedback)
        _write_stage_json(root / "tasks" / str(task["task_id"]), "task_repair_audit.json", {"attempts": audit})
    result["task_repair_audit"] = audit
    _write_stage_json(root / "tasks" / str(task["task_id"]), "task_repair_audit.json", {"attempts": audit})
    return result


def run_reconstruction(
    *,
    source: dict[str, Any],
    agent: AgentRuntime,
    output_root: str | Path,
    parser_model: ChatModel | None = None,
    parser_model_name: str = PARSER_MODEL,
    verification_model: ChatModel | None = None,
    verifier_agent: AgentRuntime | None = None,
    verification_config: VerificationConfig | None = None,
    container_runtime_factory: Callable[[], Any] | None = None,
) -> Path:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    source = copy.deepcopy(source)
    if parser_model is not None:
        source = parse_session_tools(
            source=source, model=parser_model, model_name=parser_model_name,
            output_root=root / "session_parser",
        )
    else:
        # 允许离线独立调试后续模块；正式 CLI 总是传入解析模型。
        source["session_parser"] = {"status": "NOT_RUN", "reason": "未提供会话解析模型"}
    write_reconstruction_source(source, root)
    if _domain_route({}, source) == "retrieval":
        from traceforge.reconstruction.agents import build_hermes_runtime
        from traceforge.reconstruction.search_environment import run_search_reconstruction

        rollout_agent = None
        if verification_config is not None and verification_config.execute_rollout:
            rollout_agent = build_hermes_runtime(
                config_path=verification_config.config_path,
                channel=verification_config.channel,
                model_name=verification_config.rollout_model,
                hermes_home=verification_config.hermes_home,
            )
        return run_search_reconstruction(
            source=source, agent=agent, output_root=root, rollout_agent=rollout_agent,
            rollout_trials=verification_config.rollout_trials if verification_config else 2,
            rollout_max_iterations=(verification_config.rollout_max_iterations
                                    if verification_config else 80),
        )
    native_agent, native_verifier = agent, verifier_agent
    if container_runtime_factory is not None:
        agent = SandboxedAgentRuntime(agent, container_runtime_factory)
        if verifier_agent is not None:
            verifier_agent = SandboxedAgentRuntime(verifier_agent, container_runtime_factory)
    source_tasks = selected_task_views(source)
    if not source_tasks:
        return _write_manifest(
            root,
            {
                "schema_version": RAW_SESSION_RECONSTRUCTION_SCHEMA,
                "status": "REVIEW",
                "stopped_at": "intent",
                "errors": ["会话分组没有任务"],
            },
        )
    routed: list[tuple[dict[str, Any], dict[str, Any], Any, dict[str, Any]]] = []
    early: dict[str, dict[str, Any]] = {}
    prepared: dict[str, tuple[dict[str, Any], Any]] = {}
    for source_task in source_tasks:
        task_source, replay, support, _task_root = _replay_and_route(
            task=source_task, root=root, source=source
        )
        prepared[str(source_task["task_id"])] = (source_task, replay)
        if not support.get("allow_completion"):
            early[str(source_task["task_id"])] = _support_route_result(
                task=source_task, support=support
            )
            continue
        routed.append((source_task, task_source, replay, support))
    if not routed:
        intent: dict[str, Any] = {
            "schema_version": INTENT_SCHEMA,
            "status": "SKIPPED",
            "reason": "no file workspace path",
            "tasks": [],
        }
        results = [
            early[str(task["task_id"])]
            for task in source_tasks
            if str(task["task_id"]) in early
        ]
        manifest = _write_reconstruction_manifest(root, source, intent, results, prepared)
        write_reconstruction_sft_curation(root, results)
        return manifest
    intent_source = copy.deepcopy(source)
    intent_source["tasks"] = []
    for source_task, _task_source_value, _replay, _support in routed:
        intent_task = copy.deepcopy(source_task)
        intent_task["relations"] = _task_relations(source, source_task)
        intent_source["tasks"].append(intent_task)
    # Replay.files 已排除修改后才观察到的正文；PARTIAL 初态片段也能定位任务，
    # 完整性仍由后续 Completion/Sufficiency 处理，不回退到未经屏障过滤的原始读取。
    replay_files_by_task = {
        str(source_task.get("task_id")): [
            str(item.path)
            for item in replay.files
            if item.completeness == "COMPLETE"
            or (item.completeness == "PARTIAL" and item.content)
        ]
        for source_task, _task_source_value, replay, _support in routed
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
    for source_task in source_tasks:
        task_id = str(source_task["task_id"])
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
        _source_task, task_source, replay, support = routed_task
        task_agent, task_verifier = agent, verifier_agent
        if container_runtime_factory is not None:
            task_agent = ReconstructionRuntime(
                native_agent, source=task_source, task=item["task"],
                runtime_factory=container_runtime_factory,
            )
            task_verifier = task_agent
            if native_verifier is not None and native_verifier is not native_agent:
                task_verifier = ReconstructionRuntime(
                    native_verifier, source=task_source, task=item["task"],
                    runtime_factory=container_runtime_factory,
                )
                task_verifier.conversation = task_agent.conversation
                task_verifier.phases = task_agent.phases
        results.append(
            _run_task_loop(
                task=item["task"],
                root=root,
                source=source,
                agent=task_agent,
                verification_model=verification_model,
                verification_config=verification_config,
                verifier_agent=task_verifier,
                replay=replay,
                support=support,
                task_source=task_source,
            )
        )
    manifest = _write_reconstruction_manifest(root, source, intent, results, prepared)
    write_reconstruction_sft_curation(root, results)
    return manifest


def run_raw_session_reconstruction(
    *,
    raw_line: str,
    domain: str,
    line_number: int,
    source_ref: str,
    agent: AgentRuntime,
    output_root: str | Path,
    parser_model: ChatModel | None = None,
    parser_model_name: str = PARSER_MODEL,
    verification_model: ChatModel | None = None,
    verifier_agent: AgentRuntime | None = None,
    verification_config: VerificationConfig | None = None,
    container_runtime_factory: Callable[[], Any] | None = None,
) -> Path:
    """对一条完整原始 session 直入重建管线，不读取筛选记录。"""
    from traceforge.reconstruction.raw_session import RawSessionSourceError, build_raw_session_source

    if domain not in {"search", "terminal"}:
        raise ReconstructionError("原始 session 必须显式指定 domain：search 或 terminal")
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
        raise ReconstructionError(str(exc)) from exc
    source["domain_route"] = "retrieval" if domain == "search" else "terminal"
    source["input_domain"] = domain
    return run_reconstruction(
        source=source,
        parser_model=parser_model,
        parser_model_name=parser_model_name,
        agent=agent,
        verifier_agent=verifier_agent,
        verification_model=verification_model,
        verification_config=verification_config,
        output_root=root,
        container_runtime_factory=container_runtime_factory,
    )
