"""按固定清单恢复完整调试数据；校验失败时不覆盖已有文件。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def check_file(path: Path, expected_hash: str, expected_bytes: int) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"文件不存在或为符号链接：{path}")
    if path.stat().st_size != expected_bytes:
        raise ValueError(f"文件大小不匹配：{path}")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected_hash:
        raise ValueError(f"SHA256 不匹配：{path}")


def restore_dataset(
    root: Path, item: dict[str, Any], archive_dir: Path, *, verify_only: bool = False,
) -> str:
    destination = root / item["path"]
    if destination.exists() or destination.is_symlink():
        check_file(destination, item["sha256"], item["bytes"])
        return f'{item["name"]}：原文件校验通过（{item["physical_lines"]} 条）'
    if verify_only:
        raise ValueError(f"尚未恢复：{destination}")

    archive = archive_dir / item["archive"]
    check_file(archive, item["archive_sha256"], item["archive_bytes"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.", suffix=".partial",
            dir=destination.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            with gzip.open(archive, "rb") as source:
                for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    output.write(chunk)
        check_file(temporary, item["sha256"], item["bytes"])
        # 同目录硬链接使发布保持原子性；其他进程已创建目标时拒绝覆盖。
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return f'{item["name"]}：恢复并校验通过（{item["physical_lines"]} 条）'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "data/snapshots")
    parser.add_argument("--verify-only", action="store_true", help="只校验已恢复的原文件")
    args = parser.parse_args()
    try:
        manifest = json.loads((ROOT / "data/debug-datasets.json").read_text(encoding="utf-8"))
        for item in manifest["datasets"]:
            print(restore_dataset(ROOT, item, args.archive_dir, verify_only=args.verify_only))
    except (OSError, ValueError, EOFError) as exc:
        parser.exit(1, f"数据恢复失败：{exc}\n")


if __name__ == "__main__":
    main()
