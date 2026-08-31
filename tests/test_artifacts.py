"""artifact 写入失败的封闭契约测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from traceforge.trajectory.artifacts import (
    ArtifactPublishError,
    ArtifactWorkspace,
    JsonlArtifactWriter,
    write_json_artifact,
)


class _FailingBinaryFile:
    """只暴露单个失败阶段的测试文件。"""

    def __init__(self, failing_operation: str) -> None:
        self.failing_operation = failing_operation
        self.closed = False

    def write(self, _value: bytes) -> int:
        if self.failing_operation == "write":
            raise OSError("虚构写入失败")
        return len(_value)

    def flush(self) -> None:
        if self.failing_operation == "flush":
            raise OSError("虚构 flush 失败")

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        self.closed = True


def _replace_writer_file(
    writer: JsonlArtifactWriter,
    replacement: _FailingBinaryFile,
) -> None:
    writer._file.close()
    writer._file = replacement  # type: ignore[assignment]


def test_jsonl_writer_wraps_file_creation_error(tmp_path: Path) -> None:
    root_file = tmp_path / "不是目录"
    root_file.write_text("保持原样", encoding="utf-8")

    with pytest.raises(ArtifactPublishError, match="无法创建 JSONL artifact"):
        JsonlArtifactWriter(root_file, "events.jsonl")

    assert root_file.read_text(encoding="utf-8") == "保持原样"


@pytest.mark.parametrize(
    ("failing_operation", "expected_message"),
    [
        ("write", "无法写入 JSONL artifact"),
        ("flush", "无法持久化 JSONL artifact"),
    ],
)
def test_jsonl_writer_wraps_write_and_flush_errors(
    failing_operation: str,
    expected_message: str,
    tmp_path: Path,
) -> None:
    writer = JsonlArtifactWriter(tmp_path, "events.jsonl")
    failing_file = _FailingBinaryFile(failing_operation)
    _replace_writer_file(writer, failing_file)

    with pytest.raises(ArtifactPublishError, match=expected_message):
        if failing_operation == "write":
            writer.write({"event": "虚构"})
        else:
            writer.close()

    writer.abort()
    assert failing_file.closed is True


def test_jsonl_writer_wraps_fsync_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = JsonlArtifactWriter(tmp_path, "events.jsonl")
    writer.write({"event": "虚构"})

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("虚构 fsync 失败")

    monkeypatch.setattr("traceforge.trajectory.artifacts.os.fsync", fail_fsync)

    with pytest.raises(ArtifactPublishError, match="无法持久化 JSONL artifact"):
        writer.close()


def test_json_artifact_wraps_rename_error_and_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("虚构 rename 失败")

    monkeypatch.setattr("traceforge.trajectory.artifacts.os.replace", fail_replace)

    with pytest.raises(ArtifactPublishError, match="无法写入或持久化 JSON artifact"):
        write_json_artifact(tmp_path, "report.json", {"result": "虚构"})

    assert list(tmp_path.iterdir()) == []


def test_workspace_wraps_pre_rename_fsync_error_and_keeps_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = ArtifactWorkspace(tmp_path / "artifacts", "fixture-run")

    def fail_directory_fsync(_path: Path) -> None:
        raise OSError("虚构目录 fsync 失败")

    monkeypatch.setattr(
        "traceforge.trajectory.artifacts._fsync_directory",
        fail_directory_fsync,
    )

    with pytest.raises(ArtifactPublishError, match="无法原子发布 artifact run"):
        workspace.publish()

    assert workspace.staging_path.is_dir()
    assert not workspace.final_path.exists()
    workspace.abort()


def test_workspace_keeps_renamed_run_when_parent_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "artifacts"
    workspace = ArtifactWorkspace(output_root, "fixture-run")
    write_json_artifact(workspace.staging_path, "complete.json", {"complete": True})

    def fail_parent_fsync(path: Path) -> None:
        if path == output_root:
            raise OSError("虚构父目录 fsync 失败")
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    monkeypatch.setattr(
        "traceforge.trajectory.artifacts._fsync_directory",
        fail_parent_fsync,
    )

    with pytest.raises(ArtifactPublishError, match="已完成原子改名"):
        workspace.publish()
    workspace.abort()

    assert workspace.final_path.is_dir()
    assert not workspace.staging_path.exists()


def test_workspace_abort_wraps_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = ArtifactWorkspace(tmp_path / "artifacts", "fixture-run")

    def fail_cleanup(_path: Path) -> None:
        raise OSError("虚构清理失败")

    monkeypatch.setattr("traceforge.trajectory.artifacts.shutil.rmtree", fail_cleanup)

    with pytest.raises(ArtifactPublishError, match="无法清理 artifact staging 目录"):
        workspace.abort()


def test_json_artifact_returns_manifest_entry_after_durable_write(tmp_path: Path) -> None:
    entry = write_json_artifact(tmp_path, "report.json", {"result": "虚构"})

    assert entry.relative_path == "report.json"
    assert entry.record_count is None
    assert entry.byte_length == (tmp_path / "report.json").stat().st_size
    assert len(entry.sha256) == 64
