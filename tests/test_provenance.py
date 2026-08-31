"""Git provenance 最小契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from traceforge.trajectory.provenance import collect_git_provenance


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
