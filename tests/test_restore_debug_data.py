from __future__ import annotations

import gzip
import hashlib
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "restore_debug_data", Path(__file__).resolve().parents[1] / "scripts/restore_debug_data.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
restore_dataset = _MODULE.restore_dataset


def sample(tmp_path: Path) -> tuple[Path, dict]:
    raw = '{"消息": "保留空白"}\r\n{"value": 2}\n'.encode()
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    archive = gzip.compress(raw, mtime=0)
    (archive_dir / "R01.jsonl.gz").write_bytes(archive)
    return archive_dir, {
        "name": "R01", "path": "return_data/four_batch/by-rubric/R01.jsonl",
        "archive": "R01.jsonl.gz", "archive_bytes": len(archive),
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "physical_lines": 2,
    }


def test_restore_preserves_original_bytes_and_repeated_verification(tmp_path: Path) -> None:
    archive_dir, item = sample(tmp_path)
    assert "恢复并校验通过" in restore_dataset(tmp_path, item, archive_dir)
    destination = tmp_path / item["path"]
    assert destination.read_bytes() == gzip.decompress((archive_dir / item["archive"]).read_bytes())
    assert "原文件校验通过" in restore_dataset(
        tmp_path, item, archive_dir, verify_only=True,
    )


def test_does_not_overwrite_different_existing_data(tmp_path: Path) -> None:
    archive_dir, item = sample(tmp_path)
    destination = tmp_path / item["path"]
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"local data")
    with pytest.raises(ValueError, match="不匹配"):
        restore_dataset(tmp_path, item, archive_dir)
    assert destination.read_bytes() == b"local data"


@pytest.mark.parametrize("field", ["archive_sha256", "sha256"])
def test_invalid_archive_or_restored_content_is_not_published(tmp_path: Path, field: str) -> None:
    archive_dir, item = sample(tmp_path)
    item[field] = "0" * 64
    with pytest.raises(ValueError, match="SHA256"):
        restore_dataset(tmp_path, item, archive_dir)
    assert not (tmp_path / item["path"]).exists()
    assert not list(tmp_path.rglob("*.partial"))


def test_verify_only_requires_existing_data(tmp_path: Path) -> None:
    archive_dir, item = sample(tmp_path)
    with pytest.raises(ValueError, match="尚未恢复"):
        restore_dataset(tmp_path, item, archive_dir, verify_only=True)
