"""Intent Agent：从会话任务分组和原始证据恢复真实任务。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents import INTENT_ROLE, AgentRuntime, AgentSession
from traceforge.reconstruction.environment_bindings import (
    FILE,
    _READ_CODE,
    attach_bindings_to_obligations,
    collect_allowed_paths,
    collect_binding_path_aliases,
    collect_file_binding_paths,
    mentioned_allowed_paths,
    normalize_environment_bindings,
)
from traceforge.reconstruction.session_parser import indexed_system_messages
from traceforge.task_instruction import grounded_response_contract, render_task_instruction

INTENT_SCHEMA = "traceforge.intent-recovery.v3"
INTENT_PROMPT_VERSION = "intent-recovery-agent-v16-qualified-obligations"
_STUB_OBSERVABLE = "replayed excerpts still present"
_REVIEW_ONLY = re.compile(
    r"(?i)(只读(?:代码)?(?:评审|审查)|只审查(?:并)?不修改|只查看.*不修改|"
    r"read[- ]only\s+(?:code\s+)?review|review[- ]only|"
    r"do not modify.*(?:review|audit))"
)
_IMPLEMENTATION_ACTION = re.compile(
    r"(?i)(实现|修复|修改|新增|重构|编写|测试|补丁|"
    r"\b(?:implement|fix|change|add|refactor|write|test|patch)\b)"
)


class IntentRecoveryError(RuntimeError):
    """Intent Agent 无法安全产出任务。"""


def _message_text(message: Any) -> str:
    if not isinstance(message, dict): return ""
    content = message.get("content")
    if isinstance(content, str): return content.strip()
    if isinstance(content, list):
        return "".join(str(x.get("text") or x.get("value") or "") if isinstance(x, dict) else str(x) for x in content).strip()
    if isinstance(content, dict): return str(content.get("text") or content.get("value") or "").strip()
    return ""


def _task_user_records(source: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    raw = source.get("raw_session") if isinstance(source.get("raw_session"), dict) else {}
    messages = raw.get("messages") if isinstance(raw.get("messages"), list) else []
    indices = task.get("message_indices") or []
    records: list[dict[str, Any]] = []
    for index in indices:
        if not isinstance(index, int) or index < 0 or index >= len(messages): continue
        msg = messages[index]
        if not isinstance(msg, dict) or msg.get("role") != "user": continue
        text = _message_text(msg)
        if text: records.append({"id": f"user:{index}", "message_index": index, "text": text})
    if not records:
        for text in task.get("user_texts") or []:
            if isinstance(text, str) and (clean := text.strip()):
                records.append({"id": f"user:task:{len(records)}", "message_index": None, "text": clean})
    dedup: dict[str, dict[str, Any]] = {}
    for item in records: dedup.setdefault(item["id"], item)
    return list(dedup.values())


def selected_task_views(source: dict[str, Any]) -> list[dict[str, Any]]:
    """读取会话分组得到的全部任务，不按成功、失败或分数筛选。"""
    return [task for task in source.get("tasks") or [] if isinstance(task, dict)]


def _tool_names(source: dict[str, Any]) -> list[str]:
    names = []
    for item in source.get("tool_timeline") or []:
        if isinstance(item, dict) and item.get("name"): names.append(str(item["name"]))
    return list(dict.fromkeys(names))


def deepen_requires_file(
    user_blob: str,
    file_binding_paths: list[str] | None,
    *,
    domain_route: str = "",
) -> bool:
    """Stage1 有正文且锚点能落到这些文件时，深化 q 必须带 FILE 义务。"""

    if not file_binding_paths:
        return False
    review_text = user_blob or ""
    # Negative constraints such as “不修改” and “do not modify” must not
    # themselves count as an implementation request.
    action_text = re.sub(
        r"(?i)(?:不|不要|无需)修改|do not modify(?:ing)?|without modifying",
        " ",
        review_text,
    )
    if _REVIEW_ONLY.search(review_text) and not _IMPLEMENTATION_ACTION.search(action_text):
        return False
    if mentioned_allowed_paths(user_blob, file_binding_paths):
        return True
    if _READ_CODE.search(user_blob or ""):
        return True
    return domain_route.strip() in {"code_file", "terminal"}


def _file_obligation_ready(bindings: list[dict[str, Any]]) -> bool:
    for item in bindings:
        if item.get("verifier_kind") != FILE:
            continue
        if not item.get("required_paths"):
            continue
        observable = str(item.get("observable") or "").strip()
        if observable and observable.lower() != _STUB_OBSERVABLE:
            return True
    return False


def _prompt(
    source: dict[str, Any],
    task: dict[str, Any],
    records: list[dict[str, Any]],
    allowed_paths: list[str],
    file_binding_paths: list[str] | None = None,
    path_aliases: dict[str, str] | None = None,
) -> str:
    bindable = list(file_binding_paths or [])
    # 只预览相邻文本以消解省略；完整原始 session 仍可由只读工具逐条访问。
    raw = source.get("raw_session") or {}
    messages = raw.get("messages", []) if isinstance(raw, dict) else []
    context = []
    seen = set()
    for record in records:
        index = record.get("message_index")
        if not isinstance(index, int):
            continue
        for neighbor in (index - 1, index + 1):
            if neighbor in seen or not 0 <= neighbor < len(messages):
                continue
            message = messages[neighbor]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            text = _message_text(message)
            if text:
                context.append({"message_index": neighbor, "role": "assistant", "text": text[:2400], "truncated": len(text) > 2400})
                seen.add(neighbor)
    return "\n".join([
        "Recover a sandbox-solvable task q from the tagged user request.",
        "The original user query is the anchor. task_instruction and core_objective must preserve its main goal and intent type.",
        "Use observed files to ground the same task, but never replace an implementation, repair, or review request with a plan or report unless the user explicitly asked for one.",
        "If the observed environment is incomplete, retain the original acceptance obligations; Completion may enrich the workspace and later sufficiency/verifier gates may return REVIEW.",
        "Research, forum lookup, production publish remain user obligations "
        "when explicitly requested; do not discard them as context.",
        "Do not invent a different product goal or a nearby unrelated coding task. Keep the same task_id.",
        "Use only explicit user intent and evidence refs; never turn assistant/tool actions into requirements.",
        "每条 acceptance_obligations.text 必须保留该用户要求的时间范围、指定资料来源、比较对象、"
        "数据条件和输出证据，不能只概括最终动作。observable 也必须涵盖这些限定；"
        "例如用户要求依据特定年份的报告，不能降为泛化建议。暂时无法核实的限定仍是义务，"
        "不得因工具可用或另一子问题已能回答而删除。原文、任务说明与验收义务必须表达同一要求。",
        "TASK_USER_MESSAGES 保留原角色为 user 的完整文本。harness 可能把协议、提醒、历史摘要与"
        "实际请求包装在同一条消息；须结合上下文区分，不把模板当用户目标，也不能丢掉其后的请求。",
        "Do not merge another tagged task. A clarification/correction belongs here only when its message is in this task tag.",
        "意图提取阶段不要联网搜索或发明工作区路径；只绑定 ALLOWED_OBSERVED_PATHS。"
        "这是当前解析角色的运行约束，不能复制成原任务的 mandatory_constraints/prohibitions。"
        "这些字段只能保留原用户或原系统对该任务实际声明的约束；只读要求不自动等于禁止网络或运行测试。",
        "路径按证据角色绑定：引用用户消息中实际要求读取/修改的路径是初始输入；明确新增/生成的路径是执行输出，不要求 task-start 已存在。格式示例、分类词、工具正文中的字符串不是环境依赖。每条义务只使用其 evidence_ref_ids 引用的用户要求，不能把其他消息的平台说明转成依赖。",
        "PATH_ALIASES 是由原始路径和 Replay 工作目录确定的坐标转换；task_instruction、environment_bindings 和 response_contract 中的路径统一使用右侧工作区路径。不能按 basename 猜测路径。",
        "initial_required_paths are task-start inputs; they may name an explicitly referenced but currently missing input and must remain a blocker. output_paths are only explicit new/generated final files. required_paths is their union. Listing-only names are not bindings. When FILE_BINDING_PATHS is empty, do not invent a project.",
        "Classify every acceptance obligation exactly once in environment_bindings. Do not omit an obligation or infer a missing binding from shared context; missing bindings are a REVIEW error.",
        "FILE 表示该义务的完成状态可以从沙盒文件或本地程序行为中完整验证。observable 只描述用户要求的最终行为，不规定实现步骤、修改测试文件或运行某个测试命令，除非原用户明确提出这些要求。不能仅检查初始文件仍然存在。",
        "仅将与用户目标直接相关的对象绑定为初始路径，周边依赖和已有测试不自动成为交付义务。FILE 的初始或输出路径至少有一项非空；路径不完整不代表可以删掉用户要求。has_examples 根据用户是否提供具体示例填写。",
        '必须返回 response_contract 字段；原用户要求包含下列已支持响应结构时，声明完整检查，否则返回 null 并保留原义务未验证。契约为 response_contract={"schema_version":"traceforge.response-contract.v1","checks":[...]}。仅为纯输出结构或摘要一致性义务声明检查：{"kind":"acceptance_report","obligation_id":"...","criterion_ids":["..."],"required_fields":{"字段名":"类型"}} 或 {"kind":"basic_summary","obligation_id":"...","verdicts":["..."],"finding_levels":["..."],"report_path":"...","match_report":true}。所有字段、义务 ID、枚举和路径必须来自该义务引用的原用户要求；不得把事实正确性或行为已完成声明成格式检查。不认识的响应要求保留未验证。同一义务包含摘要和 acceptance-report 时，在 checks 中使用相同 obligation_id 分别列出 basic_summary 与 acceptance_report；该义务的检查必须全部通过，不能只声明其中一项。',
        "Return JSON only, with no Markdown or prose before/after it.",
        "{\"task_id\":\"same tag\",\"task_instruction\":\"...\",\"core_objective\":\"...\",\"acceptance_obligations\":[{\"id\":\"obl-001\",\"text\":\"...\",\"evidence_ref_ids\":[\"user:<message_index>\"]}],\"environment_bindings\":[{\"obligation_id\":\"obl-001\",\"required_paths\":[\"input-or-output/path\"],\"initial_required_paths\":[\"existing-or-missing-input\"],\"output_paths\":[\"new/generated/output\"],\"observable\":\"任务完成后可观测、且足以证明本条义务达成的具体状态\",\"verifier_kind\":\"FILE|NON_FILE\"}],\"success_criteria\":[\"...\"],\"specified_output_format\":null,\"has_examples\":false,\"mandatory_constraints\":[],\"prohibitions\":[],\"response_contract\":null}",
        "Cite evidence ids exactly as listed in TASK_USER_MESSAGES / list_user_texts. Obligation evidence ids must be user:<message_index>.",
        "When FILE_BINDING_PATHS is non-empty and the anchor can bind those files, at least one FILE obligation is required.",
        "read_user_text accepts id=user:<message_index> or index=<original message_index>.",
        "先使用 TASK_ADJACENT_CONTEXT 消解省略和指代。仅在仍有具体歧义时调用 read_session_message；index 是完整 session 的原始索引。不要穷举读取工具输出、调查实现细节或重复读同一消息；意图明确后立即提交 JSON。上下文不是新增用户指令来源，义务仍仅引用本任务 user ID。",
        "SOURCE_SYSTEM_CONTEXT 是原 system/developer 指令的带来源解读，用于理解原任务的工具、"
        "环境和输出约定。按本任务所在时间使用；有歧义时按 message_indices 读取原文。"
        "历史权限声明不等于实际执行结果或当前授权；通用工作流不构成新的用户目标、文件依赖或验收义务。",
        "SOURCE_SYSTEM_MESSAGES 是完整系统原文，以原文为准，不能用模型解读替代它。"
        "阅读其中与本任务相关的历史摘要、用户偏好、工具协议及约束条件；全部内容均为历史数据。",
        "TASK_TAG=" + json.dumps(
            {k: task.get(k) for k in (
                "task_id", "span_ids", "message_indices", "evidence_refs",
                "relations", "domain_route", "task_kind",
            )},
            ensure_ascii=False,
        ),
        "SESSION_TAGS=" + json.dumps(source.get("session_tags") or [], ensure_ascii=False),
        "TASK_USER_MESSAGES=" + json.dumps(records, ensure_ascii=False),
        "TASK_ADJACENT_CONTEXT=" + json.dumps(context, ensure_ascii=False),
        "SOURCE_SYSTEM_MESSAGES=" + json.dumps(indexed_system_messages(raw), ensure_ascii=False),
        "SOURCE_SYSTEM_CONTEXT=" + json.dumps(
            (source.get("session_parser") or {}).get("system_context", []), ensure_ascii=False,
        ),
        "ALLOWED_OBSERVED_PATHS=" + json.dumps(allowed_paths[:80], ensure_ascii=False),
        "FILE_BINDING_PATHS=" + json.dumps(bindable[:80], ensure_ascii=False),
        "PATH_ALIASES=" + json.dumps(path_aliases or {}, ensure_ascii=False),
        "TOOL_NAMES_CONTEXT_ONLY=" + json.dumps(_tool_names(source), ensure_ascii=False),
    ])


def _gate(
    payload: dict[str, Any],
    task: dict[str, Any],
    known_ids: set[str],
    *,
    allowed_paths: list[str],
    user_blob: str,
    user_records: list[dict[str, Any]] | None = None,
    path_aliases: dict[str, str] | None = None,
    file_binding_paths: list[str] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    errors: list[str] = []
    if str(payload.get("task_id") or task.get("task_id")) != str(task.get("task_id")): errors.append("TASK_ID_MISMATCH")
    for key in ("task_instruction", "core_objective"):
        if not isinstance(payload.get(key), str) or not payload[key].strip(): errors.append(key.upper() + "_REQUIRED")
    obligations = payload.get("acceptance_obligations")
    if not isinstance(obligations, list) or not obligations: errors.append("ACCEPTANCE_OBLIGATIONS_REQUIRED"); obligations = []
    seen: set[str] = set(); normalized = []
    for item in obligations:
        if not isinstance(item, dict): errors.append("OBLIGATION_NOT_OBJECT"); continue
        oid, text, refs = item.get("id"), item.get("text") or item.get("obligation"), item.get("evidence_ref_ids")
        if not isinstance(oid, str) or not oid.strip() or oid in seen: errors.append("OBLIGATION_ID_INVALID"); continue
        seen.add(oid)
        if not isinstance(text, str) or not text.strip(): errors.append("OBLIGATION_TEXT_REQUIRED:" + oid); text = ""
        if not isinstance(refs, list) or not refs or any(not isinstance(x, str) or x not in known_ids for x in refs): errors.append("OBLIGATION_EVIDENCE_REQUIRED:" + oid); refs = []
        normalized.append({"id": oid, "text": text, "evidence_ref_ids": refs})
    bindings, binding_errors = normalize_environment_bindings(
        payload,
        normalized,
        allowed_paths,
        user_blob=user_blob,
        user_records=user_records,
        path_aliases=path_aliases,
        file_binding_paths=file_binding_paths,
        # 模型返回该字段时必须覆盖全部义务；旧 fixture 未返回字段时，
        # 保留确定性的兼容推导。
        require_complete="environment_bindings" in payload,
    )
    errors.extend(binding_errors)
    payload["acceptance_obligations"] = attach_bindings_to_obligations(normalized, bindings)
    payload["environment_bindings"] = bindings
    if deepen_requires_file(
        user_blob,
        list(file_binding_paths or []),
        domain_route=str(task.get("domain_route") or ""),
    ) and not _file_obligation_ready(bindings):
        errors.append("FILE_OBLIGATION_REQUIRED")
    if payload.get("success_criteria") is None: payload["success_criteria"] = [x["text"] for x in normalized if x["text"]]
    elif not isinstance(payload.get("success_criteria"), list) or any(not isinstance(x, str) for x in payload["success_criteria"]): errors.append("INVALID_SUCCESS_CRITERIA")
    for key in ("mandatory_constraints", "prohibitions"):
        if payload.get(key) is None: payload[key] = []
        elif not isinstance(payload.get(key), list) or any(not isinstance(x, str) for x in payload[key]): errors.append("INVALID_" + key.upper())
    return ("READY" if not errors else "REVIEW"), errors, payload


def _binding_repair_changed_task(original: dict[str, Any], corrected: dict[str, Any]) -> bool:
    """绑定纠正不允许重写原任务或把已有 FILE 义务降级以绕过校验。"""

    if {k: v for k, v in original.items() if k != "environment_bindings"} != {
        k: v for k, v in corrected.items() if k != "environment_bindings"
    }:
        return True
    original_bindings = original.get("environment_bindings")
    corrected_bindings = corrected.get("environment_bindings")
    if not isinstance(original_bindings, list):
        return False
    corrected_kinds = {
        oid: str(item.get("verifier_kind") or "").strip().upper()
        for item in corrected_bindings or []
        if isinstance(item, dict)
        and isinstance(oid := item.get("obligation_id") or item.get("id"), str)
    } if isinstance(corrected_bindings, list) else {}
    known_ids = {
        item.get("id") for item in original.get("acceptance_obligations") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    return any(
        isinstance(oid := item.get("obligation_id") or item.get("id"), str)
        and oid in known_ids
        and str(item.get("verifier_kind") or "").strip().upper() == FILE
        and corrected_kinds.get(oid) != FILE
        for item in original_bindings if isinstance(item, dict)
    )


def _write_intent_exchange(root: Path, instruction: str, final_text: str | None) -> None:
    """每次真实尝试保存自己的请求和答复，纠正不覆盖首轮证据。"""

    private = root / "private"
    private.mkdir(parents=True, exist_ok=True)
    (private / "model_exchange.json").write_text(json.dumps({
        "schema_version": "traceforge.private-model-exchange.v1",
        "request": {
            "prompt_version": INTENT_PROMPT_VERSION, "prompt": instruction,
            "prompt_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
            "system": INTENT_ROLE.identity,
        },
        "response": {"text": final_text}, "credentials_embedded": False,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_intent_recovery(
    *,
    source: dict[str, Any],
    agent: AgentRuntime,
    output_root: str | Path,
    replay_files_by_task: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    tasks = selected_task_views(source)
    if not tasks: raise IntentRecoveryError("会话分组没有任务")
    root = Path(output_root); root.mkdir(parents=True, exist_ok=True)
    outputs: list[dict[str, Any]] = []
    for task in tasks:
        task = {**task, "domain_route": source.get("domain_route") or task.get("domain_route")}
        task_id = str(task["task_id"]); records = _task_user_records(source, task)
        if not records:
            outputs.append(
                {
                    "task_id": task_id,
                    "status": "REVIEW",
                    "task": {"task_id": task_id, "source_task": task},
                    "errors": ["NO_ACTIONABLE_USER_MESSAGE"],
                }
            )
            continue
        path_aliases = collect_binding_path_aliases(source, records)
        replay_files = (replay_files_by_task or {}).get(task_id)
        allowed_paths = collect_allowed_paths(
            source, records, replay_files=replay_files, path_aliases=path_aliases
        )
        file_binding_paths = collect_file_binding_paths(
            source, records, replay_files=replay_files
        )
        user_blob = " ".join(str(item.get("text") or "") for item in records)
        instruction = _prompt(source, task, records, allowed_paths, file_binding_paths, path_aliases)
        task_root = root / "tasks" / task_id; task_root.mkdir(parents=True, exist_ok=True)
        known_ids = {x["id"] for x in records}
        attempts: list[dict[str, Any]] = []
        repair_payload: dict[str, Any] | None = None
        current_instruction = instruction
        for attempt in range(2):
            attempt_root = task_root if attempt == 0 else task_root / "binding-repair"
            session = AgentSession(
                user_records=list(records),
                user_texts=[x["text"] for x in records],
                tool_names=_tool_names(source),
                session_context=json.dumps(source.get("raw_session"), ensure_ascii=False, separators=(",", ":")),
            )
            ran = agent.run(
                role=INTENT_ROLE, instruction=current_instruction,
                session=session, output_root=attempt_root,
            )
            payload = copy.deepcopy(ran.payload or {})
            raw_payload = copy.deepcopy(payload)
            _write_intent_exchange(attempt_root, current_instruction, ran.final_text)
            sandbox_init = [item for item in ran.errors if str(item).startswith("SANDBOX_INIT")]
            if sandbox_init:
                status, errors = "REVIEW", sandbox_init
            elif ran.completed and payload:
                status, errors, payload = _gate(
                    payload, task, known_ids, allowed_paths=allowed_paths,
                    user_blob=user_blob, user_records=records,
                    path_aliases=path_aliases, file_binding_paths=file_binding_paths,
                )
                errors = list(ran.errors) + errors
                if repair_payload is not None and _binding_repair_changed_task(repair_payload, raw_payload):
                    errors.append("INTENT_BINDING_REPAIR_CHANGED_TASK")
                if errors:
                    status = "REVIEW"
            else:
                errors = list(ran.errors)
                if not ran.completed:
                    errors.append("AGENT_INCOMPLETE")
                status = "REVIEW"
            attempts.append({
                "output_dir": attempt_root.relative_to(task_root).as_posix(),
                "status": status, "errors": list(errors), "turns": len(ran.turns),
                "completed": ran.completed,
            })
            # 仅已完整返回的绑定合同错误反馈一次；基础设施、权限与用户证据错误不重试。
            if (attempt or not ran.completed or not raw_payload or ran.errors or not errors
                    or not all(error.startswith("BINDING_") or error in {
                        "ENVIRONMENT_BINDINGS_NOT_ARRAY", "FILE_OBLIGATION_REQUIRED",
                    } for error in errors)):
                break
            repair_payload = raw_payload
            current_instruction = "\n".join([
                instruction,
                "上一条结果的文件绑定合同未通过校验。只纠正 environment_bindings，其他字段逐项保留原值；"
                "不得删除用户义务、替换目标、伪造路径或把已有 FILE 改为 NON_FILE 来绕过错误。"
                "FILE_BINDING_PATHS 中的路径用于识别任务对象，不代表环境已经完整；"
                "只读上下文工具可用于确认对应关系，环境补全仍交给后续模块。"
                "无法从证据确认绑定时保留缺口。本次只有一次纠正机会，返回完整 JSON。",
                "BINDING_ERRORS=" + json.dumps(errors, ensure_ascii=False),
                "PREVIOUS_RESULT=" + json.dumps(raw_payload, ensure_ascii=False),
            ])
        result_task = {"task_id": task_id, "source_task": task, "task_instruction": payload.get("task_instruction", ""), "core_objective": payload.get("core_objective", ""), "acceptance_obligations": payload.get("acceptance_obligations", []), "environment_bindings": payload.get("environment_bindings", []), "success_criteria": payload.get("success_criteria", []), "specified_output_format": payload.get("specified_output_format"), "has_examples": bool(payload.get("has_examples")), "mandatory_constraints": payload.get("mandatory_constraints", []), "prohibitions": payload.get("prohibitions", []), "evidence_refs": {"message_indices": [x["message_index"] for x in records if isinstance(x.get("message_index"), int)]}}
        if isinstance(payload.get("response_contract"), dict):
            result_task["response_contract"] = payload["response_contract"]
        if path_aliases:
            result_task["environment_path_aliases"] = path_aliases
        result_task["task_instruction"] = render_task_instruction(result_task)
        response_contract = grounded_response_contract(result_task)
        if response_contract is not None:
            result_task["response_contract"] = response_contract
        else:
            # 不能从原文确认的检查不获验收资格；原义务继续保留，不阻断意图恢复。
            result_task.pop("response_contract", None)
        result = {"schema_version": INTENT_SCHEMA, "prompt_version": INTENT_PROMPT_VERSION, "status": status, "task": result_task, "agent": {"role": INTENT_ROLE.name, "backend": ran.backend, "turns": sum(item["turns"] for item in attempts), "completed": ran.completed, "attempts": attempts}, "errors": errors}
        (task_root / "intent.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outputs.append(result)
    ready = [x for x in outputs if x.get("status") == "READY"]
    result = {"schema_version": INTENT_SCHEMA, "prompt_version": INTENT_PROMPT_VERSION, "status": "READY" if len(ready) == len(outputs) else "REVIEW", "tasks": outputs, "errors": [e for x in outputs for e in x.get("errors", [])]}
    # Singular alias eases migration; multi-task consumers must use tasks[].
    if len(outputs) == 1: result["task"] = outputs[0]["task"]
    (root / "intent.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
