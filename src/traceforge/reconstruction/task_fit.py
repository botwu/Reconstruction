"""在补全环境上拟合原任务，并验证一次受控变体；不改写环境。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents import SUFFICIENCY_ROLE, AgentRuntime, AgentSession
from traceforge.reconstruction.agents.session import safe_relpath, workspace_tree_hash
from traceforge.reconstruction.environment_bindings import environment_bindings
from traceforge.reconstruction.reconstructability import assess_reconstructability

ENVIRONMENT_CONTRACT_SCHEMA = "traceforge.environment-contract.v1"
TASK_CONTRACT_SCHEMA = "traceforge.task-contract.v1"
TASK_FIT_SCHEMA = "traceforge.task-fit.v1"
TASK_VARIANT_SCHEMA = "traceforge.task-variant.v1"
ENVIRONMENT_UNRECONSTRUCTABLE = "SKIPPED_UNRECONSTRUCTABLE"
TRANSFORMATIONS = frozenset({
    "EQUIVALENT_TARGET", "EQUIVALENT_INTERFACE", "SCOPE_REDUCTION", "OUTPUT_ADAPTATION",
})
CONFLICTS = frozenset({"TARGET_UNAVAILABLE", "INTERFACE_UNAVAILABLE", "CONSTRAINT_CONFLICT"})


class TaskFitError(ValueError):
    """任务拟合结果不满足证据与派生契约。"""


def _initial_binding_paths(binding: dict[str, Any]) -> list[str]:
    paths = binding.get("initial_required_paths")
    if isinstance(paths, list):
        return [path for path in paths if isinstance(path, str) and path]
    return [path for path in binding.get("required_paths") or [] if isinstance(path, str) and path]


def artifact_hash(value: Any) -> str:
    """稳定摘要用于绑定原任务、环境快照和变体。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def write_contract(root: Path, name: str, value: dict[str, Any]) -> Path:
    """持久化门禁产物；原任务和变体使用不同目录。"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _path_present(path: str, files: dict[str, str]) -> bool:
    if not isinstance(path, str) or safe_relpath(path) is None:
        return False
    value = path.rstrip("/")
    return value in files or any(item.startswith(value + "/") for item in files)


def build_environment_contract(
    *, workspace_root: str | Path, sufficiency: dict[str, Any], replay: Any = None,
    env_root: str | Path | None = None,
) -> dict[str, Any]:
    """建立上下文契约，并单独记录后续执行是否已探测。

    补全后的工作区先回答一个问题：它是否保留了足够的、有依据的任务
    上下文。load/reset/dependency 探测属于后续执行就绪证据，不能把一个
    已经足够的上下文重新降级成 ``REVIEW``。探测失败仍然完整记录，交给
    Verifier/Harbor 的真实执行阶段处理。
    """
    workspace = Path(workspace_root)
    inventory = workspace_tree_hash(workspace) if workspace.is_dir() else {}
    assessment = assess_reconstructability(sufficiency, replay)
    # A sufficiency result can be semantically ready while an execution
    # preflight is unavailable. Keep confirmed reconstruction blockers intact,
    # but do not turn a preflight-only failure into a context rejection.
    preflight = sufficiency.get("execution_preflight")
    preflight_errors = {
        str(error)
        for error in (preflight.get("errors") if isinstance(preflight, dict) else [])
        if isinstance(error, str)
    }
    assessment_errors = [str(error) for error in assessment.get("errors") or []]
    if (
        sufficiency.get("semantic_status") == "READY"
        and preflight_errors
        and assessment["status"] in {"INFRA_ERROR", "PIPELINE_ERROR"}
        and assessment_errors
        and set(assessment_errors) <= preflight_errors
    ):
        assessment = {**assessment, "status": "READY", "errors": []}
    context_errors = list(assessment.get("errors") or [])
    status = assessment["status"]
    probes = {
        p["probe_id"]: p for p in sufficiency.get("environment_probes", [])
        if isinstance(p, dict) and isinstance(p.get("probe_id"), str)
    }
    checks = sufficiency.get("environment_checks")
    checks = checks if isinstance(checks, list) else []
    checked: set[str] = set()
    execution_errors: list[str] = []
    # 静态证据已确认不可重建时，无需再要求执行探针。
    checks_required = status not in {ENVIRONMENT_UNRECONSTRUCTABLE, "INFRA_ERROR"}
    for check in checks if checks_required else []:
        if not isinstance(check, dict):
            execution_errors.append("ENVIRONMENT_CHECK_INVALID")
            continue
        kind = check.get("kind")
        refs = check.get("probe_ids")
        if kind not in {"load", "reset", "dependency"} or kind in checked:
            execution_errors.append("ENVIRONMENT_CHECK_KIND_INVALID")
            continue
        checked.add(kind)
        if not isinstance(refs, list) or not refs or not str(check.get("reason") or "").strip():
            execution_errors.append(f"ENVIRONMENT_CHECK_EVIDENCE_REQUIRED:{kind}")
            continue
        for ref in refs:
            probe = probes.get(ref) if isinstance(ref, str) else None
            if (
                probe is None or probe.get("purpose") != kind
                or probe.get("status") != "PASS"
                or probe.get("environment_unchanged") is not True
                or (kind == "reset" and probe.get("reproducible") is not True)
            ):
                execution_errors.append(f"ENVIRONMENT_PROBE_NOT_PASS:{kind}")
                continue
            executions = probe.get("executions")
            if (
                probe.get("workspace_before") != inventory
                or probe.get("workspace_after") != inventory
                or not isinstance(executions, list)
                or len(executions) != (2 if kind == "reset" else 1)
                or any(
                    not isinstance(item, dict)
                    or type(item.get("exit_code")) is not int
                    or item["exit_code"] != 0
                    or item.get("timed_out") is not False
                    or item.get("workspace_after") != inventory
                    for item in executions
                )
            ):
                execution_errors.append(f"ENVIRONMENT_PROBE_RECEIPT_INVALID:{kind}")
    if checks_required and checked != {"load", "reset", "dependency"}:
        execution_errors.append("ENVIRONMENT_PROBES_REQUIRED")
    if not workspace.is_dir():
        context_errors.append("WORKSPACE_NOT_FOUND")
        status = "PIPELINE_ERROR"
    expected_hashes = sufficiency.get("workspace_hashes")
    if isinstance(expected_hashes, dict) and expected_hashes != inventory:
        context_errors.append("ENVIRONMENT_CHANGED_AFTER_SUFFICIENCY")
        status = "PIPELINE_ERROR"
    context_errors = sorted(set(context_errors))
    execution_errors = sorted(set(execution_errors))
    # Context readiness is independent from execution readiness. A missing or
    # failed probe is evidence for the execution stage, not reconstruction
    # failure and not a reason to discard the candidate before Verifier.
    if status == "READY" and context_errors:
        status = "REVIEW"
    if status in {ENVIRONMENT_UNRECONSTRUCTABLE, "INFRA_ERROR", "PIPELINE_ERROR"}:
        execution_readiness = "NOT_APPLICABLE"
    elif checked == {"load", "reset", "dependency"} and not execution_errors:
        execution_readiness = "PROBED"
    elif execution_errors:
        execution_readiness = "FAILED"
    else:
        execution_readiness = "UNPROBED"
    return {
        "schema_version": ENVIRONMENT_CONTRACT_SCHEMA,
        "status": status,
        "context_status": status,
        "workspace_hashes": inventory,
        "workspace_sha256": artifact_hash(inventory),
        "workspace_ref": str(workspace),
        "env_root_ref": str(Path(env_root).resolve()) if env_root else None,
        "facts": {"paths": sorted(inventory)},
        "checks": checks,
        "probes": list(probes.values()),
        "blockers": list(assessment.get("blockers") or []),
        "errors": context_errors,
        "execution_errors": execution_errors,
        "execution_readiness": execution_readiness,
        "execution_status": (
            "EXECUTABLE" if execution_readiness == "PROBED"
            else ("NOT_APPLICABLE" if execution_readiness == "NOT_APPLICABLE" else "UNEXECUTABLE")
        ),
        "limitations": [
            "探测只覆盖任务所需的入口、依赖与可重复初态，不是任意程序可解性的证明。",
            "reset 探测在同一只读快照上使用新的临时目录重复执行，不包含外部服务复位。",
        ],
    }


def environment_execution_blockers(environment: dict[str, Any] | None) -> list[str]:
    """Return deterministic blockers for a real verifier/Hermes execution.

    READY certifies that the reconstructed context is present. A real agent run
    additionally needs immutable load/reset/dependency probes. This keeps a
    review snapshot from being presented as an executable task.
    """
    if not isinstance(environment, dict):
        return ["ENVIRONMENT_CONTRACT_MISSING"]
    blockers: list[str] = []
    if environment.get("status") != "READY":
        blockers.append(f"ENVIRONMENT_CONTEXT_{environment.get('status', 'UNKNOWN')}")
    if environment.get("execution_readiness") != "PROBED":
        blockers.append("ENVIRONMENT_EXECUTION_UNREADY")
    for error in environment.get("execution_errors") or []:
        if isinstance(error, str) and error:
            blockers.append(error)
    probes = environment.get("probes")
    if not isinstance(probes, list) or not probes:
        blockers.append("ENVIRONMENT_PROBES_REQUIRED")
    return list(dict.fromkeys(blockers))


def build_task_contract(*, task: dict[str, Any]) -> dict[str, Any]:
    """原始用户意图只复制不改写；派生任务另存。"""
    for key in ("task_id", "task_instruction", "core_objective"):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise TaskFitError(f"原任务缺少 {key}")
    obligations = task.get("acceptance_obligations")
    if not isinstance(obligations, list) or not obligations:
        raise TaskFitError("原任务缺少验收义务")
    ids = [item.get("id") for item in obligations if isinstance(item, dict)]
    if len(ids) != len(obligations) or len(set(ids)) != len(ids) or not all(ids):
        raise TaskFitError("原任务义务 ID 必须非空且唯一")
    return {
        "schema_version": TASK_CONTRACT_SCHEMA,
        "task_id": task["task_id"],
        "source_task_hash": artifact_hash(task),
        "original_task": copy.deepcopy(task),
        "core_intent": task["core_objective"],
        "acceptance_obligations": copy.deepcopy(obligations),
        "environment_bindings": copy.deepcopy(environment_bindings(task)),
    }


def fit_task_environment(
    *, environment: dict[str, Any], task: dict[str, Any], agent_fit: Any = None,
) -> dict[str, Any]:
    """按义务核对拟合证据；执行失败、未探测和未知项均不能触发变体。"""
    result = {
        "schema_version": TASK_FIT_SCHEMA,
        "decision": "REVIEW_TASK_FIT",
        "variant_eligible": False,
        "source_task_hash": task["source_task_hash"],
        "environment_sha256": environment["workspace_sha256"],
        "requirements": [],
        "errors": [],
    }
    if environment["status"] != "READY":
        result["decision"] = environment["status"]
        result["errors"] = list(environment.get("errors") or [])
        return result
    if not isinstance(agent_fit, dict):
        files = environment["workspace_hashes"]
        requirements = []
        for binding in task["environment_bindings"]:
            required = _initial_binding_paths(binding)
            missing = [path for path in required if not _path_present(path, files)]
            requirements.append(
                {
                    "obligation_id": binding["obligation_id"],
                    "status": "UNSATISFIED" if missing else "SATISFIED",
                    "evidence_paths": required,
                    "reason": "静态绑定路径检查",
                }
            )
        result["requirements"] = requirements
        result["decision"] = (
            "READY_ORIGINAL"
            if all(item["status"] == "SATISFIED" for item in requirements)
            else "INCOMPATIBLE"
        )
        result["errors"] = []
        result["fit_source"] = "STATIC_BINDING_COMPATIBILITY"
        return result
    requirements = agent_fit.get("requirements")
    if not isinstance(requirements, list):
        result["errors"] = ["TASK_FIT_REQUIREMENTS_REQUIRED"]
        return result
    expected = {item["id"] for item in task["acceptance_obligations"]}
    seen: set[str] = set()
    requirements = copy.deepcopy(requirements)
    for item in requirements:
        if isinstance(item, dict) and isinstance(item.get("status"), str):
            item["status"] = item["status"].strip().upper()
    probes = {p["probe_id"]: p for p in environment["probes"]}
    errors: list[str] = []
    for item in requirements:
        if not isinstance(item, dict):
            errors.append("TASK_FIT_REQUIREMENT_INVALID")
            continue
        oid = item.get("obligation_id")
        if not isinstance(oid, str) or oid not in expected or oid in seen:
            errors.append("TASK_FIT_OBLIGATION_INVALID")
            continue
        seen.add(oid)
        if item.get("status") not in {"SATISFIED", "UNSATISFIED", "UNKNOWN"}:
            errors.append(f"TASK_FIT_STATUS_INVALID:{oid}")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            errors.append(f"TASK_FIT_REASON_REQUIRED:{oid}")
        paths = item.get("evidence_paths", [])
        refs = item.get("probe_ids", [])
        if paths is None:
            paths = []
        if refs is None:
            refs = []
        if not isinstance(paths, list) or not isinstance(refs, list):
            errors.append(f"TASK_FIT_EVIDENCE_INVALID:{oid}")
            continue
        binding = next(
            (
                value for value in task["environment_bindings"]
                if value.get("obligation_id") == oid
            ),
            {},
        )
        if binding.get("verifier_kind") != "NON_FILE" and not (paths or refs):
            errors.append(f"TASK_FIT_EVIDENCE_REQUIRED:{oid}")
            continue
        if any(not _path_present(p, environment["workspace_hashes"]) for p in paths):
            errors.append(f"TASK_FIT_PATH_UNKNOWN:{oid}")
        if any(not isinstance(ref, str) or ref not in probes for ref in refs):
            errors.append(f"TASK_FIT_PROBE_UNKNOWN:{oid}")
        if item.get("status") == "UNSATISFIED":
            conflict_probes = [probes[ref] for ref in refs if isinstance(ref, str) and ref in probes]
            if (
                item.get("conflict_kind") not in CONFLICTS
                or item.get("repairable_within_task") is not False
                or not any(
                    p.get("purpose") == "task_conflict" and p.get("status") == "FAIL"
                    and p.get("reproducible") is True and p.get("environment_unchanged") is True
                    for p in conflict_probes
                )
            ):
                errors.append(f"TASK_CONFLICT_UNCONFIRMED:{oid}")
    if seen != expected:
        errors.append("TASK_FIT_OBLIGATION_COVERAGE")
    result["requirements"] = copy.deepcopy(requirements)
    result["errors"] = errors
    if errors:
        return result
    statuses = {item["status"] for item in requirements}
    raw_decision = agent_fit.get("decision")
    decision = raw_decision.strip().upper() if isinstance(raw_decision, str) else raw_decision
    if "UNKNOWN" in statuses:
        # Evidence that a path was observed proves only observability. It does
        # not prove that the obligation is satisfied, so keep UNKNOWN as an
        # explicit audit result and let downstream verification decide.
        result["errors"] = [
            *(result.get("errors") or []),
            *[
                f"TASK_FIT_UNKNOWN:{item['obligation_id']}"
                for item in requirements
                if item["status"] == "UNKNOWN"
            ],
        ]
        result["requirements"] = copy.deepcopy(requirements)
        return result
    if statuses == {"SATISFIED"} and decision == "READY_ORIGINAL":
        result["decision"] = "READY_ORIGINAL"
    elif "UNSATISFIED" in statuses and decision == "INCOMPATIBLE":
        result["decision"] = "INCOMPATIBLE"
        result["variant_eligible"] = True
    else:
        result["errors"] = ["TASK_FIT_DECISION_CONFLICT"]
    return result


def build_task_variant(
    *, parent_task: dict[str, Any], environment: dict[str, Any], fit: dict[str, Any],
    proposal: Any,
) -> dict[str, Any]:
    """仅替换已证实冲突的义务，保留其他义务、约束和领域目标。"""
    if (
        environment["status"] != "READY" or environment.get("execution_readiness") != "PROBED"
        or fit.get("decision") != "INCOMPATIBLE" or fit.get("variant_eligible") is not True
    ):
        raise TaskFitError("环境未通过或原任务冲突未证实，不允许生成变体")
    if not isinstance(proposal, dict):
        raise TaskFitError("变体提案必须为对象")
    if proposal.get("decision") == "NO_VALID_VARIANT":
        return {"schema_version": TASK_VARIANT_SCHEMA, "status": "REJECTED", "reason":
                str(proposal.get("reason") or "没有保留核心意图的变体")}
    instruction = proposal.get("task_instruction")
    changes = proposal.get("changed_requirements")
    if not isinstance(instruction, str) or not instruction.strip():
        raise TaskFitError("变体缺少独立执行指令")
    if not isinstance(changes, list) or not changes:
        raise TaskFitError("变体缺少逐义务变更记录")
    original = parent_task["original_task"]
    transformed = copy.deepcopy(original)
    by_id = {item["id"]: item for item in transformed["acceptance_obligations"]}
    bindings = {item["obligation_id"]: copy.deepcopy(item)
                for item in parent_task["environment_bindings"]}
    allowed = {item["obligation_id"] for item in fit["requirements"]
               if item["status"] == "UNSATISFIED"}
    seen: set[str] = set()
    for change in changes:
        if not isinstance(change, dict):
            raise TaskFitError("变体义务变更必须为对象")
        oid = change.get("obligation_id")
        if not isinstance(oid, str) or oid not in allowed or oid in seen:
            raise TaskFitError("变体只能修改有冲突证据的义务且不能重复")
        seen.add(oid)
        if change.get("transformation") not in TRANSFORMATIONS:
            raise TaskFitError("变体转换类型不受支持")
        for key in ("text", "reason"):
            if not isinstance(change.get(key), str) or not change[key].strip():
                raise TaskFitError(f"变体变更缺少 {key}")
        binding = change.get("environment_binding")
        paths = binding.get("required_paths") if isinstance(binding, dict) else None
        if (
            not isinstance(binding, dict) or binding.get("obligation_id") != oid
            or binding.get("verifier_kind") != bindings.get(oid, {}).get("verifier_kind")
            or not isinstance(paths, list)
            or not isinstance(binding.get("observable"), str) or not binding["observable"].strip()
            or (binding.get("verifier_kind") == "FILE" and not paths)
            or (binding.get("verifier_kind") == "NON_FILE" and paths)
            or any(not _path_present(p, environment["workspace_hashes"]) for p in paths)
        ):
            raise TaskFitError("变体验收绑定未指向补全环境或改变了验收类型")
        by_id[oid]["text"] = change["text"]
        by_id[oid]["environment_binding"] = copy.deepcopy(binding)
        by_id[oid]["derivation"] = {"parent_obligation_id": oid, "reason": change["reason"]}
        bindings[oid] = copy.deepcopy(binding)
    if seen != allowed:
        raise TaskFitError("变体没有处理所有已确认的冲突义务")
    transformed["task_instruction"] = instruction
    transformed["environment_bindings"] = list(bindings.values())
    transformed["success_criteria"] = [item["text"] for item in transformed["acceptance_obligations"]]
    variant_id = "variant_" + artifact_hash({
        "parent": parent_task["source_task_hash"], "environment": environment["workspace_sha256"],
        "instruction": instruction, "changes": changes,
    })[:20]
    transformed["task_id"] = variant_id
    transformed["parent_task_id"] = parent_task["task_id"]
    transformed["derivation_kind"] = "ENVIRONMENT_GROUNDED_VARIANT"
    return {
        "schema_version": TASK_VARIANT_SCHEMA, "status": "PROPOSED",
        "variant_task_id": variant_id, "parent_task_id": parent_task["task_id"],
        "source_task_hash": parent_task["source_task_hash"],
        "environment_sha256": environment["workspace_sha256"],
        "changed_requirements": copy.deepcopy(changes),
        "unchanged_core_intent": parent_task["core_intent"],
        "unchanged_obligation_ids": sorted(set(by_id) - seen),
        "acceptance_criteria": transformed["success_criteria"],
        "task": transformed,
    }


def generate_task_variant(
    *, task: dict[str, Any], environment: dict[str, Any], fit: dict[str, Any],
    workspace_root: Path, agent: AgentRuntime, output_root: Path,
) -> dict[str, Any]:
    """过滤模块内的一次提案调用；只看公开环境、原任务和失败证据。"""
    if environment["status"] != "READY" or fit.get("variant_eligible") is not True:
        raise TaskFitError("变体提案前置条件未满足")
    session = AgentSession(workspace=workspace_root, allow_write=False)
    prompt = "\n".join([
        "你正在环境过滤模块中生成最多一个任务变体。只读环境，不得修改或补造任何资源。",
        "输入是补全且探测通过的环境、原始用户意图和拟合任务的已证实冲突。",
        "不得把缺失的目标功能、一次解题失败、超时或API故障当作冲突。",
        "只修改 UNSATISFIED 义务；保留全部义务、核心目标、强制约束和禁止事项。",
        "只允许等价目标/接口、范围缩小或输出适配。不得改成打印、探测、报告或固定答案。",
        "不得使用任何 solution/隐藏测试/原始终态。必要时 read_file 查看公开初态。",
        '无合理变体返回 {"decision":"NO_VALID_VARIANT","reason":"..."}。',
        '否则返回 {"decision":"PROPOSE","task_instruction":"完整的新任务指令",',
        '"changed_requirements":[{"obligation_id":"原义务ID",',
        '"transformation":"EQUIVALENT_TARGET|EQUIVALENT_INTERFACE|SCOPE_REDUCTION|OUTPUT_ADAPTATION",',
        '"text":"新验收义务","reason":"最小改动与核心意图保持的理由",',
        '"environment_binding":{"obligation_id":"同一ID","required_paths":["真实路径"],',
        '"observable":"足以证明完成的状态","verifier_kind":"保留原值"}}]}',
        "ORIGINAL_TASK=" + json.dumps(task, ensure_ascii=False),
        "ENVIRONMENT=" + json.dumps(environment, ensure_ascii=False),
        "FITTED_TASK=" + json.dumps(fit, ensure_ascii=False),
    ])
    ran = agent.run(role=SUFFICIENCY_ROLE, instruction=prompt, session=session,
                    output_root=output_root)
    errors = list(ran.errors) + list(session.policy_errors)
    if not ran.completed:
        errors.append("AGENT_INCOMPLETE")
    if getattr(agent, "backend", "") != "hermes-sandbox":
        errors.append("REAL_PROBE_REQUIRED")
    if session.read_only_probe_blocked is not True:
        errors.append("READ_ONLY_PROBE_NOT_CONFIRMED")
    if not session.sandbox_stopped or session.sandbox_cleanup_error:
        errors.append("SANDBOX_CLEANUP_NOT_CONFIRMED")
    if workspace_tree_hash(workspace_root) != environment["workspace_hashes"]:
        errors.append("VARIANT_ENVIRONMENT_CHANGED")
    write_contract(output_root / "private", "model_exchange.json", {
        "prompt": prompt, "prompt_sha256": artifact_hash(prompt),
        "response": ran.final_text, "errors": errors,
    })
    if errors:
        return {"schema_version": TASK_VARIANT_SCHEMA, "status": "REVIEW", "errors": errors}
    try:
        return build_task_variant(parent_task=task, environment=environment, fit=fit,
                                  proposal=ran.payload)
    except TaskFitError as exc:
        return {"schema_version": TASK_VARIANT_SCHEMA, "status": "PIPELINE_ERROR",
                "errors": ["VARIANT_CONTRACT_INVALID"], "reason": str(exc)}
