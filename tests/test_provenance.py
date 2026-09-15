"""Git provenance 最小契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from traceforge.trajectory.provenance import (
    GIT_COMMAND_TIMEOUT_SECONDS,
    collect_git_provenance,
)


def test_git_command_timeout_tolerates_slow_network_filesystem() -> None:
    """慢速网络盘（AFS）实测 `git status --porcelain` 需 35–41 秒（194 个跟踪文件、
    每次 lstat 约 150–200ms）。超时上限必须留足余量：30 秒会让就地 run 的
    `collect_git_provenance` 因 `git status` 超时而恒返回 available=False，进而被 M1
    完整性校验判为 RUN_RECEIPT_GIT_PROVENANCE_UNVERIFIED，阻断整条重建闭环。
    此处只钉一个「足够大」的下限，不锁死具体值。"""

    assert GIT_COMMAND_TIMEOUT_SECONDS >= 60


@pytest.mark.parametrize(
    ("status", "expected_dirty"),
    [
        ("", False),
        (" M src/example.py\n?? private-name.txt\n", True),
    ],
)
def test_git_provenance_records_only_clean_or_dirty_boolean(
    status: str,
    expected_dirty: bool,
) -> None:
    responses = {
        ("rev-parse", "--is-inside-work-tree"): "true\n",
        ("rev-parse", "--verify", "HEAD"): "a" * 40 + "\n",
        ("rev-parse", "--verify", f"{'a' * 40}^{{tree}}"): "b" * 40 + "\n",
        ("status", "--porcelain=v1", "--untracked-files=normal"): status,
    }
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], _working_directory: Path) -> str:
        calls.append(arguments)
        return responses[arguments]

    result = collect_git_provenance(Path("虚构仓库"), runner=runner).to_dict()

    assert result == {
        "available": True,
        "commit": "a" * 40,
        "tree": "b" * 40,
        "dirty": expected_dirty,
    }
    assert calls.count(("rev-parse", "--verify", "HEAD")) == 2
    assert ("rev-parse", "--verify", f"{'a' * 40}^{{tree}}") in calls
    assert "example.py" not in str(result)
    assert "private-name.txt" not in str(result)


def test_git_provenance_uses_explicit_nulls_when_git_is_unavailable() -> None:
    def unavailable(_arguments: tuple[str, ...], _working_directory: Path) -> str:
        raise FileNotFoundError("虚构 git 不可用")

    result = collect_git_provenance(Path("虚构仓库"), runner=unavailable)

    assert result.to_dict() == {
        "available": False,
        "commit": None,
        "tree": None,
        "dirty": None,
    }


def test_git_provenance_rejects_malformed_object_ids() -> None:
    responses = {
        ("rev-parse", "--is-inside-work-tree"): "true\n",
        ("rev-parse", "--verify", "HEAD"): "not-an-object-id\n",
        ("rev-parse", "--verify", "not-an-object-id^{tree}"): "b" * 40 + "\n",
        ("status", "--porcelain=v1", "--untracked-files=normal"): "",
    }

    def runner(arguments: tuple[str, ...], _working_directory: Path) -> str:
        return responses[arguments]

    assert collect_git_provenance(Path("虚构仓库"), runner=runner).to_dict() == {
        "available": False,
        "commit": None,
        "tree": None,
        "dirty": None,
    }


def test_git_provenance_rejects_snapshot_when_head_changes_concurrently() -> None:
    first_commit = "a" * 40
    second_commit = "c" * 40
    head_values = iter((first_commit + "\n", second_commit + "\n"))
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], _working_directory: Path) -> str:
        calls.append(arguments)
        if arguments == ("rev-parse", "--is-inside-work-tree"):
            return "true\n"
        if arguments == ("rev-parse", "--verify", "HEAD"):
            return next(head_values)
        if arguments == ("rev-parse", "--verify", f"{first_commit}^{{tree}}"):
            return "b" * 40 + "\n"
        if arguments == ("status", "--porcelain=v1", "--untracked-files=normal"):
            return ""
        raise AssertionError(f"非预期 Git 命令：{arguments!r}")

    result = collect_git_provenance(Path("虚构仓库"), runner=runner)

    assert result.to_dict() == {
        "available": False,
        "commit": None,
        "tree": None,
        "dirty": None,
    }
    assert ("rev-parse", "--verify", f"{first_commit}^{{tree}}") in calls
