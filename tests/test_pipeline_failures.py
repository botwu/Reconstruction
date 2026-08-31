"""编译流水线隔离坏行与拒绝覆盖测试。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import json_line, read_private

from traceforge.trajectory import compile_trajectory
from traceforge.trajectory.artifacts import (
    ArtifactPublishError,
    ArtifactWorkspace,
    write_json_artifact,
)
from traceforge.trajectory.source_adapter import (
    RESTORED_LONG_CAPTURE_SCHEMA,
    UnsupportedSourceSchemaError,
)


def _compile_raw(tmp_path: Path, raw: bytes, label: str) -> Path:
    source = tmp_path / f"{label}.jsonl"
    source.write_bytes(raw)
    return compile_trajectory(
        input_path=source,
        dataset_id=f"{label}-dataset-v1",
        source_schema=RESTORED_LONG_CAPTURE_SCHEMA,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        output_root=tmp_path / f"{label}-artifacts",
    )


def test_strict_json_failure_is_quarantined_without_fake_capture(
    tmp_path: Path,
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    valid_capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    duplicate_key = "data:image/png;base64,RFVQTElDQVRFX0tFWQ=="
    raw = f'{{"{duplicate_key}":1,"{duplicate_key}":2}}\n'.encode() + json_line(valid_capture)
    run = _compile_raw(tmp_path, raw, "strict-json")

    source_records = read_private(run, "source_records")
    captures = read_private(run, "captures")
    qualities = read_private(run, "capture_quality")
    counts = json.loads((run / "reports" / "attrition_report.json").read_text(encoding="utf-8"))[
        "counts"
    ]

    assert len(source_records) == 2
    assert [record["ingestion_status"] for record in source_records] == [
        "QUARANTINED",
        "PARSED",
    ]
    assert len(captures) == 1
    assert [quality["processing_status"] for quality in qualities] == [
        "QUARANTINED",
        "COMPLETE",
    ]
    assert qualities[0]["capture_occurrence_id"] is None
    assert counts["quarantined_source_record_count"] == 1
    assert counts["normalized_capture_count"] == 1
    assert duplicate_key not in b"".join(
        path.read_bytes() for path in sorted(run.rglob("*")) if path.is_file()
    ).decode("utf-8")
    assert source_records[0]["parse_error"]["key_character_length"] == len(duplicate_key)


def test_unsafe_json_lines_are_quarantined_and_later_capture_still_compiles(
    tmp_path: Path,
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    valid_capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    raw = b"".join(
        [
            b'{"value":1e400}\n',
            b'{"value":' + b"9" * 5_000 + b"}\n",
            b'{"value":"\\ud800"}\n',
            json_line(valid_capture),
        ]
    )
    run = _compile_raw(tmp_path, raw, "unsafe-json-lines")

    source_records = read_private(run, "source_records")
    qualities = read_private(run, "capture_quality")
    assert [record["ingestion_status"] for record in source_records] == [
        "QUARANTINED",
        "QUARANTINED",
        "QUARANTINED",
        "PARSED",
    ]
    assert [record["parse_error"]["code"] for record in source_records[:3]] == [
        "NON_FINITE_NUMBER",
        "INTEGER_TOO_LONG",
        "UNPAIRED_SURROGATE",
    ]
    assert [quality["processing_status"] for quality in qualities] == [
        "QUARANTINED",
        "QUARANTINED",
        "QUARANTINED",
        "COMPLETE",
    ]
    assert len(read_private(run, "captures")) == 1


def test_extreme_json_nesting_has_terminal_quality_and_later_capture_compiles(
    tmp_path: Path,
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    valid_capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    deeply_nested = b"[" * 20_000 + b"0" + b"]" * 20_000 + b"\n"
    run = _compile_raw(
        tmp_path,
        deeply_nested + json_line(valid_capture),
        "extreme-json-nesting",
    )

    source_records = read_private(run, "source_records")
    qualities = read_private(run, "capture_quality")
    assert [record["ingestion_status"] for record in source_records] == [
        "QUARANTINED",
        "PARSED",
    ]
    assert [quality["processing_status"] for quality in qualities] == [
        "QUARANTINED",
        "COMPLETE",
    ]
    assert source_records[0]["parse_error"]["code"] == "JSON_NESTING_TOO_DEEP"
    assert qualities[0]["processing_error"]["code"] == "JSON_NESTING_TOO_DEEP"
    assert len(source_records) == len(qualities) == 2
    assert len(read_private(run, "captures")) == 1


@pytest.mark.parametrize("depth", [86, 180])
def test_deep_derived_value_is_quarantined_and_later_capture_compiles(
    tmp_path: Path,
    capture_factory: Callable[..., dict[str, Any]],
    depth: int,
) -> None:
    deeply_nested: object = "安全值"
    for index in range(depth):
        key = "data:image/png;base64,U0VDUkVUX0tFWQ==" if index == 0 else f"level-{index}"
        deeply_nested = {key: deeply_nested}

    bad_capture = capture_factory(
        messages=[
            {"role": "user", "content": [{"metadata": deeply_nested}]},
            {"role": "assistant", "content": "坏行后仍应继续。"},
        ],
        terminal_prefix_depths=[2],
    )
    valid_capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    raw = json_line(bad_capture) + json_line(valid_capture)
    run = _compile_raw(tmp_path, raw, f"derived-depth-{depth}")

    qualities = read_private(run, "capture_quality")
    assert [quality["processing_status"] for quality in qualities] == [
        "QUARANTINED",
        "COMPLETE",
    ]
    assert qualities[0]["processing_error"]["code"] == ("PRIVACY_TRANSFORM_DEPTH_EXCEEDED")
    assert len(read_private(run, "captures")) == 1
    assert b"U0VDUkVUX0tFWQ==" not in b"".join(
        path.read_bytes() for path in sorted(run.rglob("*")) if path.is_file()
    )


def test_valid_json_with_invalid_capture_contract_has_structural_quarantine(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[2],
    )
    run = compile_dataset([capture], label="invalid-boundary")

    assert read_private(run, "captures") == []
    [source_record] = read_private(run, "source_records")
    [quality] = read_private(run, "capture_quality")
    assert source_record["ingestion_status"] == "PARSED"
    assert quality["processing_status"] == "QUARANTINED"
    assert quality["reason_codes"] == ["REQUEST_BOUNDARY_INVALID"]
    assert quality["processing_error"]["code"] == "REQUEST_BOUNDARY_INVALID"


def test_json_null_is_not_misreported_as_json_parse_failure(tmp_path: Path) -> None:
    run = _compile_raw(tmp_path, b"null\n", "json-null")

    [source_record] = read_private(run, "source_records")
    [quality] = read_private(run, "capture_quality")
    assert source_record["ingestion_status"] == "PARSED"
    assert quality["reason_codes"] == ["TOP_LEVEL_NOT_OBJECT"]
    assert quality["processing_error"]["code"] == "TOP_LEVEL_NOT_OBJECT"


def test_other_representation_is_quarantined_without_fake_capture(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    capture["meta"]["representation"] = "future_format"

    run = compile_dataset([capture], label="unsupported-representation")

    assert read_private(run, "captures") == []
    [quality] = read_private(run, "capture_quality")
    assert quality["processing_status"] == "QUARANTINED"
    assert quality["processing_error"]["code"] == "SOURCE_REPRESENTATION_UNSUPPORTED"


def test_content_addressed_run_refuses_overwrite(
    tmp_path: Path,
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    raw = json_line(capture)
    source = tmp_path / "same.jsonl"
    output = tmp_path / "same-artifacts"
    source.write_bytes(raw)
    arguments = {
        "input_path": source,
        "dataset_id": "same-dataset-v1",
        "source_schema": RESTORED_LONG_CAPTURE_SCHEMA,
        "expected_sha256": hashlib.sha256(raw).hexdigest(),
        "output_root": output,
    }

    compile_trajectory(**arguments)
    with pytest.raises(ArtifactPublishError):
        compile_trajectory(**arguments)


def test_unsupported_source_schema_fails_before_reading_or_staging(tmp_path: Path) -> None:
    output = tmp_path / "unsupported-artifacts"
    with pytest.raises(UnsupportedSourceSchemaError):
        compile_trajectory(
            input_path=tmp_path / "不存在.jsonl",
            dataset_id="unsupported-dataset-v1",
            source_schema="traceforge.future-capture.v1",
            expected_sha256=None,
            output_root=output,
        )

    assert not output.exists()


def test_parent_directory_fsync_failure_keeps_complete_renamed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "publish-failure"
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
    assert json.loads((workspace.final_path / "complete.json").read_bytes()) == {"complete": True}
