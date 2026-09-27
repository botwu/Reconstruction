"""保留公开任务约束和用户提供的响应格式，不从隐藏答案推断要求。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_REPORT_BLOCK = re.compile(r"(?ms)^```acceptance-report[^\S\n]*\n(.*?)^```[^\S\n]*$")
_CONTRACT = re.compile(r"(?mi)^(?:#{1,6}\s+)?Acceptance Contract\s*$")


# 契约生成与后验检查共享同一组已支持嵌套字段。
RESPONSE_REPORT_ITEM_FIELDS = {
    "criteriaSatisfied": frozenset({"id", "status", "evidence"}),
    "commandsRun": frozenset({"command", "result", "summary"}),
}


def source_user_texts(task: dict[str, Any]) -> list[str]:
    """仅使用所属任务的用户消息；工具输出不能新增公开验收要求。"""
    source = task.get("source_task") or {}
    return [text for text in source.get("user_texts", []) if isinstance(text, str)]


def render_task_instruction(task: dict[str, Any]) -> str:
    """把容易被摘要遗漏的约束与原始格式交给解题者，不新增格式缺失门禁。"""
    instruction = task.get("task_instruction") or task.get("core_objective") or ""
    parts = [instruction] if isinstance(instruction, str) else []

    def append(text: Any) -> None:
        if isinstance(text, str) and text.strip() and text.strip() not in "\n\n".join(parts):
            parts.append(text.strip())

    append(task.get("specified_output_format"))
    for field in ("mandatory_constraints", "prohibitions"):
        for text in task.get(field) or []:
            append(text)
    for text in source_user_texts(task):
        # 验收 JSON 是格式示例，不能由 Intent 的一句“遵循指定 schema”替代。
        blocks = list(_REPORT_BLOCK.finditer(text))
        if not blocks:
            continue
        heading = _CONTRACT.search(text)
        if heading:
            contract = text[heading.start():].strip()
            if not all(block.group(0).strip() in "\n\n".join(parts) for block in blocks):
                append(contract)
        else:
            for block in blocks:
                append(block.group(0))
    return "\n\n".join(parts)


def grounded_response_contract(task: dict[str, Any]) -> dict[str, Any] | None:
    """将模型标注的机械检查绑定到公开示例；无法确认的检查留给后验未覆盖处理。"""
    declared = task.get("response_contract")
    if not isinstance(declared, dict) or declared.get("schema_version") != "traceforge.response-contract.v1":
        return None
    texts = source_user_texts(task)
    source = "\n\n".join(texts)
    prose = _REPORT_BLOCK.sub("", source)
    examples: list[dict[str, Any]] = []
    for text in texts:
        for block in _REPORT_BLOCK.findall(text):
            try:
                example = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(example, dict) and example:
                examples.append(example)
    bindings = task.get("environment_bindings") or []
    non_file_ids = {row.get("obligation_id") for row in bindings
                    if isinstance(row, dict) and row.get("verifier_kind") == "NON_FILE"}
    output_paths = {path for row in bindings if isinstance(row, dict)
                    for path in row.get("output_paths", []) if isinstance(path, str) and not path.endswith("/")}
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for check in declared.get("checks", []) if isinstance(declared.get("checks"), list) else []:
        if not isinstance(check, dict):
            continue
        oid = check.get("obligation_id")
        if not isinstance(oid, str) or oid not in non_file_ids or oid in seen:
            continue
        if check.get("kind") == "acceptance_report" and len(examples) == 1:
            example = examples[0]
            if not _supported_report_shape(example):
                continue
            types = {key: _json_type(value) for key, value in example.items()}
            # 只在原要求明确标为 optional 时取消该字段的必填性。
            for key in list(types):
                if re.search(rf"(?i)`?{re.escape(key)}`?[^\n]{{0,70}}\boptional\b", prose):
                    types.pop(key)
            criteria = example.get("criteriaSatisfied")
            ids = [row.get("id") for row in criteria if isinstance(row, dict)] if isinstance(criteria, list) else []
            if not ids or any(not isinstance(value, str) or not value for value in ids):
                continue
            item_fields = {
                key: {name: _json_type(value) for row in example[key] for name, value in row.items()}
                for key in RESPONSE_REPORT_ITEM_FIELDS if types.get(key) == "array" and example[key]
            }
            checks.append({"kind": "acceptance_report", "obligation_id": oid,
                           "required_fields": types, "criterion_ids": ids,
                           "required_item_fields": item_fields})
        elif check.get("kind") == "basic_summary":
            verdicts, levels = check.get("verdicts"), check.get("finding_levels")
            if not (isinstance(verdicts, list) and verdicts and isinstance(levels, list) and levels):
                continue
            if any(not isinstance(value, str) or not value or value.lower() not in source.lower()
                   for value in [*verdicts, *levels]):
                continue
            levels = _grounded_finding_levels(prose, levels)
            if levels is None:
                continue
            path = check.get("report_path")
            if path not in output_paths or check.get("match_report") is not True:
                continue
            checks.append({"kind": "basic_summary", "obligation_id": oid, "verdicts": verdicts,
                           "finding_levels": levels, "report_path": path, "match_report": True})
        else:
            continue
        seen.add(oid)
    if not checks:
        return None
    return {"schema_version": "traceforge.response-contract.v1", "checks": checks,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest()}


def _grounded_finding_levels(prose: str, declared: list[str]) -> list[str] | None:
    """用模型标注定位原文的完整枚举；多个不同集合不能推断为同一要求。"""
    token = r"[A-Za-z][A-Za-z0-9_-]*"
    separator = r"(?:\s*/\s*|\s*,\s*(?:and\s+)?)"
    groups: dict[tuple[str, ...], list[str]] = {}
    required = {value.casefold() for value in declared}
    for match in re.finditer(rf"(?<![\w-]){token}(?:{separator}{token})+(?![\w-])", prose):
        values = re.split(separator, match.group())
        key = tuple(value.casefold() for value in values)
        if required.issubset(key) and len(set(key)) == len(key):
            groups[key] = values
    return next(iter(groups.values())) if len(groups) == 1 else None


def _supported_report_shape(example: dict[str, Any]) -> bool:
    """仅声明后验检查器能完整校验的嵌套结构，避免把 object 类型当作字段覆盖。"""
    string_arrays = {
        "changedFiles", "testsAddedOrUpdated", "validationOutput", "residualRisks", "reviewFindings",
    }
    for key, value in example.items():
        if isinstance(value, dict):
            return False
        if not isinstance(value, list):
            continue
        if key in RESPONSE_REPORT_ITEM_FIELDS:
            if any(
                not isinstance(row, dict)
                or not set(row).issubset(RESPONSE_REPORT_ITEM_FIELDS[key])
                or any(not isinstance(item, str) for item in row.values())
                for row in value
            ):
                return False
        elif key not in string_arrays or any(not isinstance(item, str) for item in value):
            return False
    return True


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "number"
