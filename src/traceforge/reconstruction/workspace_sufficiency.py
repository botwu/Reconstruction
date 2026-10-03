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
    missing_binding_paths,
    workspace_is_stub_ensemble,
    workspace_task_context,
)
from traceforge.reconstruction.reconstructability import task_evidence_ref_ids
from traceforge.reconstruction.workspace_integrity import (
    classify_integrity_issues,
    inspect_workspace_integrity,
)

SUFFICIENCY_SCHEMA = "traceforge.workspace-sufficiency.v1"
SUFFICIENCY_PROMPT_VERSION = "workspace-sufficiency-agent-v17-target-grounded-validation"


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
    known_task_refs = sorted(task_evidence_ref_ids(task))
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
            "initial_required_paths 是初态输入；output_paths 是 solver 的目标产物，不要求提前存在。",
            "Return INSUFFICIENT when missing or damaged pre-task context blocks the operations needed to implement or verify the user's goal; the ability to edit source alone is not sufficient. Missing target behavior or the defect explicitly assigned to the solver is not itself a reconstruction gap.",
            "STATIC_INTEGRITY_REPORT is a read-only syntax/token diagnostic under the stated host Python version, not a completeness proof.",
            "Inspect every issue and classify it with issue_id, exact path, classification, and a concrete task-grounded reason. "
            "Use only issue_id and path values present in STATIC_INTEGRITY_REPORT; never invent additional issue IDs.",
            "BASELINE_TASK_DEFECT: the requested task itself requires fixing this observed defect; preserve it as the unsolved baseline.",
            "RECONSTRUCTION_GAP: required pre-task source/context is missing or damaged independently of the requested change.",
            "IRRELEVANT: the issue is outside the task's necessary execution/analysis path, or arises solely from a supported target Python version mismatch; justify with evidence.",
            "Do not infer these categories from keywords. Explain their relationship to the actual task and inspected source.",
            "classification_evidence_ref_ids 只能引用 TASK_EVIDENCE_REF_IDS 中的已有任务证据。"
            "以 BASELINE_TASK_DEFECT 或 IRRELEVANT 豁免缺失或读取诊断时，"
            "必须提供非空引用列表和具体理由；"
            "引用缺失、未知或格式错误时保留未决，不得编造或补齐。引用存在不代表分类正确："
            "仍须根据实际任务和源码解释关系，不能把任务无关的语法损坏归为 BASELINE_TASK_DEFECT。",
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
            "reset 会逐项比较两次 stdout、stderr、退出码及临时文件内容哈希。"
            "应实际调用原有入口，断言并输出稳定的业务结果；pytest 耗时、随机标识和带时间戳的文件字节"
            "不应充当业务结果。生成 Excel 等文件时，先重新读取并校验其业务内容，再清理临时二进制，"
            "保留稳定的内容记录供两次比较。业务结果不同仍须失败，不能只打印固定成功文本。"
            "如果探针自身包含不稳定信息，应在当前环境修正探针并重跑，不要求作者修改源码消除测试耗时或文件时间戳。",
            "实现功能的任务需要实际加载相关源码与依赖，不能用文件存在、源码可读或字符串匹配代替。"
            "确认原有入口能工作，再把缺失的目标功能留给 solver。"
            "涉及具体故障修复时，先依原用户目标说明实现和验证所必需的条件。"
            "区分原轨迹真实触发条件与探针构造输入；人工错误使初态抛错，不证明原故障致因或有效修复。"
            "真实错误文本及实际抛错代码支持、明确标注的受控场景可用于行为验证；"
            "说明原始依据、可区分未修与合理修复的可观察目标行为及未验证范围。"
            "核对后续真实成功与失败观察，不能把模拟冒称历史复现或唯一致因；"
            "不强加无依据的重试、请求参数阈值或部分数据处理策略。"
            "不得仅因缺少真实账户、完整生产回放或唯一致因证明就判定整个重建环境不足；"
            "若认为必需，须说明它与原用户目标的关系及为何没有可靠替代验证。"
            "若仍缺少验证原用户目标所必需的条件且无可靠替代，将缺口记入 missing_context，相关 task_fit 为 UNKNOWN；"
            "不能仅因源码、依赖和模拟接口可运行就宣称原目标可验证，也不要求提前实现修复。"
            "任务必需源码的 load 检查必须输出 module.__file__ 和该文件实际正文 SHA256，"
            "并断言加载的是候选预期路径；仅调整 sys.path 不证明加载来源。"
            "安装库成功只能证明该依赖可用，不能证明同名原路径完整；确需使用安装库时说明其"
            "位置、版本、哈希和原路径的片段范围，不能用它豁免任务必需源码的恢复缺口。"
            "语法损坏若阻碍必要入口加载，属于重建缺口；不能以用户没有要求运行测试为由豁免。",
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
            '"classification":"BASELINE_TASK_DEFECT|RECONSTRUCTION_GAP|IRRELEVANT","reason":"...",'
            '"classification_evidence_ref_ids":[]}],'
            '"task_fit":{"decision":"READY_ORIGINAL|INCOMPATIBLE|REVIEW_TASK_FIT",'
            '"reason":"...","requirements":[]},"variant_proposal":null,',
            '"environment_checks":[{"kind":"load|reset|dependency","probe_ids":[],"reason":"..."}]}',
            "TASK:",
            json.dumps(workspace_task_context(task), ensure_ascii=False),
            "TASK_EVIDENCE_REF_IDS:",
            json.dumps(known_task_refs, ensure_ascii=False),
            "WORKSPACE_ROOT: .",
            "工具路径使用工作区相对路径。run_environment_probe 的当前目录就是沙盒工作区根目录；"
            "探针可直接使用相对路径，如 Path('.')，"
            "需要绝对路径时读取 TRACEFORGE_WORKSPACE 环境变量。"
            "不要把宿主机路径复制到探针中，也不要猜测或硬编码沙盒绝对路径。",
            "RECONSTRUCTION_CONTEXT 中的范围和 PARTIAL 是历史回放事实，不等于当前候选仍有相同缺口。",
            "结合 current_sha256/current_matches_replay、当前补全 provenance 和 uncertainties 读取任务相关源码。",
            "candidate_execution_checks 是构建者真实执行的代码和回执。必须检查与任务相关的失败，"
            "不能用导入成功替代原有功能可用。checked_files_match 只比较该次已检查的文件；"
            "文件已变化、检查范围不当或异常被捕获时，应在当前候选独立重跑适当检查，不能直接沿用旧结论。",
            "candidate_completed_files 的补全和 capture_repairs 是待审候选，不是已证明的历史原文。"
            "必须用 read_session_message/read_session_context 读取相关原始源码观察或调用返回，不能只读用户要求。"
            "按 task_start_message_index 核对任务开始前已有的入口、调用接口、默认配置、持久化位置和行为，"
            "尤其检查临近起点的符号、文件大小和文档观察与较早 Replay 是否矛盾。"
            "候选自测通过不能证明它仍是原项目；删除或改名原有必要入口、另写相似程序属于重建缺口。"
            "将具体路径、原始消息号、观察与候选的差异记入 missing_context，能执行复现的接口差异同时用探针验证。"
            "允许有依据的缺失部分推断，但应保留已观察行为，不得提前实现用户目标。"
            "仍影响任务的采集损坏属于 RECONSTRUCTION_GAP，不能因为不属于用户目标就归为 BASELINE_TASK_DEFECT。",
            "字节改变不证明缺口已修复；独立判断当前环境。只有具体缺口影响任务时写入 missing_context，交回现有修复；无关 PARTIAL 可以 SUFFICIENT。",
            "RECONSTRUCTION_CONTEXT:",
            json.dumps(context, ensure_ascii=False),
            "STATIC_INTEGRITY_REPORT:",
            json.dumps(integrity, ensure_ascii=False),
        ]
    )
    if (workspace / ".traceforge/source-excerpts.json").is_file():
        instruction += "\n请查看 .traceforge/source-excerpts.json 索引及所需原始片段；缺失区间不能作为通过证据。"
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
    if not {"load", "reset", "dependency"} <= probe_kinds:
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
        "task_evidence_ref_ids": known_task_refs,
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
