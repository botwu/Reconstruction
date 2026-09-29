"""校验显式采集修复与原始片段的对应关系，不替代独立语义判断。"""

from difflib import unified_diff
from itertools import islice
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
    "原始 Replay 和证据不可改。COMPLETE 只表示全量观测，不保证采集没有损坏。若占位符、乱码或截断损坏"
    "阻碍任务初态使用，可在该文件声明 capture_repairs=[{old_text,new_text,reason}]，"
    "依据可信初态证据作必要的局部修复。每个 old_text 必须在最初 Replay 片段中唯一出现，"
    "重复词需要带上相邻行作为唯一定位，不能因短词重复而放弃修复。替换范围不得重叠。"
    "PARTIAL 可以补充缺失上下文，但须完整保留替换后的非空片段；COMPLETE 只允许声明的替换，不能追加或删改其他内容。"
    "匹配时统一 CRLF/LF，原始证据的字节仍保留不变。UNKNOWN 和 ABSENT 不能修复或覆盖。"
    "修复记录和文件来源为 MODEL_COMPLETED，不声称恢复了历史原文；外层 evidence_ref_ids 仍必填。"
    "write_file 返修省略 capture_repairs 时继承上轮已校验声明，显式 [] 取消声明并恢复原片段保护。"
    "独立 Sufficiency 会结合原片段、修改理由和当前候选判断这是采集损坏还是用户要解决的缺陷。"
    "采集修复不能实现目标功能、预写目标产物或消除任务本身的缺陷；证据不足时说明不确定性。"
)


def apply_capture_repairs(
    original: str | None, repairs: Any, *,
    diagnostics: bool = False,
) -> str:
    """按原始坐标一次应用声明；失败时不产生候选，也不修改原文。"""
    if not isinstance(repairs, list):
        raise ValueError("CAPTURE_REPAIRS_INVALID")
    if original is None:
        raise ValueError("CAPTURE_REPAIR_REQUIRES_OBSERVATION")
    original = original.replace("\r\n", "\n")
    if not repairs:
        return original
    spans: list[tuple[int, int, str]] = []
    for item in repairs:
        if not isinstance(item, dict) or set(item) != {"old_text", "new_text", "reason"}:
            raise ValueError("CAPTURE_REPAIRS_INVALID")
        old, new, reason = item["old_text"], item["new_text"], item["reason"]
        if (
            not isinstance(old, str) or not old
            or not isinstance(new, str)
            or not isinstance(reason, str) or not reason.strip()
        ):
            raise ValueError("CAPTURE_REPAIRS_INVALID")
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        start = original.find(old)
        if start < 0 or original.find(old, start + 1) >= 0:
            if diagnostics:
                match = "没有找到" if start < 0 else "出现多次"
                raise ValueError(
                    "CAPTURE_REPAIR_OLD_TEXT_NOT_UNIQUE\n"
                    f"原始片段中{match} old_text={old[:200]!r}。"
                    "请从原始观察复制正文，并包含足够的相邻行作为唯一定位。"
                )
            raise ValueError("CAPTURE_REPAIR_OLD_TEXT_NOT_UNIQUE")
        spans.append((start, start + len(old), new))
    spans.sort()
    chunks: list[str] = []
    cursor = 0
    for start, end, replacement in spans:
        if start < cursor:
            raise ValueError("CAPTURE_REPAIR_OVERLAP")
        chunks.extend((original[cursor:start], replacement))
        cursor = end
    corrected = "".join([*chunks, original[cursor:]])
    if not corrected:
        raise ValueError("CAPTURE_REPAIR_EMPTY_RESULT")
    return corrected


def capture_repair_error(
    original: str | None, content: str, repairs: Any, *, complete: bool = False,
    diagnostics: bool = False,
) -> str | None:
    """校验最终正文与同一个修改声明实现一致，不放宽完整或部分观察的约束。"""
    if not isinstance(repairs, list):
        return "CAPTURE_REPAIRS_INVALID"
    original = original.replace("\r\n", "\n") if original is not None else None
    content = content.replace("\r\n", "\n")
    if not repairs:
        if complete:
            return "PROTECTED_FILE_OVERWRITE"
        return (
            "PARTIAL_OBSERVED_CONTENT_LOST"
            if original is not None and original not in content else None
        )
    try:
        corrected = apply_capture_repairs(original, repairs, diagnostics=diagnostics)
    except ValueError as exc:
        return str(exc)
    matches = corrected == content if complete else corrected in content
    if not matches:
        if diagnostics:
            diff = unified_diff(
                corrected.splitlines(keepends=True), content.splitlines(keepends=True),
                fromfile="已声明修复后的原片段", tofile="提交内容", n=1,
            )
            detail = "".join(islice(diff, 30))[:2800]
            return (
                "CAPTURE_REPAIR_CONTENT_MISMATCH\n"
                "以下为首段差异；原片段中的额外改动需要声明，或还原不必要的修改。"
                "PARTIAL 仍允许补充缺失上下文。\n" + detail
            )
        return "CAPTURE_REPAIR_CONTENT_MISMATCH"
    return None
