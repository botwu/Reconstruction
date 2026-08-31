"""M1A、M1B 的流式编排与 artifact 发布。"""

from __future__ import annotations

import platform
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from traceforge import __version__
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    JsonlArtifactWriter,
    write_json_artifact,
)
from traceforge.trajectory.compiler import (
    CaptureCompileError,
    CompiledCapture,
    compile_capture,
)
from traceforge.trajectory.contracts import (
    ARTIFACT_MANIFEST_SCHEMA,
    ATTRITION_REPORT_SCHEMA,
    CAPTURE_QUALITY_SCHEMA,
    COMPILER_CONTRACT_VERSION,
    RUN_RECEIPT_SCHEMA,
    ArtifactManifestV1,
    AttritionReportV2,
    BoundaryStatus,
    CaptureQualityV2,
    CompactionStatus,
    PrivacyStatus,
    ProcessingStatus,
    TerminalStatus,
    ToolSchemaStatus,
)
from traceforge.trajectory.json_codec import stable_id
from traceforge.trajectory.provenance import collect_git_provenance
from traceforge.trajectory.source import iter_verified_records, scan_jsonl_source
from traceforge.trajectory.source_adapter import (
    RESTORED_LONG_CAPTURE_SCHEMA,
    SourceRecordAdaptError,
    UnsupportedSourceSchemaError,
    adapt_source_record,
)

_JSONL_ARTIFACTS = {
    "source_records": "private/source_records.jsonl",
    "captures": "private/captures.jsonl",
    "request_boundaries": "private/request_boundaries.jsonl",
    "event_occurrences": "private/event_occurrences.jsonl",
    "action_batches": "private/action_batches.jsonl",
    "tool_pairings": "private/tool_pairings.jsonl",
    "tool_catalogs": "private/tool_catalogs.jsonl",
    "capture_quality": "private/capture_quality.jsonl",
}

_ZERO_COUNTS = (
    "physical_line_count",
    "parsed_source_record_count",
    "quarantined_source_record_count",
    "normalized_capture_count",
    "request_boundary_count",
    "unique_source_request_count",
    "event_occurrence_count",
    "system_event_count",
    "user_event_count",
    "assistant_message_event_count",
    "tool_call_event_count",
    "tool_result_event_count",
    "pre_first_observed_terminal_event_count",
    "observed_request_window_event_count",
    "action_batch_count",
    "tool_pairing_record_count",
    "matched_one_to_one_pairing_count",
    "unobserved_call_id_count",
    "duplicate_call_id_group_count",
    "duplicate_result_group_count",
    "extra_result_occurrence_count",
    "orphan_result_group_count",
    "name_mismatch_group_count",
    "result_before_call_group_count",
    "invalid_call_arguments_group_count",
    "captures_with_unobserved_results",
    "tool_definition_count",
    "inferred_tool_name_annotation_count",
    "schema_consistent_capture_count",
    "schema_inferred_capture_count",
    "schema_conflict_reported_capture_count",
    "schema_inferred_and_conflict_capture_count",
    "schema_invalid_capture_count",
    "compaction_capture_count",
    "input_truncated_capture_count",
    "input_truncation_unknown_capture_count",
    "terminal_text_outcome_capture_count",
    "terminal_tool_call_pending_capture_count",
    "terminal_empty_outcome_capture_count",
    "terminal_invalid_capture_count",
    "processing_complete_count",
    "processing_partial_count",
    "processing_quarantined_count",
)


class _AttritionAccumulator:
    """累积公共计数；为精确去重保留请求 ID 集合，不保留原始消息或记录。"""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter({key: 0 for key in _ZERO_COUNTS})
        self._source_request_ids: set[str] = set()

    def add_source(self, *, parsed: bool) -> None:
        self.counts["physical_line_count"] += 1
        status = "parsed_source_record_count" if parsed else "quarantined_source_record_count"
        self.counts[status] += 1

    def add_quarantined_quality(self) -> None:
        self.counts["processing_quarantined_count"] += 1

    def add_capture(self, result: CompiledCapture) -> None:
        self.counts["normalized_capture_count"] += 1
        self.counts["request_boundary_count"] += len(result.request_boundaries)
        self._source_request_ids.update(
            boundary.source_request_id for boundary in result.request_boundaries
        )

        self.counts["event_occurrence_count"] += len(result.event_occurrences)
        for event in result.event_occurrences:
            event_key = {
                "SYSTEM": "system_event_count",
                "USER": "user_event_count",
                "ASSISTANT_MESSAGE": "assistant_message_event_count",
                "TOOL_CALL": "tool_call_event_count",
                "TOOL_RESULT": "tool_result_event_count",
            }[event.event_kind]
            self.counts[event_key] += 1
            scope_key = {
                "PRE_FIRST_OBSERVED_TERMINAL": "pre_first_observed_terminal_event_count",
                "OBSERVED_REQUEST_WINDOW": "observed_request_window_event_count",
            }[event.event_scope]
            self.counts[scope_key] += 1

        self.counts["action_batch_count"] += len(result.action_batches)
        self.counts["tool_pairing_record_count"] += len(result.tool_pairings)
        has_unobserved_result = False
        pairing_count_keys = {
            "MATCHED_ONE_TO_ONE": "matched_one_to_one_pairing_count",
            "RESULT_NOT_OBSERVED": "unobserved_call_id_count",
            "DUPLICATE_CALL_ID": "duplicate_call_id_group_count",
            "DUPLICATE_RESULT": "duplicate_result_group_count",
            "ORPHAN_RESULT": "orphan_result_group_count",
            "NAME_MISMATCH": "name_mismatch_group_count",
            "RESULT_BEFORE_CALL": "result_before_call_group_count",
            "INVALID_CALL_ARGUMENTS": "invalid_call_arguments_group_count",
        }
        for pairing in result.tool_pairings:
            for status in pairing.statuses:
                self.counts[pairing_count_keys[status]] += 1
                has_unobserved_result = has_unobserved_result or status == "RESULT_NOT_OBSERVED"
            if "DUPLICATE_RESULT" in pairing.statuses:
                self.counts["extra_result_occurrence_count"] += max(
                    len(pairing.result_event_ids) - 1,
                    0,
                )
        if has_unobserved_result:
            self.counts["captures_with_unobserved_results"] += 1

        self.counts["tool_definition_count"] += len(result.tool_catalog.definitions)
        self.counts["inferred_tool_name_annotation_count"] += len(
            result.tool_catalog.inferred_tool_names
        )
        schema_key = {
            "CONSISTENT": "schema_consistent_capture_count",
            "INFERRED": "schema_inferred_capture_count",
            "CONFLICT_REPORTED": "schema_conflict_reported_capture_count",
            "INFERRED_AND_CONFLICT": "schema_inferred_and_conflict_capture_count",
            "INVALID": "schema_invalid_capture_count",
        }[result.quality.tool_schema_status]
        self.counts[schema_key] += 1
        if result.quality.compaction_status == "UNLOCALIZED_COMPACTION_EVIDENCE":
            self.counts["compaction_capture_count"] += 1
        if result.capture.input_truncation_status == "OBSERVED_TRUNCATED":
            self.counts["input_truncated_capture_count"] += 1
        elif result.capture.input_truncation_status == "UNKNOWN":
            self.counts["input_truncation_unknown_capture_count"] += 1

        terminal_key = {
            "TEXT_OUTCOME": "terminal_text_outcome_capture_count",
            "TOOL_CALL_PENDING": "terminal_tool_call_pending_capture_count",
            "EMPTY_OUTCOME": "terminal_empty_outcome_capture_count",
            "INVALID": "terminal_invalid_capture_count",
        }[result.quality.terminal_status]
        self.counts[terminal_key] += 1
        processing_key = {
            "COMPLETE": "processing_complete_count",
            "PARTIAL": "processing_partial_count",
            "QUARANTINED": "processing_quarantined_count",
        }[result.quality.processing_status]
        self.counts[processing_key] += 1

    def finish(self) -> dict[str, int]:
        self.counts["unique_source_request_count"] = len(self._source_request_ids)
        return dict(sorted(self.counts.items()))


def _quarantined_quality(
    source_record_id: str,
    reason_code: str,
    processing_error: dict[str, Any],
) -> CaptureQualityV2:
    return CaptureQualityV2(
        schema_version=CAPTURE_QUALITY_SCHEMA,
        source_record_id=source_record_id,
        capture_occurrence_id=None,
        processing_status=ProcessingStatus.QUARANTINED,
        boundary_status=BoundaryStatus.INVALID,
        tool_pairing_applicable=False,
        tool_pairing_statuses=(),
        tool_schema_status=ToolSchemaStatus.INVALID,
        terminal_status=TerminalStatus.INVALID,
        privacy_status=PrivacyStatus.NOT_EVALUATED,
        compaction_status=CompactionStatus.NOT_OBSERVED,
        processing_error=processing_error,
        reason_codes=(reason_code,),
        boundary_count=0,
        event_count=0,
        action_batch_count=0,
        tool_call_count=0,
        tool_result_count=0,
        missing_result_count=0,
        duplicate_result_count=0,
    )


def _write_compiled_capture(
    writers: dict[str, JsonlArtifactWriter],
    result: CompiledCapture,
) -> None:
    writers["captures"].write(result.capture.to_dict())
    for boundary in result.request_boundaries:
        writers["request_boundaries"].write(boundary.to_dict())
    for event in result.event_occurrences:
        writers["event_occurrences"].write(event.to_dict())
    for batch in result.action_batches:
        writers["action_batches"].write(batch.to_dict())
    for pairing in result.tool_pairings:
        writers["tool_pairings"].write(pairing.to_dict())
    writers["tool_catalogs"].write(result.tool_catalog.to_dict())
    writers["capture_quality"].write(result.quality.to_dict())


def _close_writers(writers: dict[str, JsonlArtifactWriter]) -> list[Any]:
    return [writers[name].close() for name in sorted(writers)]


def _abort_writers(writers: dict[str, JsonlArtifactWriter]) -> list[BaseException]:
    errors: list[BaseException] = []
    for writer in writers.values():
        try:
            writer.abort()
        except BaseException as exc:
            errors.append(exc)
    return errors


def compile_trajectory(
    *,
    input_path: str | Path,
    dataset_id: str,
    source_schema: str,
    expected_sha256: str | None,
    output_root: str | Path,
) -> Path:
    """把一个冻结 JSONL 来源编译成 M1A、M1B 确定性 artifacts。"""

    if source_schema != RESTORED_LONG_CAPTURE_SCHEMA:
        raise UnsupportedSourceSchemaError(
            "不支持该 source_schema；当前编译器只接受显式 restored-long v1 契约"
        )
    started_at = datetime.now(UTC)
    git_provenance = collect_git_provenance()
    source_path = Path(input_path)
    scan = scan_jsonl_source(
        source_path,
        dataset_id=dataset_id,
        source_schema=source_schema,
        expected_sha256=expected_sha256,
    )
    run_id = stable_id(
        "trajectory-compile-run-v1",
        {
            "compiler_contract_version": COMPILER_CONTRACT_VERSION,
            "dataset_id": scan.manifest.dataset_id,
            "dataset_sha256": scan.manifest.dataset_sha256,
            "source_schema": scan.manifest.source_schema,
        },
    )
    workspace = ArtifactWorkspace(Path(output_root), run_id)
    writers: dict[str, JsonlArtifactWriter] = {}
    entries = []
    accumulator = _AttritionAccumulator()
    try:
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "source_manifest.json",
                scan.manifest.to_dict(),
            )
        )
        writers = workspace.open_jsonl_writers(_JSONL_ARTIFACTS)
        for source_record in iter_verified_records(source_path, scan):
            reference = source_record.reference
            writers["source_records"].write(reference.to_dict())
            parsed = reference.ingestion_status == "PARSED"
            accumulator.add_source(parsed=parsed)
            if not parsed:
                parse_error = reference.parse_error or {}
                reason_code = str(parse_error.get("code", "STRICT_JSON_ERROR"))
                quality = _quarantined_quality(
                    reference.source_record_id,
                    reason_code,
                    dict(parse_error),
                )
                writers["capture_quality"].write(quality.to_dict())
                accumulator.add_quarantined_quality()
                continue
            try:
                envelope = adapt_source_record(source_record.value)
                result = compile_capture(reference, envelope)
            except (SourceRecordAdaptError, CaptureCompileError) as exc:
                quality = _quarantined_quality(
                    reference.source_record_id,
                    exc.reason_code,
                    {"code": exc.reason_code, "message": exc.detail},
                )
                writers["capture_quality"].write(quality.to_dict())
                accumulator.add_quarantined_quality()
                continue
            _write_compiled_capture(writers, result)
            accumulator.add_capture(result)

        entries.extend(_close_writers(writers))
        report = AttritionReportV2(
            schema_version=ATTRITION_REPORT_SCHEMA,
            dataset_id=scan.manifest.dataset_id,
            dataset_sha256=scan.manifest.dataset_sha256,
            counts=accumulator.finish(),
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "reports/attrition_report.json",
                report.to_dict(),
            )
        )
        artifact_manifest = ArtifactManifestV1(
            schema_version=ARTIFACT_MANIFEST_SCHEMA,
            run_id=run_id,
            source_schema=scan.manifest.source_schema,
            dataset_id=scan.manifest.dataset_id,
            dataset_sha256=scan.manifest.dataset_sha256,
            compiler_contract_version=COMPILER_CONTRACT_VERSION,
            files=tuple(
                entry.to_dict() for entry in sorted(entries, key=lambda item: item.relative_path)
            ),
        )
        manifest_entry = write_json_artifact(
            workspace.staging_path,
            "artifact_manifest.json",
            artifact_manifest.to_dict(),
        )
        completion_git_provenance = collect_git_provenance()
        git_provenance_verified_at_completion = (
            git_provenance.available
            and completion_git_provenance.available
            and git_provenance == completion_git_provenance
        )
        completed_at = datetime.now(UTC)
        write_json_artifact(
            workspace.staging_path,
            "run_receipt.json",
            {
                "artifact_manifest_sha256": manifest_entry.sha256,
                "completed_at": completed_at.isoformat(),
                "duration_seconds": (completed_at - started_at).total_seconds(),
                "git_provenance": git_provenance.to_dict(),
                "git_provenance_verified_at_completion": (git_provenance_verified_at_completion),
                "python": platform.python_version(),
                "run_id": run_id,
                "schema_version": RUN_RECEIPT_SCHEMA,
                "traceforge_version": __version__,
            },
        )
        return workspace.publish()
    except BaseException as primary_error:
        cleanup_errors = _abort_writers(writers)
        try:
            workspace.abort()
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        for cleanup_error in cleanup_errors:
            primary_error.add_note(f"清理失败：{cleanup_error!r}")
        raise
