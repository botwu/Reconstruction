"""Intent Agent：把筛选出的任务标签重建为一个或多个真实 task。"""
from __future__ import annotations

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
    collect_file_binding_paths,
    mentioned_allowed_paths,
    normalize_environment_bindings,
)
from traceforge.screening.task_labels import apply_task_tags, is_selected_reconstruction_task

INTENT_SCHEMA = "traceforge.intent-recovery.v3"
INTENT_PROMPT_VERSION = "intent-recovery-agent-v11-scoped-path-evidence"
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
_FRAMEWORK_HEADS = ("<environment_context>", "# AGENTS.md", "<INSTRUCTIONS>", "Sender (untrusted metadata)", "<system-reminder>")
_CODEX_REQUEST = re.compile(r"##\s*My request(?:\s+for\s+Codex)?:\s*(.+)", re.S | re.I)
_IMAGE_BLOCK = re.compile(r"<image\b[^>]*>.*?</image>", re.S | re.I)
_IMAGE_TAG = re.compile(r"<image\b[^>]*/>", re.I)


class IntentRecoveryError(RuntimeError):
    """Intent Agent 无法安全产出任务。"""


def _substantive_text(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    match = _CODEX_REQUEST.search(stripped)
    if match:
        body = _IMAGE_BLOCK.sub("", match.group(1)); body = _IMAGE_TAG.sub("", body).strip()
        return body or None
    if stripped.lstrip().startswith("# Files mentioned by the user") or any(stripped.startswith(x) for x in _FRAMEWORK_HEADS):
        return None
    if "AUTOCLAW_OUTPUT_PROTOCOL" in stripped[:800]:
        return None
    return stripped


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
        text = _substantive_text(_message_text(msg))
        if text: records.append({"id": f"user:{index}", "message_index": index, "text": text})
    if not records:
        for text in task.get("user_texts") or []:
            if isinstance(text, str) and (clean := _substantive_text(text)):
                records.append({"id": f"user:task:{len(records)}", "message_index": None, "text": clean})
    dedup: dict[str, dict[str, Any]] = {}
    for item in records: dedup.setdefault(item["id"], item)
    return list(dedup.values())


def selected_task_views(source: dict[str, Any]) -> list[dict[str, Any]]:
    """只返回 screening 标出的可重建任务，保留每个 task 的原始证据。"""
    tasks = [x for x in source.get("tasks") or [] if isinstance(x, dict)]
    if source.get("entry_mode") == "RAW_SESSION":
        # RAW_SESSION 的任务由边界 agent 明确标出；不写入 screening
        # 的 reconstruction_eligible/eligibility 字段，避免把 intake 伪装成筛选结论。
        return [task for task in tasks if task.get("intake_selected") is True]
    for task in tasks:
        apply_task_tags(task)
    eligible = [x for x in tasks if is_selected_reconstruction_task(x)]
    if eligible:
        return eligible
    # legacy labels are intentionally review-only; callers get a clear error.
    return []


def _tool_names(source: dict[str, Any]) -> list[str]:
    names = []
    for item in source.get("tool_timeline") or []:
        if isinstance(item, dict) and item.get("name"): names.append(str(item["name"]))
    return list(dict.fromkeys(names))


def user_text_id(index: int) -> str: return f"user:{index}"
def user_text_records(texts: list[str], *, message_indices: list[int] | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for offset, text in enumerate(texts):
        index = message_indices[offset] if message_indices and offset < len(message_indices) else offset
        records.append({"id": user_text_id(index), "message_index": index, "text": text})
    return records


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
        "Do not merge another tagged task. A clarification/correction belongs here only when its message is in this task tag.",
        "Do not web-search or invent workspace paths. Bind only paths listed in ALLOWED_OBSERVED_PATHS.",
        "路径按证据角色绑定：引用用户消息中实际要求读取/修改的路径是初始输入；明确新增/生成的路径是执行输出，不要求 task-start 已存在。格式示例、分类词、工具正文中的字符串不是环境依赖。每条义务只使用其 evidence_ref_ids 引用的用户要求，不能把其他消息的平台说明转成依赖。",
        "initial_required_paths are task-start inputs; they may name an explicitly referenced but currently missing input and must remain a blocker. output_paths are only explicit new/generated final files. required_paths is their union. Listing-only names are not bindings. When FILE_BINDING_PATHS is empty, do not invent a project.",
        "Classify every acceptance obligation exactly once in environment_bindings. Do not omit an obligation or infer a missing binding from shared context; missing bindings are a REVIEW error.",
        "FILE 表示该义务的完成状态可以从沙盒文件或本地程序行为中完整验证。observable 必须描述用户要求的最终状态，不能仅检查初始文件仍然存在。",
        '可选返回 response_contract={"schema_version":"traceforge.response-contract.v1","checks":[...]}。仅为纯输出结构或摘要一致性义务声明检查：{"kind":"acceptance_report","obligation_id":"...","criterion_ids":["..."],"required_fields":{"字段名":"类型"}} 或 {"kind":"basic_summary","obligation_id":"...","verdicts":["..."],"finding_levels":["..."],"report_path":"...","match_report":true}。所有字段、义务 ID、枚举和路径必须来自该义务引用的原用户要求；不得把事实正确性或行为已完成声明成格式检查。不认识的响应要求保留未验证。',
        "Return JSON only, with no Markdown or prose before/after it.",
        "{\"task_id\":\"same tag\",\"task_instruction\":\"...\",\"core_objective\":\"...\",\"acceptance_obligations\":[{\"id\":\"obl-001\",\"text\":\"...\",\"evidence_ref_ids\":[\"user:<message_index>\"]}],\"environment_bindings\":[{\"obligation_id\":\"obl-001\",\"required_paths\":[\"input-or-output/path\"],\"initial_required_paths\":[\"existing-or-missing-input\"],\"output_paths\":[\"new/generated/output\"],\"observable\":\"任务完成后可观测、且足以证明本条义务达成的具体状态\",\"verifier_kind\":\"FILE|NON_FILE\"}],\"success_criteria\":[\"...\"],\"specified_output_format\":null,\"has_examples\":false,\"mandatory_constraints\":[],\"prohibitions\":[]}",
        "Cite evidence ids exactly as listed in TASK_USER_MESSAGES / list_user_texts. Obligation evidence ids must be user:<message_index>.",
        "When FILE_BINDING_PATHS is non-empty and the anchor can bind those files, at least one FILE obligation is required.",
        "read_user_text accepts id=user:<message_index> or index=<original message_index>.",
        "先使用 TASK_ADJACENT_CONTEXT 消解省略和指代。仅在仍有具体歧义时调用 read_session_message；index 是完整 session 的原始索引。不要穷举读取工具输出、调查实现细节或重复读同一消息；意图明确后立即提交 JSON。上下文不是新增用户指令来源，义务仍仅引用本任务 user ID。",
        "TASK_TAG=" + json.dumps(
            {k: task.get(k) for k in (
                "task_id", "outcome", "span_ids", "message_indices", "evidence_refs",
                "relations", "tags", "reconstruction_eligible", "domain_route", "rubric",
            )},
            ensure_ascii=False,
        ),
        "SESSION_TAGS=" + json.dumps(source.get("session_tags") or [], ensure_ascii=False),
        "TASK_USER_MESSAGES=" + json.dumps(records, ensure_ascii=False),
        "TASK_ADJACENT_CONTEXT=" + json.dumps(context, ensure_ascii=False),
        "ALLOWED_OBSERVED_PATHS=" + json.dumps(allowed_paths[:80], ensure_ascii=False),
        "FILE_BINDING_PATHS=" + json.dumps(bindable[:80], ensure_ascii=False),
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


def run_intent_recovery(
    *,
    source: dict[str, Any],
    agent: AgentRuntime,
    output_root: str | Path,
    replay_files_by_task: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    tasks = selected_task_views(source)
    if not tasks: raise IntentRecoveryError("筛选记录没有可重建的真实任务标签")
    root = Path(output_root); root.mkdir(parents=True, exist_ok=True)
    outputs: list[dict[str, Any]] = []
    for task in tasks:
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
        allowed_paths = collect_allowed_paths(source, records)
        replay_files = (replay_files_by_task or {}).get(task_id)
        file_binding_paths = collect_file_binding_paths(
            source, records, replay_files=replay_files
        )
        user_blob = " ".join(str(item.get("text") or "") for item in records)
        instruction = _prompt(source, task, records, allowed_paths, file_binding_paths)
        task_root = root / "tasks" / task_id; task_root.mkdir(parents=True, exist_ok=True)
        session = AgentSession(
            user_records=list(records),
            user_texts=[x["text"] for x in records],
            tool_names=_tool_names(source),
            session_context=json.dumps(source.get("raw_session"), ensure_ascii=False, separators=(",", ":")),
        )
        ran = agent.run(role=INTENT_ROLE, instruction=instruction, session=session, output_root=task_root)
        payload = dict(ran.payload or {}); known_ids = {x["id"] for x in records}
        sandbox_init = [item for item in ran.errors if str(item).startswith("SANDBOX_INIT")]
        if sandbox_init:
            status, errors = "REVIEW", sandbox_init
        elif ran.completed and payload:
            status, errors, payload = _gate(
                payload,
                task,
                known_ids,
                allowed_paths=allowed_paths,
                user_blob=user_blob,
                user_records=records,
                file_binding_paths=file_binding_paths,
            )
            errors = list(ran.errors) + errors
            if errors:
                status = "REVIEW"
        else:
            errors = list(ran.errors)
            if not ran.completed:
                errors.append("AGENT_INCOMPLETE")
            status = "REVIEW"
        result_task = {"task_id": task_id, "source_task": task, "task_instruction": payload.get("task_instruction", ""), "core_objective": payload.get("core_objective", ""), "acceptance_obligations": payload.get("acceptance_obligations", []), "environment_bindings": payload.get("environment_bindings", []), "success_criteria": payload.get("success_criteria", []), "specified_output_format": payload.get("specified_output_format"), "has_examples": bool(payload.get("has_examples")), "mandatory_constraints": payload.get("mandatory_constraints", []), "prohibitions": payload.get("prohibitions", []), "evidence_refs": {"message_indices": [x["message_index"] for x in records if isinstance(x.get("message_index"), int)]}}
        if isinstance(payload.get("response_contract"), dict):
            result_task["response_contract"] = payload["response_contract"]
        result = {"schema_version": INTENT_SCHEMA, "prompt_version": INTENT_PROMPT_VERSION, "status": status, "task": result_task, "agent": {"role": INTENT_ROLE.name, "backend": ran.backend, "turns": len(ran.turns), "completed": ran.completed}, "errors": errors}
        (task_root / "intent.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outputs.append(result)
        private = task_root / "private"; private.mkdir(exist_ok=True)
        (private / "model_exchange.json").write_text(json.dumps({"schema_version": "traceforge.private-model-exchange.v1", "request": {"prompt_version": INTENT_PROMPT_VERSION, "prompt": instruction, "prompt_sha256": hashlib.sha256(instruction.encode()).hexdigest(), "system": INTENT_ROLE.identity}, "response": {"text": ran.final_text}, "credentials_embedded": False}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ready = [x for x in outputs if x.get("status") == "READY"]
    result = {"schema_version": INTENT_SCHEMA, "prompt_version": INTENT_PROMPT_VERSION, "status": "READY" if len(ready) == len(outputs) else "REVIEW", "tasks": outputs, "errors": [e for x in outputs for e in x.get("errors", [])]}
    # Singular alias eases migration; multi-task consumers must use tasks[].
    if len(outputs) == 1: result["task"] = outputs[0]["task"]
    (root / "intent.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
