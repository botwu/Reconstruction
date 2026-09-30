"""检索交接的来源边界：默认保留返回，历史输入按原文取回。"""

from __future__ import annotations

import copy
from typing import Any

from traceforge.reconstruction.session_source import message_text
from traceforge.trajectory.privacy import omit_private_reasoning

SEARCH_ENVIRONMENT_SCHEMA = "traceforge.search-environment.v3"


def deliver_captures(
    records: list[dict[str, Any]], exclusions: Any, *, event_count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_index = {item["event_index"]: item for item in records}
    excluded: set[int] = set()
    if not isinstance(exclusions, list):
        raise ValueError("excluded_events 必须为数组；没有需隔离的返回时填 []")
    for item in exclusions:
        index = item.get("event_index") if isinstance(item, dict) else None
        reason = item.get("reason") if isinstance(item, dict) else None
        if (type(index) is not int or not 0 <= index < event_count or index in excluded
                or not isinstance(reason, str) or not reason.strip()):
            raise ValueError("排除必须引用唯一且存在的原始事件，并说明其不属于任务初态的原因")
        excluded.add(index)
    delivered = []
    for original in records:
        if original["event_index"] in excluded:
            continue
        record = copy.deepcopy(original)
        if "session_parse" in record:
            parsed = record["session_parse"] or {}
            # 解析意见只供重建者理解；解题者取得原文及经过来源校验的读取坐标。
            record["session_parse"] = {
                key: [{k: copy.deepcopy(v) for k, v in op.items() if k in {
                    "kind", "path", "source_path", "event_id", "content", "content_ref",
                    "line_contents", "line_numbers", "partial", "total_lines",
                }} for op in parsed.get(key, []) if op.get("kind") == "read"]
                for key in ("file_ops", "reference_file_ops")
            }
        delivered.append(record)
    return delivered, {
        "returned_event_indices": list(by_index),
        "pending_event_indices": [i for i in range(event_count) if i not in by_index],
        "delivered_event_indices": [r["event_index"] for r in delivered],
        "excluded_events": copy.deepcopy(exclusions),
    }


def restore_context(
    messages: list[dict[str, Any]], task: dict[str, Any], references: Any,
) -> list[dict[str, Any]]:
    task_indices = (task.get("source_task") or {}).get("message_indices")
    task_users = {i for i, message in enumerate(messages) if message.get("role") == "user"
                  and (task_indices is None or i in task_indices)}
    if not isinstance(references, list):
        raise ValueError("context_references 必须为原始消息引用数组")
    restored, seen = [], set()
    for ref in references:
        index = ref.get("message_index") if isinstance(ref, dict) else None
        user = ref.get("used_by_user_message_index") if isinstance(ref, dict) else None
        if (type(index) is not int or type(user) is not int or user not in task_users
                or not 0 <= index <= user or (index, user) in seen
                or messages[index].get("role") not in {"user", "assistant"}):
            raise ValueError(
                f"历史引用 message_index={index} → used_by_user_message_index={user} 无效。"
                f"来源不能晚于所支持的用户请求；本任务用户索引为 {sorted(task_users)}。"
                "原用户要求可以作为输入，但之后的回答不能倒置为该问题的输入。"
            )
        original = omit_private_reasoning(messages[index])
        content = copy.deepcopy(original.get("content"))
        if "quote" in ref:
            quote = ref["quote"]
            text = content if isinstance(content, str) else message_text(original)
            if not isinstance(quote, str) or not quote.strip() or quote not in text:
                raise ValueError("历史摘录必须逐字来自引用消息；不能改写或补写待求结论")
            content = quote
        if not content:
            raise ValueError("引用的历史消息没有可交付正文")
        restored.append({"message_index": index, "used_by_user_message_index": user,
                         "role": original["role"], "content": content})
        seen.add((index, user))
    return restored


def validate_requirement_coverage(
    task: dict[str, Any], coverage: Any, available_refs: set[str], read_refs: set[str],
) -> list[str]:
    """核对支持材料确实交付且读过；语义是否足够仍由 researcher 和实跑复核。"""
    obligations = {item["id"] for item in task.get("acceptance_obligations", [])}
    if not isinstance(coverage, list):
        return ["requirement_coverage 必须逐项说明原任务所需输入的来源"]
    errors, seen = [], set()
    for item in coverage:
        if not isinstance(item, dict):
            errors.append("requirement_coverage 条目必须为对象")
            continue
        key = item.get("obligation_id")
        refs = item.get("evidence_ref_ids")
        if not isinstance(key, str) or key not in obligations or key in seen:
            errors.append(f"输入覆盖引用了未知或重复的原始要求：{key}")
            continue
        seen.add(key)
        reason = item.get("reason")
        explanations = [reason] if isinstance(reason, str) else reason
        if (not isinstance(explanations, list) or not explanations
                or any(not isinstance(text, str) or not text.strip() for text in explanations)
                or not isinstance(refs, list) or not refs
                or any(not isinstance(ref, str) for ref in refs)):
            errors.append(f"{key} 必须提供非空 reason 文本或文本数组，以及 evidence_ref_ids 来源数组")
            continue
        for ref in refs:
            if ref not in available_refs:
                errors.append(f"{key} 的来源未交付：{ref}")
            elif ref not in read_refs:
                errors.append(f"{key} 的来源尚未实际读取：{ref}")
    if obligations - seen:
        errors.append("尚未说明以下原要求的输入是否充足：" + ", ".join(sorted(obligations - seen)))
    return errors
