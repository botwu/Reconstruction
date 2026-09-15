"""运行回执使用的 Git 来源信息。"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GitCommandRunner = Callable[[tuple[str, ...], Path], str]

# 单条 git 命令的硬上限。慢速网络盘（AFS）上 `git status --porcelain` 实测需 35–41 秒
# （仅 194 个跟踪文件，但每次 lstat 约 150–200ms，且预热无效），30 秒会使就地 run 的
# provenance 因 `git status` 超时而恒为 available=False，被 M1 完整性校验判为
# RUN_RECEIPT_GIT_PROVENANCE_UNVERIFIED 而拒绝整个 run。90 秒对实测值留约 2 倍余量，
# 仍是有界的，只防真正挂起。
GIT_COMMAND_TIMEOUT_SECONDS = 90


@dataclass(frozen=True, slots=True)
class GitProvenance:
    """不包含文件名或补丁内容的最小 Git 来源信息。"""

    available: bool
    commit: str | None
    tree: str | None
    dirty: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "commit": self.commit,
            "tree": self.tree,
            "dirty": self.dirty,
        }


def collect_git_provenance(
    repository_hint: Path | None = None,
    *,
    runner: GitCommandRunner | None = None,
) -> GitProvenance:
    """读取一个自洽的 HEAD 快照；无法完整确认时显式返回 unavailable。"""

    execute = runner or _run_git
    working_directory = repository_hint or Path(__file__).resolve().parent
    try:
        if execute(("rev-parse", "--is-inside-work-tree"), working_directory).strip() != "true":
            return _unavailable_provenance()
        commit = execute(("rev-parse", "--verify", "HEAD"), working_directory).strip()
        if not _is_object_id(commit):
            return _unavailable_provenance()
        tree = execute(
            ("rev-parse", "--verify", f"{commit}^{{tree}}"),
            working_directory,
        ).strip()
        status = execute(
            ("status", "--porcelain=v1", "--untracked-files=normal"),
            working_directory,
        )
        verified_commit = execute(
            ("rev-parse", "--verify", "HEAD"),
            working_directory,
        ).strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return _unavailable_provenance()
    if not _is_object_id(tree) or verified_commit != commit:
        return _unavailable_provenance()
    return GitProvenance(
        available=True,
        commit=commit,
        tree=tree,
        dirty=bool(status.strip()),
    )


def _run_git(arguments: tuple[str, ...], working_directory: Path) -> str:
    completed = subprocess.run(
        ("git", "--no-optional-locks", *arguments),
        cwd=working_directory,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    return completed.stdout


def _is_object_id(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value)


def _unavailable_provenance() -> GitProvenance:
    return GitProvenance(
        available=False,
        commit=None,
        tree=None,
        dirty=None,
    )
