"""确定性 artifact 写入与原子发布。"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, BinaryIO

from traceforge.trajectory.contracts import ArtifactEntryV1
from traceforge.trajectory.json_codec import canonical_json_bytes, canonical_json_line


def artifact_entry_dicts(entries: Iterable[ArtifactEntryV1]) -> tuple[dict[str, Any], ...]:
    """把 writer 返回的 ArtifactEntryV1 按 relative_path 排序后转为 manifest 的 files 项。"""

    return tuple(entry.to_dict() for entry in sorted(entries, key=lambda item: item.relative_path))


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
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file: BinaryIO = self._path.open("wb")
        except OSError as exc:
            raise ArtifactPublishError("无法创建 JSONL artifact") from exc
        self._digest = hashlib.sha256()
        self._byte_length = 0
        self._record_count = 0
        self._closed = False

    def write(self, value: Any) -> None:
        if self._closed:
            raise ArtifactPublishError(f"artifact 已关闭：{self.relative_path}")
        encoded = canonical_json_line(value)
        try:
            written = self._file.write(encoded)
        except OSError as exc:
            raise ArtifactPublishError("无法写入 JSONL artifact") from exc
        if written != len(encoded):
            raise ArtifactPublishError("JSONL artifact 写入不完整")
        self._digest.update(encoded)
        self._byte_length += len(encoded)
        self._record_count += 1

    def close(self) -> ArtifactEntryV1:
        if not self._closed:
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
            except OSError as exc:
                try:
                    self._file.close()
                except OSError:
                    exc.add_note("JSONL artifact 文件句柄清理失败")
                self._closed = True
                raise ArtifactPublishError("无法持久化 JSONL artifact") from exc
            self._closed = True
            try:
                _fsync_directory(self._path.parent)
            except OSError as exc:
                raise ArtifactPublishError("无法持久化 JSONL artifact 目录") from exc
        return ArtifactEntryV1(
            relative_path=self.relative_path,
            sha256=self._digest.hexdigest(),
            byte_length=self._byte_length,
            record_count=self._record_count,
        )

    def abort(self) -> None:
        if not self._closed:
            try:
                self._file.close()
            except OSError as exc:
                raise ArtifactPublishError("无法关闭 JSONL artifact") from exc
            finally:
                self._closed = True


def write_json_artifact(root: Path, relative_path: str, value: Any) -> ArtifactEntryV1:
    """在 staging 目录内以临时文件发布单个 JSON artifact。"""

    path = root / relative_path
    encoded = canonical_json_bytes(value) + b"\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
    except OSError as exc:
        raise ArtifactPublishError("无法创建 JSON artifact 临时文件") from exc
    temporary_path = Path(temporary_name)
    open_descriptor: int | None = descriptor
    try:
        file = os.fdopen(descriptor, "wb")
        open_descriptor = None
        with file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        publish_error = ArtifactPublishError("无法写入或持久化 JSON artifact")
        if open_descriptor is not None:
            try:
                os.close(open_descriptor)
            except OSError:
                publish_error.add_note("JSON artifact 文件描述符清理失败")
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            publish_error.add_note("JSON artifact 临时文件清理失败")
        raise publish_error from exc
    except BaseException as primary_error:
        if open_descriptor is not None:
            try:
                os.close(open_descriptor)
            except OSError:
                primary_error.add_note("JSON artifact 文件描述符清理失败")
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            primary_error.add_note("JSON artifact 临时文件清理失败")
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
        try:
            if self.output_root.exists() and not self.output_root.is_dir():
                raise ArtifactPublishError("artifact 根路径必须是目录")
            self.output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ArtifactPublishError("无法创建 artifact 根目录") from exc
        try:
            if not self.output_root.is_dir():
                raise ArtifactPublishError("artifact 根路径必须是目录")
            if self.final_path.exists():
                raise ArtifactPublishError(f"目标 run 已存在，拒绝覆盖：{self.final_path}")
        except OSError as exc:
            raise ArtifactPublishError("无法检查 artifact 发布位置") from exc
        try:
            self.staging_path = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=self.output_root))
        except OSError as exc:
            raise ArtifactPublishError("无法创建 artifact staging 目录") from exc
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
        try:
            if self.final_path.exists():
                raise ArtifactPublishError(f"目标 run 已存在，拒绝覆盖：{self.final_path}")
            _fsync_directory(self.staging_path)
            os.replace(self.staging_path, self.final_path)
        except OSError as exc:
            raise ArtifactPublishError("无法原子发布 artifact run") from exc
        self._published = True
        try:
            _fsync_directory(self.output_root)
        except OSError as exc:
            raise ArtifactPublishError(
                f"run 已完成原子改名，但父目录持久化失败：{self.final_path}"
            ) from exc
        return self.final_path

    def abort(self) -> None:
        try:
            if not self._published and self.staging_path.exists():
                shutil.rmtree(self.staging_path)
        except OSError as exc:
            raise ArtifactPublishError("无法清理 artifact staging 目录") from exc
