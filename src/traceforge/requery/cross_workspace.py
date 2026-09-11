"""Terminal-Universe breadth expansion：跨 workspace 任务候选。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspaceProfile:
    workspace_id: str
    files: tuple[str, ...]
    languages: tuple[str, ...]
    tokens: tuple[str, ...]
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CrossWorkspaceCandidate:
    reference_workspace_id: str
    target_workspace_id: str
    reference_mount: str
    task_instruction: str
    dependency_reason: str
    evidence: tuple[str, ...]
    decision: str


def _tokens(text: str) -> set[str]:
    return {x.lower() for x in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text)}


def profile_workspace(workspace_id: str, root: str | Path) -> WorkspaceProfile:
    path = Path(root).resolve()
    if not path.is_dir():
        raise ValueError(f"workspace 不存在：{path}")
    files = sorted(
        p.relative_to(path).as_posix()
        for p in path.rglob("*")
        if p.is_file()
        and not any(x in p.parts for x in (".git", "tests", "solution", "hidden_control"))
    )
    text = "\n".join((path / f).read_text(encoding="utf-8", errors="ignore")[:20000] for f in files)
    languages = tuple(sorted({p.rsplit(".", 1)[-1] for p in files if "." in p}))
    tokens = tuple(sorted(_tokens(text)))
    capability_markers = {
        "api",
        "cli",
        "parser",
        "server",
        "client",
        "database",
        "config",
        "transform",
        "export",
        "import",
    }
    capabilities = tuple(sorted(set(tokens) & capability_markers))
    return WorkspaceProfile(workspace_id, tuple(files), languages, tokens, capabilities)


def retrieve_directional_pairs(
    profiles: list[WorkspaceProfile], *, min_overlap: int = 2
) -> list[tuple[WorkspaceProfile, WorkspaceProfile, tuple[str, ...]]]:
    if min_overlap < 1:
        raise ValueError("min_overlap 必须大于 0")
    pairs = []
    for reference in profiles:
        for target in profiles:
            if reference.workspace_id == target.workspace_id:
                continue
            overlap = tuple(sorted(set(reference.capabilities) - set(target.capabilities)))
            if len(overlap) >= min_overlap:
                pairs.append((reference, target, overlap))
    return pairs


def build_cross_workspace_prompt(
    reference: WorkspaceProfile,
    target: WorkspaceProfile,
    gap: tuple[str, ...],
    mount: str = "/app/reference",
) -> str:
    if not gap or not mount.startswith("/"):
        raise ValueError("依赖缺口和 reference mount 不能为空")
    return (
        f"在可写目标 workspace 中补齐能力缺口：{', '.join(gap)}。"
        f"参考 workspace 只读挂载于 {mount}。"
        "只能描述可观察行为，不泄漏参考实现细节；结果必须能由本地 verifier 检查。\n"
        f"target={target.workspace_id}; reference={reference.workspace_id}; "
        f"languages={','.join(target.languages)}"
    )
