"""确定性 artifact 写入与原子发布。"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

from traceforge.trajectory.contracts import ArtifactEntryV1
from traceforge.trajectory.json_codec import canonical_json_bytes, canonical_json_line


class ArtifactPublishError(RuntimeError):
    """artifact 无法安全发布。"""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class JsonlArtifactWriter:
    """逐条写入 canonical JSONL，并同步计算清单信息。"""

    def __init__(self, root: Path, relative_path: str) -> None:
        self.relative_path = relative_path
        self._path = root / relative_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file: BinaryIO = self._path.open("wb")
        self._digest = hashlib.sha256()
        self._byte_length = 0
        self._record_count = 0
        self._closed = False

    def write(self, value: Any) -> None:
        if self._closed:
            raise ArtifactPublishError(f"artifact 已关闭：{self.relative_path}")
        encoded = canonical_json_line(value)
        self._file.write(encoded)
        self._digest.update(encoded)
        self._byte_length += len(encoded)
        self._record_count += 1

    def close(self) -> ArtifactEntryV1:
        if not self._closed:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            self._closed = True
            _fsync_directory(self._path.parent)
        return ArtifactEntryV1(
            relative_path=self.relative_path,
            sha256=self._digest.hexdigest(),
            byte_length=self._byte_length,
            record_count=self._record_count,
        )

    def abort(self) -> None:
        if not self._closed:
            self._file.close()
            self._closed = True


def write_json_artifact(root: Path, relative_path: str, value: Any) -> ArtifactEntryV1:
    """在 staging 目录内以临时文件发布单个 JSON artifact。"""

    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return ArtifactEntryV1(
        relative_path=relative_path,
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_length=len(encoded),
        record_count=None,
    )


class ArtifactWorkspace:
    """在同一文件系统 staging，成功后一次性发布整个 run。"""

    def __init__(self, output_root: Path, run_id: str) -> None:
        self.output_root = output_root
        self.final_path = output_root / run_id
        self.output_root.mkdir(parents=True, exist_ok=True)
        if self.final_path.exists():
            raise ArtifactPublishError(f"目标 run 已存在，拒绝覆盖：{self.final_path}")
        self.staging_path = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=self.output_root))
        self._published = False

    def open_jsonl_writers(
        self, relative_paths: Mapping[str, str]
    ) -> dict[str, JsonlArtifactWriter]:
        writers: dict[str, JsonlArtifactWriter] = {}
        try:
            for name, relative_path in relative_paths.items():
                writers[name] = JsonlArtifactWriter(self.staging_path, relative_path)
        except BaseException as primary_error:
            for writer in writers.values():
                try:
                    writer.abort()
                except BaseException as cleanup_error:
                    primary_error.add_note(f"writer 清理失败：{cleanup_error!r}")
            raise
        return writers

    def publish(self) -> Path:
        if self._published:
            raise ArtifactPublishError("同一个 staging 目录不能重复发布")
        if self.final_path.exists():
            raise ArtifactPublishError(f"目标 run 已存在，拒绝覆盖：{self.final_path}")
        _fsync_directory(self.staging_path)
        os.replace(self.staging_path, self.final_path)
        self._published = True
        try:
            _fsync_directory(self.output_root)
        except OSError as exc:
            raise ArtifactPublishError(
                f"run 已完成原子改名，但父目录持久化失败：{self.final_path}"
            ) from exc
        return self.final_path

    def abort(self) -> None:
        if not self._published and self.staging_path.exists():
            shutil.rmtree(self.staging_path)
