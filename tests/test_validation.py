"""完整 run verifier 测试。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from traceforge.trajectory.json_codec import canonical_json_bytes, canonical_json_line
from traceforge.trajectory.provenance import GitProvenance
from traceforge.trajectory.source_adapter import RESTORED_LONG_CAPTURE_SCHEMA
from traceforge.trajectory.validation import validate_compiled_run


@pytest.fixture
def stable_compile_git_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """将 M1B 编译路径的 collect_git_provenance 固定为「已核验」来源。

    真实 provenance 取决于测试机的 git 状态（脏工作区、非 git 目录、慢盘超时），会让
    validate_compiled_run 报 RUN_RECEIPT_GIT_PROVENANCE_UNVERIFIED，
    掩盖本用例真正要验的「验收合法编译产物」。固定为可核验值以隔离环境差异（镜像
    test_pipeline_determinism.py 既有做法；只 patch trajectory 编译路径，不 import M1C，
    避免 M1B 测试反向耦合到 lineage）。
    """

    monkeypatch.setattr(
        "traceforge.trajectory.pipeline.collect_git_provenance",
        lambda: GitProvenance(True, "a" * 40, "b" * 40, False),
    )


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


def _one_to_one_capture(
    capture_factory: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    return capture_factory(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "fixture-matched-call",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": {"query": "fixture"}},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "lookup",
                "tool_call_id": "fixture-matched-call",
                "content": "虚构结果",
            },
            {"role": "assistant", "content": "虚构完成。"},
        ],
        terminal_prefix_depths=[3],
    )


def test_full_run_validator_accepts_compiler_output(
    stable_compile_git_provenance: None,
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
    stable_compile_git_provenance: None,
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


def test_validator_rejects_synced_terminal_quality_from_forged_text_length(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "非空终态"}],
        terminal_prefix_depths=[1],
    )
    run = compile_dataset([capture], label="validation-forged-text-audit")
    event_path = "private/event_occurrences.jsonl"

    def forge_event(record: dict[str, Any]) -> None:
        record["payload"]["content"]["utf8_byte_length"] = 0
        visible = {"content": record["payload"]["content"]}
        encoded = canonical_json_bytes(visible)
        record["visible_payload_envelope_utf8_byte_length"] = len(encoded)
        record["visible_payload_sha256"] = hashlib.sha256(encoded).hexdigest()

    _rewrite_jsonl_record(run / event_path, forge_event)
    _resign_artifact(run, event_path)

    quality_path = "private/capture_quality.jsonl"

    def forge_quality(record: dict[str, Any]) -> None:
        record["terminal_status"] = "EMPTY_OUTCOME"
        record["reason_codes"] = ["TERMINAL_EMPTY_OUTCOME"]

    _rewrite_jsonl_record(run / quality_path, forge_quality)
    _resign_artifact(run, quality_path)

    report_path = "reports/attrition_report.json"
    report = json.loads((run / report_path).read_text(encoding="utf-8"))
    report["counts"]["terminal_text_outcome_capture_count"] = 0
    report["counts"]["terminal_empty_outcome_capture_count"] = 1
    (run / report_path).write_bytes(canonical_json_line(report))
    _resign_artifact(run, report_path)

    result = validate_compiled_run(run)

    assert not result.ok
    assert "EVENT_PAYLOAD_INVALID" in {issue.code for issue in result.issues}


@pytest.mark.parametrize(
    "terminal_content",
    [
        "data:image/png;base64,U0VDUkVU",
        "前文 data:image/png;base64,U0VDUkVU 后文",
    ],
)
def test_validator_rejects_resigned_empty_data_url_terminal(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    terminal_content: str,
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": terminal_content}],
        terminal_prefix_depths=[1],
    )
    run = compile_dataset([capture], label="validation-forged-data-url-terminal")
    event_path = "private/event_occurrences.jsonl"

    def forge_event(record: dict[str, Any]) -> None:
        content = record["payload"]["content"]
        content["utf8_byte_length"] = 0
        content["sha256"] = "0" * 64
        content["value"]["utf8_byte_length"] = 0
        content["value"]["sha256"] = "0" * 64
        visible = {"content": content}
        encoded = canonical_json_bytes(visible)
        record["visible_payload_envelope_utf8_byte_length"] = len(encoded)
        record["visible_payload_sha256"] = hashlib.sha256(encoded).hexdigest()

    _rewrite_jsonl_record(run / event_path, forge_event)
    _resign_artifact(run, event_path)

    quality_path = "private/capture_quality.jsonl"

    def forge_quality(record: dict[str, Any]) -> None:
        record["terminal_status"] = "EMPTY_OUTCOME"
        record["reason_codes"] = ["TERMINAL_EMPTY_OUTCOME"]

    _rewrite_jsonl_record(run / quality_path, forge_quality)
    _resign_artifact(run, quality_path)

    report_path = "reports/attrition_report.json"
    report = json.loads((run / report_path).read_text(encoding="utf-8"))
    report["counts"]["terminal_text_outcome_capture_count"] = 0
    report["counts"]["terminal_empty_outcome_capture_count"] = 1
    (run / report_path).write_bytes(canonical_json_line(report))
    _resign_artifact(run, report_path)

    result = validate_compiled_run(run)

    assert not result.ok
    assert "EVENT_PAYLOAD_INVALID" in {issue.code for issue in result.issues}


def test_validator_binds_tool_arguments_pointer_to_event_position(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    run = compile_dataset(
        [_one_to_one_capture(capture_factory)],
        label="validation-tool-arguments-pointer",
    )
    event_path = "private/event_occurrences.jsonl"
    records = [
        json.loads(line) for line in (run / event_path).read_text(encoding="utf-8").splitlines()
    ]
    [tool_call] = [record for record in records if record["event_kind"] == "TOOL_CALL"]
    tool_call["payload"]["function"]["arguments"]["source_json_pointer"] = (
        "/messages/99/tool_calls/7/function/arguments"
    )
    encoded = canonical_json_bytes(tool_call["payload"])
    tool_call["visible_payload_envelope_utf8_byte_length"] = len(encoded)
    tool_call["visible_payload_sha256"] = hashlib.sha256(encoded).hexdigest()
    (run / event_path).write_bytes(b"".join(canonical_json_line(record) for record in records))
    _resign_artifact(run, event_path)

    assert "TOOL_ARGUMENT_POINTER_MISMATCH" in _issue_codes(run)


def test_validator_rejects_unsafe_dataset_id_even_when_identity_is_consistent(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    monkeypatch: Any,
) -> None:
    unsafe_dataset_id = "https://user-secret@example.test/private?token=x"
    monkeypatch.setattr(
        "traceforge.trajectory.source.validate_dataset_id",
        lambda _value: None,
    )
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    run = compile_dataset(
        [capture],
        label="validation-unsafe-dataset-id",
        dataset_id=unsafe_dataset_id,
    )

    report_text = (run / "reports/attrition_report.json").read_text(encoding="utf-8")
    result = validate_compiled_run(run)

    assert unsafe_dataset_id in report_text
    assert not result.ok
    assert "DATASET_ID_INVALID" in {issue.code for issue in result.issues}


def test_validator_reports_deep_resigned_catalog_instead_of_raising(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    run = compile_dataset([capture], label="validation-deep-catalog")
    nested: dict[str, Any] = {}
    for _ in range(90):
        nested = {"deep": nested}

    relative_path = "private/tool_catalogs.jsonl"

    def deepen_catalog(record: dict[str, Any]) -> None:
        record["definitions"][0]["definition"]["function"]["parameters"] = nested

    _rewrite_jsonl_record(run / relative_path, deepen_catalog)
    _resign_artifact(run, relative_path)

    assert "CATALOG_VISIBLE_PROJECTION_INVALID" in _issue_codes(run)


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


def test_validator_recomputes_boundary_owned_message_start(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    run = compile_dataset([two_boundary_capture], label="validation-boundary-semantics")
    relative_path = "private/request_boundaries.jsonl"
    _rewrite_jsonl_record(
        run / relative_path,
        lambda record: record.update(owned_message_start_index=999),
    )
    _resign_artifact(run, relative_path)

    assert "BOUNDARY_WINDOW_SEMANTICS_MISMATCH" in _issue_codes(run)


def test_validator_requires_exact_matched_pairing_edges(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    run = compile_dataset(
        [_one_to_one_capture(capture_factory)],
        label="validation-pairing-matched-edges",
    )
    relative_path = "private/tool_pairings.jsonl"
    _rewrite_jsonl_record(
        run / relative_path,
        lambda record: record.update(
            matched_call_event_id=None,
            matched_result_event_id=None,
        ),
    )
    _resign_artifact(run, relative_path)

    codes = _issue_codes(run)
    assert "PAIRING_MATCHED_CALL_SEMANTICS_MISMATCH" in codes
    assert "PAIRING_MATCHED_RESULT_SEMANTICS_MISMATCH" in codes


def test_validator_rejects_forged_matched_edges_for_anomalous_one_to_one_pair(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[
            {
                "role": "tool",
                "name": "lookup",
                "tool_call_id": "fixture-result-before-call",
                "content": "提前出现的虚构结果",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "fixture-result-before-call",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": {}},
                    }
                ],
            },
            {"role": "assistant", "content": "虚构完成。"},
        ],
        terminal_prefix_depths=[3],
    )
    run = compile_dataset([capture], label="validation-anomalous-matched-edges")
    relative_path = "private/tool_pairings.jsonl"

    def forge_matched_edges(record: dict[str, Any]) -> None:
        record["matched_call_event_id"] = record["call_event_ids"][0]
        record["matched_result_event_id"] = record["result_event_ids"][0]

    _rewrite_jsonl_record(run / relative_path, forge_matched_edges)
    _resign_artifact(run, relative_path)

    codes = _issue_codes(run)
    assert "PAIRING_MATCHED_CALL_SEMANTICS_MISMATCH" in codes
    assert "PAIRING_MATCHED_RESULT_SEMANTICS_MISMATCH" in codes


def test_validator_rejects_invalid_quality_enums(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    run = compile_dataset(
        [_one_to_one_capture(capture_factory)],
        label="validation-quality-enums",
    )
    relative_path = "private/capture_quality.jsonl"

    def corrupt(record: dict[str, Any]) -> None:
        record["boundary_status"] = "B0GUS"
        record["privacy_status"] = "B0GUS"
        record["compaction_status"] = "B0GUS"

    _rewrite_jsonl_record(run / relative_path, corrupt)
    _resign_artifact(run, relative_path)

    codes = _issue_codes(run)
    assert "QUALITY_BOUNDARY_STATUS_INVALID" in codes
    assert "QUALITY_PRIVACY_STATUS_INVALID" in codes
    assert "QUALITY_COMPACTION_STATUS_INVALID" in codes


def test_validator_recomputes_pairing_status_despite_synced_self_reports(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    run = compile_dataset(
        [_one_to_one_capture(capture_factory)],
        label="validation-pairing-status-semantics",
    )
    pairing_path = "private/tool_pairings.jsonl"
    _rewrite_jsonl_record(
        run / pairing_path,
        lambda record: record.update(statuses=["RESULT_NOT_OBSERVED"]),
    )
    _resign_artifact(run, pairing_path)

    quality_path = "private/capture_quality.jsonl"

    def rewrite_quality(record: dict[str, Any]) -> None:
        record["tool_pairing_statuses"] = ["RESULT_NOT_OBSERVED"]
        record["missing_result_count"] = 1
        record["reason_codes"] = ["TOOL_PAIRING_RESULT_NOT_OBSERVED"]

    _rewrite_jsonl_record(run / quality_path, rewrite_quality)
    _resign_artifact(run, quality_path)

    report_path = "reports/attrition_report.json"
    report = json.loads((run / report_path).read_text(encoding="utf-8"))
    report["counts"]["matched_one_to_one_pairing_count"] = 0
    report["counts"]["unobserved_call_id_count"] = 1
    report["counts"]["captures_with_unobserved_results"] = 1
    (run / report_path).write_bytes(canonical_json_line(report))
    _resign_artifact(run, report_path)

    assert "PAIRING_STATUS_SEMANTICS_MISMATCH" in _issue_codes(run)


def test_validator_recomputes_all_capture_quality_axes_and_report_counts(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    run = compile_dataset(
        [_one_to_one_capture(capture_factory)],
        label="validation-quality-derived-facts",
    )
    quality_path = "private/capture_quality.jsonl"

    def falsify_quality(record: dict[str, Any]) -> None:
        record.update(
            processing_status="QUARANTINED",
            boundary_status="INVALID",
            tool_pairing_applicable=False,
            tool_pairing_statuses=[],
            tool_schema_status="INVALID",
            terminal_status="EMPTY_OUTCOME",
            privacy_status="NOT_EVALUATED",
            compaction_status="UNLOCALIZED_COMPACTION_EVIDENCE",
            reason_codes=[
                "TOOL_SCHEMA_INVALID",
                "TERMINAL_EMPTY_OUTCOME",
                "UNLOCALIZED_COMPACTION_EVIDENCE",
            ],
            event_count=999,
        )

    _rewrite_jsonl_record(run / quality_path, falsify_quality)
    _resign_artifact(run, quality_path)

    report_path = "reports/attrition_report.json"
    report = json.loads((run / report_path).read_text(encoding="utf-8"))
    counts = report["counts"]
    counts.update(
        processing_complete_count=0,
        processing_quarantined_count=1,
        schema_consistent_capture_count=0,
        schema_invalid_capture_count=1,
        terminal_text_outcome_capture_count=0,
        terminal_empty_outcome_capture_count=1,
        compaction_capture_count=1,
        matched_one_to_one_pairing_count=0,
    )
    (run / report_path).write_bytes(canonical_json_line(report))
    _resign_artifact(run, report_path)

    codes = _issue_codes(run)
    assert {
        "QUALITY_PROCESSING_STATUS_MISMATCH",
        "QUALITY_BOUNDARY_STATUS_MISMATCH",
        "QUALITY_PAIRING_APPLICABILITY_MISMATCH",
        "QUALITY_PAIRING_STATUS_MISMATCH",
        "QUALITY_TOOL_SCHEMA_STATUS_MISMATCH",
        "QUALITY_TERMINAL_STATUS_MISMATCH",
        "QUALITY_PRIVACY_STATUS_MISMATCH",
        "QUALITY_COMPACTION_STATUS_MISMATCH",
        "QUALITY_COUNT_MISMATCH",
        "QUALITY_REASON_CODES_MISMATCH",
        "ATTRITION_COUNT_MISMATCH",
    } <= codes


def test_validator_derives_input_truncation_and_invalid_schema_from_v2_facts(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = _one_to_one_capture(capture_factory)
    capture["domain_meta"]["input_audit"]["input_truncated"] = True
    capture["meta"]["inferred_tool_definitions"] = {"损坏": "形态"}
    run = compile_dataset([capture], label="validation-v2-quality-evidence")

    result = validate_compiled_run(run)

    semantic_codes = {
        issue.code for issue in result.issues if not issue.code.startswith("RUN_RECEIPT_")
    }
    assert not semantic_codes, result.errors
    assert result.observed_counts["source_reports_truncated_capture_count"] == 1
    assert result.observed_counts["schema_invalid_capture_count"] == 1


def test_validator_recomputes_complete_pairing_status_rule_set(
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[
            {
                "role": "tool",
                "name": "lookup",
                "tool_call_id": "fixture-before",
                "content": "先到结果",
            },
            {
                "role": "tool",
                "name": "lookup",
                "tool_call_id": "fixture-orphan",
                "content": "孤立结果",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "fixture-before",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": {}},
                    },
                    {
                        "id": "fixture-duplicate",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "[]"},
                    },
                    {
                        "id": "fixture-duplicate",
                        "type": "function",
                        "function": {"name": "other", "arguments": {}},
                    },
                ],
            },
            {
                "role": "tool",
                "name": "lookup",
                "tool_call_id": "fixture-duplicate",
                "content": "第一份结果",
            },
            {
                "role": "tool",
                "name": "third",
                "tool_call_id": "fixture-duplicate",
                "content": "第二份结果",
            },
            {"role": "assistant", "content": "虚构完成。"},
        ],
        terminal_prefix_depths=[6],
    )
    run = compile_dataset([capture], label="validation-pairing-complete-rules")

    result = validate_compiled_run(run)
    pairing_codes = {
        issue.code
        for issue in result.issues
        if issue.code.startswith("PAIRING_") or issue.code.startswith("QUALITY_PAIRING_")
    }

    assert not pairing_codes, result.errors
    assert result.observed_counts["result_before_call_group_count"] == 1
    assert result.observed_counts["orphan_result_group_count"] == 1
    assert result.observed_counts["duplicate_call_id_group_count"] == 1
    assert result.observed_counts["duplicate_result_group_count"] == 1
    assert result.observed_counts["name_mismatch_group_count"] == 1
    assert result.observed_counts["invalid_call_arguments_group_count"] == 1


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


def test_validator_does_not_treat_plain_base64_fragment_as_data_url(
    compile_dataset: Callable[..., Path],
    two_boundary_capture: dict[str, Any],
) -> None:
    run = compile_dataset([two_boundary_capture], label="validation-plain-base64-fragment")
    relative_path = "private/event_occurrences.jsonl"

    def add_plain_fragment(record: dict[str, Any]) -> None:
        record["payload"]["extensions"] = {"note": "普通文本只提到 ;base64, 并非 Data URL"}

    _rewrite_jsonl_record(run / relative_path, add_plain_fragment)
    _resign_artifact(run, relative_path)

    assert "BASE64_DATA_URL_OBSERVED" not in _issue_codes(run)


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
