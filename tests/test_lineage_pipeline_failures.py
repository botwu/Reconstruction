"""M1C 发布编排失败路径：上游完整性先校验、内容寻址拒绝覆盖、失败不留半份产物。"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from traceforge.lineage import build_lineage
from traceforge.lineage.reader import LineageInputError
from traceforge.trajectory.artifacts import ArtifactPublishError


def _capture(
    capture_factory: Callable[..., dict[str, Any]], request_ids: list[str]
) -> dict[str, Any]:
    """构造一个每请求一 boundary（user→assistant）的最小合法 capture。"""

    messages: list[dict[str, Any]] = []
    for index in range(len(request_ids)):
        messages.append({"role": "user", "content": f"用户回合 {index}"})
        messages.append({"role": "assistant", "content": f"助手回合 {index}"})
    depths = [2 * (index + 1) for index in range(len(request_ids))]
    return capture_factory(
        messages=messages, terminal_prefix_depths=depths, request_ids=request_ids
    )


def _lineage_dirs(output_root: Path) -> list[Path]:
    return (
        [child for child in output_root.iterdir() if child.is_dir()] if output_root.exists() else []
    )


def test_m1b_integrity_failure_blocks_build_before_publish(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """篡改上游私有表（不重签）→ 先校验拦下，绝不产出半份 lineage。"""

    m1b_run = compile_dataset([_capture(capture_factory, ["r1", "r2"])], label="fail-sha")
    tampered = m1b_run / "private" / "captures.jsonl"
    tampered.write_bytes(tampered.read_bytes() + b'{"injected":1}\n')

    output_root = tmp_path / "lineage_out"
    with pytest.raises(LineageInputError):
        build_lineage(m1b_run_dir=m1b_run, output_root=output_root)
    assert _lineage_dirs(output_root) == []


def test_m1b_missing_private_table_blocks_build(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """上游缺私有表 → 完整性校验不通过 → LineageInputError。"""

    m1b_run = compile_dataset([_capture(capture_factory, ["r1"])], label="fail-missing")
    (m1b_run / "private" / "event_occurrences.jsonl").unlink()

    with pytest.raises(LineageInputError):
        build_lineage(m1b_run_dir=m1b_run, output_root=tmp_path / "lineage_out")


def test_m1b_directory_renamed_breaks_content_address(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """目录名≠已发布 run_id → 内容寻址绑定不符 → LineageInputError。"""

    m1b_run = compile_dataset([_capture(capture_factory, ["r1"])], label="fail-rename")
    renamed = m1b_run.parent / "not-the-content-address"
    shutil.move(str(m1b_run), str(renamed))

    with pytest.raises(LineageInputError):
        build_lineage(m1b_run_dir=renamed, output_root=tmp_path / "lineage_out")


def test_missing_m1b_directory_is_rejected(tmp_path: Path) -> None:
    """上游目录不存在 → 直接 fail-closed。"""

    with pytest.raises(LineageInputError):
        build_lineage(m1b_run_dir=tmp_path / "nope", output_root=tmp_path / "lineage_out")


def test_content_address_refuses_overwrite(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """同一 M1B run 二次发布到同一 root → 内容寻址目录已存在 → 拒绝覆盖。"""

    m1b_run = compile_dataset([_capture(capture_factory, ["r1", "r2"])], label="overwrite")
    output_root = tmp_path / "lineage_out"

    first = build_lineage(m1b_run_dir=m1b_run, output_root=output_root)
    assert first.is_dir()

    with pytest.raises(ArtifactPublishError):
        build_lineage(m1b_run_dir=m1b_run, output_root=output_root)
    # 已发布 run 未被破坏，root 下仍只有那一个内容寻址目录。
    assert _lineage_dirs(output_root) == [first]
