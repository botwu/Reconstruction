"""收集求解后工作区，排除已确认的可重建运行时目录，保留任务材料。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any


def collect_workspace(
    source: Path, destination: Path, *,
    initial_paths: list[str], output_paths: list[str],
) -> dict[str, Any]:
    """只复制常规文件；初态或明确交付路径不能因运行时目录规则而丢失。"""
    for parent in [destination, *destination.parents]:
        if parent.is_symlink():
            raise ValueError(f"收集目的路径不能包含符号链接: {parent}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = destination.parent / "workspace-collection.json"
    receipt: dict[str, Any] = {
        "schema_version": "traceforge.workspace-collection.v1",
        "status": "COLLECTING", "files": [], "directories": [], "excluded": [], "protected": [], "errors": [],
    }
    files: list[tuple[Path, str]] = []

    def fail(message: str) -> None:
        receipt["status"] = "ERROR"
        receipt["errors"].append(message)
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        raise ValueError(message)

    if source.is_symlink() or not source.is_dir():
        fail("工作区根必须是常规目录")
    for current, directories, names in os.walk(source, followlinks=False, onerror=lambda exc: fail(str(exc))):
        folder = Path(current)
        for name in list(directories):
            path = folder / name
            relative = path.relative_to(source).as_posix()
            if path.is_symlink():
                fail(f"工作区包含不支持的符号链接: {relative}")
            marker = path / "pyvenv.cfg"
            reason = (
                "PYTHON_VENV" if marker.is_file() and not marker.is_symlink()
                else "PYTHON_BYTECODE_CACHE" if name == "__pycache__" else None
            )
            if reason is None:
                continue
            protected = (
                any(item == relative or item.startswith(relative + "/") for item in initial_paths)
                or any(item == relative or item.startswith(relative + "/")
                       or relative.startswith(item.rstrip("/") + "/") for item in output_paths)
            )
            record = {"path": relative, "reason": reason}
            if protected:
                receipt["protected"].append(record)
            else:
                receipt["excluded"].append(record)
                directories.remove(name)
        receipt["directories"].extend((folder / name).relative_to(source).as_posix() for name in directories)
        for name in names:
            path = folder / name
            relative = path.relative_to(source).as_posix()
            if not stat.S_ISREG(path.lstat().st_mode):
                fail(f"工作区包含不支持的链接或特殊文件: {relative}")
            files.append((path, relative))
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    for relative in receipt["directories"]:
        (destination / relative).mkdir(parents=True, exist_ok=True)
    for path, relative in sorted(files, key=lambda item: item[1]):
        if path.is_symlink():
            fail(f"收集期间文件变为符号链接: {relative}")
        raw = path.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(stat.S_IMODE(path.stat().st_mode))
        receipt["files"].append({
            "path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        })
    receipt["status"] = "COLLECTED"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt
