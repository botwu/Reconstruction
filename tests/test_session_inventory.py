"""检查完整覆盖、逐条可追溯和原始输入不可覆盖约束。"""

import hashlib
import json
from pathlib import Path

import pytest

from traceforge.reconstruction.session_inventory import prepare_session_batch


def _source(tmp_path: Path, r04: bytes, r05: bytes) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    entries = []
    for rubric, content in (("R04", r04), ("R05", r05)):
        (root / f"{rubric}.jsonl").write_bytes(content)
        entries.append(
            {
                "code": rubric,
                "records": len(content.splitlines()),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    (root / "distribution.json").write_text(json.dumps({"distribution": entries}))
    return root


def test_freezes_every_physical_session_and_preserves_duplicate_rows(tmp_path: Path) -> None:
    raw = b'{"messages":[{"role":"user","content":"hello"}]}\n'
    source = _source(tmp_path, raw + raw, raw.rstrip(b"\n"))
    frozen, output = tmp_path / "frozen", tmp_path / "output"
    result = prepare_session_batch(source, frozen, output)
    rows = [json.loads(line) for line in (output / "sessions.jsonl").read_text().splitlines()]
    assert result["coverage_complete"] is True
    assert result["total_sessions"] == result["pending_sessions"] == 3
    assert result["duplicate_line_count"] == 1
    assert [row["rubric"] for row in rows] == ["R04", "R04", "R05"]
    assert len({row["session_id"] for row in rows}) == 3
    assert all(row["input_status"] == "PENDING" for row in rows)
    for row in rows:
        with Path(row["input"]).open("rb") as stream:
            stream.seek(row["byte_offset"])
            line = stream.read(row["byte_length"])
        assert hashlib.sha256(line).hexdigest() == row["line_sha256"]
    assert (frozen / "R04.jsonl").read_bytes() == raw + raw
    assert (frozen / "R05.jsonl").read_bytes() == raw.rstrip(b"\n")
    assert (frozen / "R04.jsonl").stat().st_mode & 0o222 == 0


@pytest.mark.parametrize("key,value", [("records", 99), ("bytes", 2), ("sha256", "f" * 64)])
def test_coverage_mismatch_does_not_publish_input(
    tmp_path: Path, key: str, value: int | str
) -> None:
    source = _source(tmp_path, b'{"messages":[]}\n', b'{"messages":[]}\n')
    meta = source / "distribution.json"
    data = json.loads(meta.read_text())
    data["distribution"][0][key] = value
    meta.write_text(json.dumps(data))
    frozen, output = tmp_path / "frozen", tmp_path / "output"
    result = prepare_session_batch(source, frozen, output)
    assert result["status"] == "PARTIAL_SOURCE"
    assert result["coverage_complete"] is False
    assert key in result["sources"][0]["errors"][0]
    assert not (output / "sessions.jsonl").exists()
    assert not (frozen / "R04.jsonl").exists()
    assert not (frozen / "R05.jsonl").exists()
    assert json.loads((output / "source_manifest.json").read_text())["sessions"] is None


def test_bad_json_remains_in_inventory_with_location(tmp_path: Path) -> None:
    source = _source(tmp_path, b'{"messages":[]}\n{broken\n\n', b'[]\n{"x":1}\n')
    output = tmp_path / "output"
    result = prepare_session_batch(source, tmp_path / "frozen", output)
    rows = [json.loads(line) for line in (output / "sessions.jsonl").read_text().splitlines()]
    assert result["coverage_complete"] is True
    assert result["total_sessions"] == 5
    assert result["invalid_sessions"] == 4
    assert rows[1]["input_status"] == "INVALID_INPUT"
    assert "R04.jsonl:2" in rows[1]["errors"][0]
    assert rows[2]["line_number"] == 3
    assert rows[4]["input_status"] == "INVALID_INPUT"


def test_rerun_refuses_to_overwrite_frozen_data_and_manifests(tmp_path: Path) -> None:
    source = _source(tmp_path, b'{"messages":[]}\n', b'{"messages":[]}\n')
    frozen, output = tmp_path / "frozen", tmp_path / "output"
    prepare_session_batch(source, frozen, output)
    original = (output / "source_manifest.json").read_bytes()
    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        prepare_session_batch(source, frozen, output)
    assert (output / "source_manifest.json").read_bytes() == original
    with pytest.raises(FileExistsError, match=r"R04\.jsonl"):
        prepare_session_batch(source, frozen, tmp_path / "another-output")


def test_requires_both_rubric_coverage_declarations(tmp_path: Path) -> None:
    source = _source(tmp_path, b'{"messages":[]}\n', b'{"messages":[]}\n')
    (source / "distribution.json").write_text('{"distribution":[]}')
    with pytest.raises(ValueError, match="同时声明"):
        prepare_session_batch(source, tmp_path / "frozen", tmp_path / "output")
