"""校验显式采集修复与原始片段的对应关系，不替代独立语义判断。"""

from typing import Any

CAPTURE_REPAIRS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["old_text", "new_text", "reason"],
        "additionalProperties": False,
    },
}

CAPTURE_REPAIR_GUIDANCE = (
    "原始 Replay 和证据不可改。PARTIAL 默认保留原片段；若采集占位符、乱码或截断损坏"
    "阻碍任务初态使用，可在该文件声明 capture_repairs=[{old_text,new_text,reason}]，"
    "依据可信初态证据作必要的局部修复。每个 old_text 必须在最初 Replay 片段中唯一出现，"
    "替换范围不得重叠；应用这些替换后的非空片段必须完整保留在候选正文中。"
    "修复记录和文件来源为 MODEL_COMPLETED，不声称恢复了历史原文；外层 evidence_ref_ids 仍必填。"
    "返修省略 capture_repairs 时继承上轮已校验声明，显式 [] 取消声明并恢复原片段保护。"
    "独立 Sufficiency 会结合原片段、修改理由和当前候选判断这是采集损坏还是用户要解决的缺陷。"
    "采集修复不能实现目标功能、预写目标产物或消除任务本身的缺陷；证据不足时说明不确定性。"
)


def capture_repair_error(original: str | None, content: str, repairs: Any) -> str | None:
    """只允许声明的互不重叠替换；未声明时沿用原片段的连续子串保护。"""
    if not isinstance(repairs, list):
        return "CAPTURE_REPAIRS_INVALID"
    if not repairs:
        return (
            "PARTIAL_OBSERVED_CONTENT_LOST"
            if original is not None and original not in content else None
        )
    if original is None:
        return "CAPTURE_REPAIR_REQUIRES_PARTIAL"
    spans: list[tuple[int, int, str]] = []
    for item in repairs:
        if not isinstance(item, dict) or set(item) != {"old_text", "new_text", "reason"}:
            return "CAPTURE_REPAIRS_INVALID"
        old, new, reason = item["old_text"], item["new_text"], item["reason"]
        if (
            not isinstance(old, str) or not old
            or not isinstance(new, str)
            or not isinstance(reason, str) or not reason.strip()
        ):
            return "CAPTURE_REPAIRS_INVALID"
        start = original.find(old)
        if start < 0 or original.find(old, start + 1) >= 0:
            return "CAPTURE_REPAIR_OLD_TEXT_NOT_UNIQUE"
        spans.append((start, start + len(old), new))
    spans.sort()
    chunks: list[str] = []
    cursor = 0
    for start, end, replacement in spans:
        if start < cursor:
            return "CAPTURE_REPAIR_OVERLAP"
        chunks.extend((original[cursor:start], replacement))
        cursor = end
    corrected = "".join([*chunks, original[cursor:]])
    if not corrected:
        return "CAPTURE_REPAIR_EMPTY_RESULT"
    if corrected not in content:
        return "CAPTURE_REPAIR_CONTENT_MISMATCH"
    return None
