"""在可信控制端为 Harbor 下载后的 Artifact 生成内容清单。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .exceptions import ArtifactValidationError


def build_artifact_manifest(trial_dir: Path | str) -> dict[str, Any]:
    """扫描 ``trial/artifacts`` 并原子写入不可由 Agent 自报的 manifest。"""

    trial_root = Path(trial_dir).resolve()
    artifact_root = trial_root / "artifacts"
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise ArtifactValidationError(f"Artifact 目录不存在或不安全：{artifact_root}")

    files: list[dict[str, Any]] = []
    for path in sorted(artifact_root.rglob("*")):
        if path == artifact_root / "manifest.json":
            continue
        info = path.lstat()
        relative = path.relative_to(artifact_root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            raise ArtifactValidationError(f"Artifact 包含符号链接：{relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactValidationError(f"Artifact 包含特殊文件：{relative}")
        if info.st_nlink != 1:
            raise ArtifactValidationError(f"Artifact 包含硬链接：{relative}")
        raw = path.read_bytes()
        if path.lstat().st_ino != info.st_ino or len(raw) != info.st_size:
            raise ArtifactValidationError(f"Artifact 扫描期间发生变化：{relative}")
        files.append(
            {
                "path": relative,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    if not files:
        raise ArtifactValidationError("Artifact 目录为空")

    manifest = {
        "schema_version": "traceforge-harbor-artifacts/v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "files": files,
    }
    target = artifact_root / "manifest.json"
    temporary = artifact_root / ".manifest.json.tmp"
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return manifest


__all__ = ["build_artifact_manifest"]
