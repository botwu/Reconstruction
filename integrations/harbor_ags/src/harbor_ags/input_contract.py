"""TraceForge Harbor Task Bundle 的 Agent 输入组装与审计契约。"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any

RUNTIME_APPENDIX_HEADER = "--- ENVIRONMENT (absolute paths for THIS run) ---"
RENDERED_INPUT_SCHEMA = "traceforge-rendered-agent-input/v1"
TASK_BUNDLE_INPUT_PROFILE = "traceforge-task-bundle-v1"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _absolute_path(value: str, *, label: str) -> str:
    path = PurePosixPath(value)
    if not value or not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} 必须是无 '..' 的绝对 POSIX 路径")
    return path.as_posix()


def render_runtime_appendix(
    *,
    workspace_root: str,
    artifact_root: str = "/logs/artifacts/traceforge",
    toolsets: str = "file,terminal",
) -> str:
    """生成 Task Bundle 所需的确定性运行时说明。"""

    workspace = _absolute_path(workspace_root, label="workspace_root")
    artifacts = _absolute_path(artifact_root, label="artifact_root")
    tools = tuple(item.strip() for item in toolsets.split(",") if item.strip())
    if not tools:
        raise ValueError("toolsets 至少需要一个非空工具集")
    tool_text = ", ".join(tools)
    return "\n".join(
        (
            RUNTIME_APPENDIX_HEADER,
            f"Your workspace root is: {workspace}",
            "The task's public workspace snapshot is already materialized there.",
            f"Resolve relative task paths from {workspace}; keep all workspace edits inside it.",
            f"Persistent task artifacts must be written under: {artifacts}/",
            f"Hermes runtime toolsets enabled for this run: {tool_text}.",
            "Verifier tests, reference solutions, hidden truth, and control files are not visible ",
            "to the Agent. Use tools to inspect the public workspace and re-read required ",
            "deliverables before finishing.",
            "",
            "引用文件时保留输入中完整的仓库相对路径和源文件行号，避免缩写造成歧义。",
            "遇到工具输出分页时继续读取所需内容；若快照本身缺失，明确列出未验证部分。",
            "区分亲自验证的证据与输入报告中的自述，不以自述代替未读取代码的检查。",
            "结束前重新读取交付文件，对照公开任务要求检查章节、引用、结论及最终响应格式。",
        )
    ) + "\n"


def instruction_components(
    rendered_instruction: str,
    *,
    expected_runtime_appendix: str | None = None,
) -> dict[str, Any]:
    """拆分原始 Task instruction 与执行器追加说明，并保存最终真实输入。"""

    if not rendered_instruction:
        raise ValueError("rendered instruction 不能为空")
    task_instruction = rendered_instruction
    runtime_appendix: str | None = None
    if expected_runtime_appendix is not None:
        separator = f"\n\n{expected_runtime_appendix}"
        if not rendered_instruction.endswith(separator):
            raise ValueError("最终 instruction 未包含预期的确定性 runtime appendix")
        task_instruction = rendered_instruction[: -len(separator)]
        runtime_appendix = expected_runtime_appendix
        if not task_instruction:
            raise ValueError("runtime appendix 前缺少 Task instruction")

    def component(content: str | None) -> dict[str, Any] | None:
        if content is None:
            return None
        encoded = content.encode("utf-8")
        return {
            "content": content,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
            "characters": len(content),
        }

    return {
        "schema_version": RENDERED_INPUT_SCHEMA,
        "task_instruction": component(task_instruction),
        "runtime_appendix": component(runtime_appendix),
        "rendered_user_prompt": component(rendered_instruction),
        "composition": (
            "task_instruction + two_newlines + runtime_appendix"
            if runtime_appendix is not None
            else "task_instruction"
        ),
        "hashes": {
            "task_instruction_sha256": _sha256_text(task_instruction),
            "runtime_appendix_sha256": (
                _sha256_text(runtime_appendix) if runtime_appendix is not None else None
            ),
            "rendered_user_prompt_sha256": _sha256_text(rendered_instruction),
        },
    }


__all__ = [
    "RENDERED_INPUT_SCHEMA",
    "RUNTIME_APPENDIX_HEADER",
    "TASK_BUNDLE_INPUT_PROFILE",
    "instruction_components",
    "render_runtime_appendix",
]
