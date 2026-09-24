"""把用户原始验收约定和公开要求保留到执行指令中。"""

from __future__ import annotations

import json
import re
from typing import Any

_CONTRACT_HEADING = re.compile(r"(?m)^##[ \t]+Acceptance Contract[ \t]*$")
_REPORT_BLOCK = re.compile(r"(?ms)^```acceptance-report[ \t]*\n(.*?)^```[ \t]*$")


def render_task_instruction(task: dict[str, Any], instruction: str) -> str:
    """只展开公开任务要求；不从隐藏测试推断规则，也不生成替代 schema。"""
    parts = [instruction]

    def append(text: Any) -> None:
        if isinstance(text, str) and text.strip() and text.strip() not in "\n\n".join(parts):
            parts.append(text.strip())

    source = task.get("source_task") or {}
    for text in source.get("user_texts") or ():
        if isinstance(text, str) and (heading := _CONTRACT_HEADING.search(text)):
            # 保留契约所在的主请求，防止模型摘要漏掉正文中的验收门槛。
            append(text[:heading.start()])
            append(text[heading.start():])
    append(task.get("specified_output_format"))
    for item in task.get("acceptance_obligations") or ():
        if isinstance(item, dict):
            append(item.get("text"))
            append(item.get("observable"))
    for field in ("mandatory_constraints", "prohibitions"):
        for text in task.get(field) or ():
            append(text)

    rendered = "\n\n".join(parts)
    if "acceptance-report" in rendered.lower():
        _acceptance_report_shape(rendered)
    return rendered


def _acceptance_report_shape(text: str) -> dict[str, Any]:
    blocks = _REPORT_BLOCK.findall(text)
    if not blocks:
        raise ValueError("ACCEPTANCE_REPORT_SCHEMA_MISSING: 公开任务要求缺少原始 JSON 格式")
    shapes = []
    for block in blocks:
        try:
            shape = json.loads(block)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "ACCEPTANCE_REPORT_SCHEMA_INVALID: 原始 JSON 格式无法解析"
            ) from exc
        if not isinstance(shape, dict) or not shape:
            raise ValueError("ACCEPTANCE_REPORT_SCHEMA_INVALID: 原始 JSON 格式必须是非空对象")
        shapes.append(shape)
    if any(shape != shapes[0] for shape in shapes[1:]):
        raise ValueError("ACCEPTANCE_REPORT_SCHEMA_CONFLICT: 公开任务中有冲突的 JSON 格式")
    return shapes[0]


def acceptance_report_criterion_ids(task: dict[str, Any]) -> list[str]:
    """只以原始用户契约中的条目 ID 约束回执，不能由模型输出自行定义。"""
    source = task.get("source_task") or {}
    contracts = [
        text[heading.start():]
        for text in source.get("user_texts") or ()
        if isinstance(text, str) and (heading := _CONTRACT_HEADING.search(text))
    ]
    shape = _acceptance_report_shape("\n\n".join(contracts))
    criteria = shape.get("criteriaSatisfied")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("ACCEPTANCE_REPORT_SCHEMA_INVALID_CRITERIA")
    ids = [item.get("id") for item in criteria if isinstance(item, dict)]
    if (
        len(ids) != len(criteria)
        or any(not isinstance(item, str) or not item.strip() for item in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("ACCEPTANCE_REPORT_SCHEMA_INVALID_CRITERIA")
    return ids
