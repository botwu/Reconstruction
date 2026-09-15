"""Terminal-Universe C.2 的单 workspace 任务合成。

论文先让生成器提出五个互异、可验证且基于 workspace 证据的任务，再从合法
候选中选一个进入 teacher rollout。该模块只负责模型调用、结构校验和可复现
选择；真正的执行验证由后续 verifier/Harbor 阶段完成。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from typing import Any

from traceforge.reconstruction.model_gateway import ChatModel, ModelRequest

SINGLE_WS_PROMPT_VERSION = "terminal-universe-single-ws-synthesis-c2-v1"
SINGLE_WS_SCHEMA = "traceforge.single-workspace-synthesis.v1"


class SingleWorkspaceSynthesisError(ValueError):
    """单 workspace 候选不满足论文的输出契约。"""


@dataclass(frozen=True, slots=True)
class SingleWorkspaceSynthesisResult:
    schema_version: str
    request_id: str
    candidates: tuple[str, ...]
    selected_index: int | None
    status: str
    rejected: tuple[dict[str, Any], ...]
    prompt_sha256: str
    response_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "candidates": list(self.candidates),
            "selected_index": self.selected_index,
            "status": self.status,
            "rejected": list(self.rejected),
            "prompt_sha256": self.prompt_sha256,
            "response_sha256": self.response_sha256,
        }


def build_single_workspace_prompt(
    workspace_inventory: list[str] | tuple[str, ...],
    workspace_files: dict[str, str] | None = None,
) -> str:
    """构造附录 C.2 的输入；文件正文是可选的只读上下文。"""

    inventory = "\n".join(f"- {path}" for path in sorted(set(workspace_inventory)))
    inspected = ""
    if workspace_files:
        rows = []
        for path in sorted(workspace_files):
            rows.append(f"### {path}\n{workspace_files[path]}")
        inspected = "\n\n## Targeted read-only file contents\n" + "\n\n".join(rows)
    return f"""You are a repository exploration and terminal-task synthesis agent.

Inspect the private project rooted at /app and propose 5 diverse, challenging terminal
tasks for future coding agents. The workspace file listing below is authoritative.

OUTPUT CONTRACT
Output exactly one JSON array containing exactly 5 self-contained task strings.
Output no markdown, comments, explanations, objects, or extra keys.

EXPLORATION AND TASK RULES
- Start from the supplied inventory; inspect only targeted project files.
- Do not modify files, access the network, install packages, or scan outside /app.
- Every task needs a clear observable goal, grounding files or commands, offline
  deterministic validation, and preserved existing behavior.
- Reject documentation-only, trivial, flaky, external-service, or implementation-
  revealing tasks. Do not provide patch-level guidance.
- Internally generate 10 candidates, filter and score them, then return 5 with
  different primary focuses.

## Workspace Files
{inventory}
{inspected}
"""


def _normalize_task(value: Any) -> str:
    if not isinstance(value, str):
        raise SingleWorkspaceSynthesisError("候选任务必须是字符串")
    task = re.sub(r"\s+", " ", value).strip()
    if len(task) < 40:
        raise SingleWorkspaceSynthesisError("候选任务过短，缺少可执行目标和验证语义")
    return task


def synthesize_single_workspace_tasks(
    *,
    workspace_inventory: list[str] | tuple[str, ...],
    workspace_files: dict[str, str] | None,
    model: ChatModel,
    model_name: str = "claude-opus-4-8",
    selection_seed: int | str = 0,
) -> SingleWorkspaceSynthesisResult:
    """生成并可复现地选择一个候选；验证是否通过交给独立 verifier。"""

    if not workspace_inventory:
        raise SingleWorkspaceSynthesisError("workspace inventory 不能为空")
    prompt = build_single_workspace_prompt(workspace_inventory, workspace_files)
    prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
    request_id = hashlib.sha256(
        f"{SINGLE_WS_SCHEMA}:{model_name}:{prompt_sha256}".encode()
    ).hexdigest()
    response = model.complete(
        ModelRequest(
            request_id=request_id,
            model=model_name,
            system="Follow the requested JSON-only task synthesis contract.",
            prompt=prompt,
            response_schema=SINGLE_WS_SCHEMA,
            temperature=0.7,
            max_tokens=12000,
        )
    )
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise SingleWorkspaceSynthesisError("模型没有返回 JSON 数组") from exc
    if not isinstance(payload, list):
        raise SingleWorkspaceSynthesisError("论文 C.2 要求顶层必须是 JSON 数组")
    candidates: list[str] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        try:
            task = _normalize_task(item)
        except SingleWorkspaceSynthesisError as exc:
            rejected.append({"index": index, "reason": str(exc)})
            continue
        key = task.casefold()
        if key in seen:
            rejected.append({"index": index, "reason": "DUPLICATE_TASK"})
            continue
        seen.add(key)
        candidates.append(task)
    if len(candidates) < 5:
        return SingleWorkspaceSynthesisResult(
            SINGLE_WS_SCHEMA,
            request_id,
            tuple(candidates),
            None,
            "REVIEW",
            tuple([*rejected, {"reason": "FEWER_THAN_FIVE_VALID_CANDIDATES"}]),
            prompt_sha256,
            response.content_sha256,
        )
    rng = random.Random(str(selection_seed))
    selected = rng.randrange(len(candidates))
    return SingleWorkspaceSynthesisResult(
        SINGLE_WS_SCHEMA,
        request_id,
        tuple(candidates[:5]),
        selected,
        "READY",
        tuple(rejected),
        prompt_sha256,
        response.content_sha256,
    )


__all__ = [
    "SINGLE_WS_PROMPT_VERSION",
    "SINGLE_WS_SCHEMA",
    "SingleWorkspaceSynthesisError",
    "SingleWorkspaceSynthesisResult",
    "build_single_workspace_prompt",
    "synthesize_single_workspace_tasks",
]
