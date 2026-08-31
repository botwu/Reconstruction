"""完整 run verifier 测试。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceforge.trajectory.json_codec import canonical_json_line
from traceforge.trajectory.source_adapter import RESTORED_LONG_CAPTURE_SCHEMA
from traceforge.trajectory.validation import validate_compiled_run


def _issue_codes(run: Path) -> set[str]:
    return {issue.code for issue in validate_compiled_run(run).issues}


def _rewrite_jsonl_record(path: Path, mutator: Callable[[dict[str, Any]], None]) -> None:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    mutator(records[0])
    path.write_bytes(b"".join(canonical_json_line(record) for record in records))


def _resign_artifact(run: Path, relative_path: str) -> None:
    """测试只重签物理 manifest，使语义损坏能越过摘要层。"""

    path = run / relative_path
    raw = path.read_bytes()
    manifest_path = run / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    [entry] = [item for item in manifest["files"] if item["relative_path"] == relative_path]
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    entry["byte_length"] = len(raw)
    if relative_path.endswith(".jsonl"):
        entry["record_count"] = raw.count(b"\n")
    manifest_path.write_bytes(canonical_json_line(manifest))

    receipt_path = run / "run_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["artifact_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    receipt_path.write_bytes(canonical_json_line(receipt))


def test_full_run_validator_accepts_compiler_output(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    two_boundary_capture["messages"][2]["vendor_metadata"] = {"fixture_flag": "保留"}
    run = compile_dataset([two_boundary_capture], label="validation-ok")
    result = validate_compiled_run(run)

    assert result.ok, result.errors
    assert result.checked_file_count == 10
    assert result.observed_counts["physical_line_count"] == 1
    assert result.observed_counts["request_boundary_count"] == 2
    assert result.observed_counts["event_occurrence_count"] == 10
    assert result.observed_counts["tool_pairing_record_count"] == 3
    assert result.source_manifest["source_schema"] == RESTORED_LONG_CAPTURE_SCHEMA


def test_validator_accepts_reasoning_summary_in_non_assistant_extensions(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[
            {
                "role": "system",
                "content": "虚构系统消息。",
                "reasoning_content": "不得进入 visible fingerprint 的虚构内容",
            },
            {"role": "assistant", "content": "虚构完成。"},
        ],
        terminal_prefix_depths=[2],
    )
    run = compile_dataset([capture], label="validation-extension-reasoning")

    result = validate_compiled_run(run)

    assert result.ok, result.errors


def test_validator_binds_source_records_to_manifest_identity_and_format(
    compile_dataset: Callable[..., Path],
) -> None:
    run = compile_dataset([{}], label="validation-source-identity")
    source_records = "private/source_records.jsonl"
    _rewrite_jsonl_record(
        run / source_records,
        lambda record: record.update(dataset_id="另一虚构数据集"),
    )
    _resign_artifact(run, source_records)

    source_manifest = "source_manifest.json"
    manifest_path = run / source_manifest
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_format"] = "csv"
    manifest_path.write_bytes(canonical_json_line(manifest))
    _resign_artifact(run, source_manifest)

    codes = _issue_codes(run)
    assert "SOURCE_RECORD_DATASET_MISMATCH" in codes
    assert "SOURCE_FORMAT_UNSUPPORTED" in codes


def test_validator_reports_invalid_collection_types_without_crashing(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    run = compile_dataset([capture], label="validation-invalid-collections")
    relative_path = "private/capture_quality.jsonl"

    def corrupt(record: dict[str, Any]) -> None:
        record["tool_pairing_statuses"] = [{}]
        record["tool_schema_status"] = {}

    _rewrite_jsonl_record(run / relative_path, corrupt)
    _resign_artifact(run, relative_path)

    codes = _issue_codes(run)
    assert "QUALITY_PAIRING_STATUSES_INVALID" in codes
    assert "QUALITY_ENUM_INVALID" in codes


def test_validator_detects_manifest_digest_and_size_tampering(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    run = compile_dataset([two_boundary_capture], label="validation-digest")
    with (run / "private/event_occurrences.jsonl").open("ab") as file:
        file.write(b"{}\n")

    codes = _issue_codes(run)
    assert "ARTIFACT_SHA256_MISMATCH" in codes
    assert "ARTIFACT_SIZE_MISMATCH" in codes
    assert "ARTIFACT_RECORD_COUNT_MISMATCH" in codes


def test_validator_detects_graph_edge_break_after_valid_resigning(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    run = compile_dataset([two_boundary_capture], label="validation-edge")
    relative_path = "private/action_batches.jsonl"
    _rewrite_jsonl_record(
        run / relative_path,
        lambda record: record.update(assistant_event_id="missing-assistant"),
    )
    _resign_artifact(run, relative_path)

    codes = _issue_codes(run)
    assert "ACTION_ASSISTANT_NOT_FOUND" in codes
    assert "ACTION_TOOL_CALL_EDGE_MISMATCH" in codes


def test_validator_detects_stable_id_tampering_after_valid_resigning(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    run = compile_dataset([two_boundary_capture], label="validation-stable-id")
    relative_path = "private/event_occurrences.jsonl"
    _rewrite_jsonl_record(
        run / relative_path,
        lambda record: record.update(event_occurrence_id="0" * 64),
    )
    _resign_artifact(run, relative_path)

    assert "STABLE_ID_MISMATCH" in _issue_codes(run)


def test_validator_detects_base64_data_url_even_when_manifest_is_resigned(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    run = compile_dataset([two_boundary_capture], label="validation-privacy")
    relative_path = "private/event_occurrences.jsonl"

    def leak(record: dict[str, Any]) -> None:
        record["payload"]["content"] = "data:image/png;base64,AAAA"

    _rewrite_jsonl_record(run / relative_path, leak)
    _resign_artifact(run, relative_path)

    assert "BASE64_DATA_URL_OBSERVED" in _issue_codes(run)


def test_validator_rejects_public_report_fields_outside_allowlist(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    run = compile_dataset([two_boundary_capture], label="validation-report")
    relative_path = "reports/attrition_report.json"
    report_path = run / relative_path
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["counts"]["raw_user_literal"] = 1
    report_path.write_bytes(canonical_json_line(report))
    _resign_artifact(run, relative_path)

    assert "PUBLIC_REPORT_ALLOWLIST_MISMATCH" in _issue_codes(run)


def test_validator_detects_base64_marker_across_hashing_chunk_boundary(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    run = compile_dataset([two_boundary_capture], label="validation-chunk-boundary")
    event_path = run / "private/event_occurrences.jsonl"
    chunk_size = 8 * 1024 * 1024
    event_path.write_bytes(b"x" * (chunk_size - 4) + b";BaSe64," + b"x\n")

    assert "BASE64_DATA_URL_OBSERVED" in _issue_codes(run)


def test_validator_rejects_unknown_artifact_source_schema(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    run = compile_dataset([two_boundary_capture], label="validation-source-schema")
    manifest_path = run / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_schema"] = "traceforge.future-capture.v1"
    manifest_path.write_bytes(canonical_json_line(manifest))

    receipt_path = run / "run_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["artifact_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    receipt_path.write_bytes(canonical_json_line(receipt))

    codes = _issue_codes(run)
    assert "SOURCE_SCHEMA_UNSUPPORTED" in codes
    assert "MANIFEST_IDENTITY_MISMATCH" in codes
