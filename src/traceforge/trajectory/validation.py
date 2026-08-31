"""对 M1A、M1B 编译产物进行独立、流式验收。"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from traceforge.trajectory.contracts import (
    ACTION_BATCH_SCHEMA,
    ARTIFACT_MANIFEST_SCHEMA,
    ATTRITION_REPORT_SCHEMA,
    CAPTURE_QUALITY_SCHEMA,
    CAPTURE_SCHEMA,
    COMPILER_CONTRACT_VERSION,
    EVENT_SCHEMA,
    REQUEST_BOUNDARY_SCHEMA,
    SOURCE_MANIFEST_SCHEMA,
    SOURCE_RECORD_SCHEMA,
    TOOL_CATALOG_SCHEMA,
    TOOL_PAIRING_SCHEMA,
    ActionBatchV1,
    ArtifactEntryV1,
    ArtifactManifestV1,
    AttritionReportV1,
    CaptureQualityV1,
    EventOccurrenceV1,
    NormalizedCaptureV1,
    RequestBoundaryV1,
    SourceManifestV1,
    SourceRecordRefV1,
    ToolCatalogV1,
    ToolPairingRecordV1,
    ToolPairingStatus,
)
from traceforge.trajectory.json_codec import (
    StrictJsonError,
    canonical_json_bytes,
    canonical_json_line,
    sha256_bytes,
    source_record_id,
    stable_id,
    strict_json_loads,
)
from traceforge.trajectory.source_adapter import RESTORED_LONG_CAPTURE_SCHEMA

_PRIVATE_CONTRACTS = {
    "private/source_records.jsonl": (SOURCE_RECORD_SCHEMA, SourceRecordRefV1),
    "private/captures.jsonl": (CAPTURE_SCHEMA, NormalizedCaptureV1),
    "private/request_boundaries.jsonl": (REQUEST_BOUNDARY_SCHEMA, RequestBoundaryV1),
    "private/event_occurrences.jsonl": (EVENT_SCHEMA, EventOccurrenceV1),
    "private/action_batches.jsonl": (ACTION_BATCH_SCHEMA, ActionBatchV1),
    "private/tool_pairings.jsonl": (TOOL_PAIRING_SCHEMA, ToolPairingRecordV1),
    "private/tool_catalogs.jsonl": (TOOL_CATALOG_SCHEMA, ToolCatalogV1),
    "private/capture_quality.jsonl": (CAPTURE_QUALITY_SCHEMA, CaptureQualityV1),
}
_DETERMINISTIC_FILES = frozenset(
    {"source_manifest.json", "reports/attrition_report.json", *_PRIVATE_CONTRACTS}
)
_ALL_FILES = _DETERMINISTIC_FILES | {"artifact_manifest.json", "run_receipt.json"}
_RECEIPT_FIELDS = frozenset(
    {
        "artifact_manifest_sha256",
        "completed_at",
        "duration_seconds",
        "input_path",
        "output_path",
        "python",
        "run_id",
        "schema_version",
        "traceforge_version",
    }
)
_COUNT_KEYS = frozenset(
    {
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
        "terminal_text_outcome_capture_count",
        "terminal_tool_call_pending_capture_count",
        "terminal_empty_outcome_capture_count",
        "terminal_invalid_capture_count",
        "processing_complete_count",
        "processing_partial_count",
        "processing_quarantined_count",
    }
)
_EVENT_COUNT_KEYS = {
    "SYSTEM": "system_event_count",
    "USER": "user_event_count",
    "ASSISTANT_MESSAGE": "assistant_message_event_count",
    "TOOL_CALL": "tool_call_event_count",
    "TOOL_RESULT": "tool_result_event_count",
}
_SCOPE_COUNT_KEYS = {
    "PRE_FIRST_OBSERVED_TERMINAL": "pre_first_observed_terminal_event_count",
    "OBSERVED_REQUEST_WINDOW": "observed_request_window_event_count",
}
_PAIRING_COUNT_KEYS = {
    "MATCHED_ONE_TO_ONE": "matched_one_to_one_pairing_count",
    "RESULT_NOT_OBSERVED": "unobserved_call_id_count",
    "DUPLICATE_CALL_ID": "duplicate_call_id_group_count",
    "DUPLICATE_RESULT": "duplicate_result_group_count",
    "ORPHAN_RESULT": "orphan_result_group_count",
    "NAME_MISMATCH": "name_mismatch_group_count",
    "RESULT_BEFORE_CALL": "result_before_call_group_count",
    "INVALID_CALL_ARGUMENTS": "invalid_call_arguments_group_count",
}
_SCHEMA_COUNT_KEYS = {
    "CONSISTENT": "schema_consistent_capture_count",
    "INFERRED": "schema_inferred_capture_count",
    "CONFLICT_REPORTED": "schema_conflict_reported_capture_count",
    "INFERRED_AND_CONFLICT": "schema_inferred_and_conflict_capture_count",
    "INVALID": "schema_invalid_capture_count",
}
_TERMINAL_COUNT_KEYS = {
    "TEXT_OUTCOME": "terminal_text_outcome_capture_count",
    "TOOL_CALL_PENDING": "terminal_tool_call_pending_capture_count",
    "EMPTY_OUTCOME": "terminal_empty_outcome_capture_count",
    "INVALID": "terminal_invalid_capture_count",
}
_PROCESSING_COUNT_KEYS = {
    "COMPLETE": "processing_complete_count",
    "PARTIAL": "processing_partial_count",
    "QUARANTINED": "processing_quarantined_count",
}
_PAIRING_ORDER = tuple(status.value for status in ToolPairingStatus)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """一条不携带业务原文的校验错误。"""

    code: str
    location: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """完整 run 的验收结果。"""

    ok: bool
    issues: tuple[ValidationIssue, ...]
    observed_counts: dict[str, int]
    source_manifest: dict[str, Any] | None
    checked_file_count: int

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(f"[{issue.code}] {issue.location}: {issue.message}" for issue in self.issues)


class _Issues:
    """限制错误量，避免损坏的大文件反向耗尽内存。"""

    def __init__(self, limit: int = 300) -> None:
        self.items: list[ValidationIssue] = []
        self.limit = limit
        self.dropped = 0

    def add(self, code: str, location: str, message: str) -> None:
        if len(self.items) < self.limit:
            self.items.append(ValidationIssue(code, location, message))
        else:
            self.dropped += 1

    def finish(self) -> tuple[ValidationIssue, ...]:
        if self.dropped:
            self.items.append(
                ValidationIssue(
                    "ERROR_LIMIT_REACHED",
                    "run",
                    f"另有 {self.dropped} 条错误未展开",
                )
            )
        return tuple(self.items)


@dataclass(frozen=True, slots=True)
class _Capture:
    source_id: str
    boundary_ids: tuple[str, ...]
    catalog_id: str
    has_compaction: bool
    compaction_count: int
    has_compaction_hashes: bool


@dataclass(frozen=True, slots=True)
class _Boundary:
    capture_id: str
    terminal_event_id: str
    source_request_id: str


@dataclass(frozen=True, slots=True)
class _Event:
    capture_id: str
    kind: str
    boundary_id: str | None
    tool_call_id: str | None
    assistant_event_id: str | None


@dataclass(slots=True)
class _State:
    counts: Counter[str]
    sources: dict[str, str]
    captures: dict[str, _Capture]
    boundaries: dict[str, _Boundary]
    boundary_ids_by_capture: dict[str, list[str]]
    events: dict[str, _Event]
    catalogs: dict[str, str]
    unique_request_ids: set[str]
    event_count_by_capture: Counter[str]
    batch_count_by_capture: Counter[str]
    call_count_by_capture: Counter[str]
    result_count_by_capture: Counter[str]
    pair_statuses_by_capture: dict[str, set[str]]
    missing_count_by_capture: Counter[str]
    duplicate_result_count_by_capture: Counter[str]

    @classmethod
    def create(cls) -> _State:
        return cls(
            counts=Counter({key: 0 for key in _COUNT_KEYS}),
            sources={},
            captures={},
            boundaries={},
            boundary_ids_by_capture=defaultdict(list),
            events={},
            catalogs={},
            unique_request_ids=set(),
            event_count_by_capture=Counter(),
            batch_count_by_capture=Counter(),
            call_count_by_capture=Counter(),
            result_count_by_capture=Counter(),
            pair_statuses_by_capture=defaultdict(set),
            missing_count_by_capture=Counter(),
            duplicate_result_count_by_capture=Counter(),
        )


def validate_compiled_run(
    run_path: str | Path,
) -> ValidationResult:
    """流式校验一个 M1 run；只保留外键索引，不保留 event payload。"""

    root = Path(run_path)
    issues = _Issues()
    if not root.is_dir():
        issues.add("RUN_NOT_DIRECTORY", "run", "run_path 不是可读目录")
        finished = issues.finish()
        return ValidationResult(False, finished, {}, None, 0)

    artifact_manifest = _read_json(root / "artifact_manifest.json", issues)
    entries = _manifest_entries(root, artifact_manifest, issues)
    checked = _check_artifact_bytes(root, entries, issues)
    _check_inventory(root, issues)

    source_manifest = _read_json(root / "source_manifest.json", issues)
    _check_contract(
        source_manifest,
        SourceManifestV1,
        SOURCE_MANIFEST_SCHEMA,
        "source_manifest.json",
        issues,
    )
    report = _read_json(root / "reports/attrition_report.json", issues)
    _check_public_report(report, issues)
    receipt = _read_json(root / "run_receipt.json", issues)
    _check_receipt(root, receipt, artifact_manifest, issues)
    _check_run_identity(root, source_manifest, artifact_manifest, report, issues)

    state = _State.create()
    _read_sources(root, source_manifest, state, issues)
    _read_captures(root, state, issues)
    _read_boundaries(root, state, issues)
    _read_events(root, state, issues)
    _check_boundary_edges(state, issues)
    _read_action_batches(root, state, issues)
    _read_pairings(root, state, issues)
    _read_catalogs(root, state, issues)
    _read_quality(root, state, issues)
    _finish_counts(state)
    observed = {key: state.counts[key] for key in sorted(_COUNT_KEYS)}
    _compare_report(report, observed, issues)

    finished = issues.finish()
    return ValidationResult(
        ok=not finished,
        issues=finished,
        observed_counts=observed,
        source_manifest=source_manifest,
        checked_file_count=checked,
    )


def _manifest_entries(
    root: Path,
    manifest: dict[str, Any] | None,
    issues: _Issues,
) -> dict[str, dict[str, Any]]:
    if not _check_contract(
        manifest,
        ArtifactManifestV1,
        ARTIFACT_MANIFEST_SCHEMA,
        "artifact_manifest.json",
        issues,
    ):
        return {}
    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        issues.add("MANIFEST_FILES_INVALID", "artifact_manifest.json/files", "files 必须是数组")
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(raw_entries):
        location = f"artifact_manifest.json/files/{index}"
        if not _check_contract(entry, ArtifactEntryV1, None, location, issues):
            continue
        relative = entry.get("relative_path")
        if not isinstance(relative, str) or not relative:
            issues.add("MANIFEST_PATH_INVALID", location, "relative_path 必须是非空字符串")
            continue
        try:
            (root / relative).resolve().relative_to(root.resolve())
        except ValueError:
            issues.add("MANIFEST_PATH_ESCAPES_RUN", location, "relative_path 越出 run 目录")
            continue
        if relative in entries:
            issues.add("MANIFEST_PATH_DUPLICATE", location, "relative_path 重复")
        entries[relative] = entry
    if frozenset(entries) != _DETERMINISTIC_FILES:
        issues.add(
            "MANIFEST_FILE_SET_MISMATCH",
            "artifact_manifest.json/files",
            "manifest 文件集合与 M1 v1 不一致",
        )
    return entries


def _check_artifact_bytes(
    root: Path,
    entries: Mapping[str, Mapping[str, Any]],
    issues: _Issues,
) -> int:
    checked = 0
    for relative, entry in sorted(entries.items()):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            issues.add("ARTIFACT_NOT_REGULAR_FILE", relative, "artifact 不存在或不是普通文件")
            continue
        digest = hashlib.sha256()
        size = lines = 0
        has_base64_data_url = False
        scan_tail = b""
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                lines += chunk.count(b"\n")
                lowered = scan_tail + chunk.lower()
                has_base64_data_url |= b";base64," in lowered
                scan_tail = lowered[-7:]
        checked += 1
        if digest.hexdigest() != entry.get("sha256"):
            issues.add("ARTIFACT_SHA256_MISMATCH", relative, "SHA-256 与 manifest 不一致")
        if size != entry.get("byte_length"):
            issues.add("ARTIFACT_SIZE_MISMATCH", relative, "字节数与 manifest 不一致")
        expected_records = entry.get("record_count")
        if relative.endswith(".jsonl"):
            if not _is_int(expected_records) or lines != expected_records:
                issues.add(
                    "ARTIFACT_RECORD_COUNT_MISMATCH",
                    relative,
                    "JSONL 行数与 manifest 不一致",
                )
        elif expected_records is not None:
            issues.add(
                "ARTIFACT_RECORD_COUNT_INVALID",
                relative,
                "单体 JSON 的 record_count 必须为 null",
            )
        if has_base64_data_url:
            issues.add(
                "BASE64_DATA_URL_OBSERVED",
                relative,
                "派生 artifact 包含 ;base64, 标记",
            )
    return checked


def _check_inventory(root: Path, issues: _Issues) -> None:
    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if observed != _ALL_FILES:
        issues.add("RUN_FILE_SET_MISMATCH", "run", "run 文件集合与 M1 v1 不一致")


def _check_public_report(report: dict[str, Any] | None, issues: _Issues) -> None:
    if not _check_contract(
        report,
        AttritionReportV1,
        ATTRITION_REPORT_SCHEMA,
        "reports/attrition_report.json",
        issues,
    ):
        return
    counts = report.get("counts")
    if not isinstance(counts, dict) or frozenset(counts) != _COUNT_KEYS:
        issues.add(
            "PUBLIC_REPORT_ALLOWLIST_MISMATCH",
            "reports/attrition_report.json/counts",
            "公共报告必须只包含固定聚合计数",
        )
        return
    if any(not _is_int(value) or value < 0 for value in counts.values()):
        issues.add(
            "PUBLIC_REPORT_VALUE_INVALID",
            "reports/attrition_report.json/counts",
            "公共报告计数必须是非负整数",
        )


def _check_receipt(
    root: Path,
    receipt: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
    issues: _Issues,
) -> None:
    if not isinstance(receipt, dict):
        return
    if frozenset(receipt) != _RECEIPT_FIELDS:
        issues.add("RUN_RECEIPT_SCHEMA_MISMATCH", "run_receipt.json", "运行回执字段不匹配 v1")
    if receipt.get("schema_version") != "traceforge.run-receipt.v1":
        issues.add("RUN_RECEIPT_SCHEMA_INVALID", "run_receipt.json", "schema_version 不匹配")
    manifest_path = root / "artifact_manifest.json"
    if manifest_path.is_file() and receipt.get("artifact_manifest_sha256") != sha256_bytes(
        manifest_path.read_bytes()
    ):
        issues.add(
            "RUN_RECEIPT_MANIFEST_HASH_MISMATCH",
            "run_receipt.json",
            "artifact manifest 摘要不匹配",
        )
    if isinstance(manifest, dict) and receipt.get("run_id") != manifest.get("run_id"):
        issues.add("RUN_RECEIPT_RUN_ID_MISMATCH", "run_receipt.json", "run_id 不匹配")


def _check_run_identity(
    root: Path,
    source: dict[str, Any] | None,
    artifact: dict[str, Any] | None,
    report: dict[str, Any] | None,
    issues: _Issues,
) -> None:
    if not isinstance(source, dict) or not isinstance(artifact, dict):
        return
    if source.get("source_schema") != RESTORED_LONG_CAPTURE_SCHEMA:
        issues.add(
            "SOURCE_SCHEMA_UNSUPPORTED",
            "source_manifest.json/source_schema",
            "source_schema 不是当前 validator 唯一支持的版本",
        )
    if artifact.get("source_schema") != RESTORED_LONG_CAPTURE_SCHEMA:
        issues.add(
            "SOURCE_SCHEMA_UNSUPPORTED",
            "artifact_manifest.json/source_schema",
            "source_schema 不是当前 validator 唯一支持的版本",
        )
    if artifact.get("source_schema") != source.get("source_schema"):
        issues.add(
            "MANIFEST_IDENTITY_MISMATCH",
            "source_schema",
            "artifact/source manifest 不一致",
        )
    for key in ("dataset_id", "dataset_sha256"):
        if artifact.get(key) != source.get(key):
            issues.add("MANIFEST_IDENTITY_MISMATCH", key, "artifact/source manifest 不一致")
        if isinstance(report, dict) and report.get(key) != source.get(key):
            issues.add("REPORT_IDENTITY_MISMATCH", key, "report/source manifest 不一致")
    if artifact.get("compiler_contract_version") != COMPILER_CONTRACT_VERSION:
        issues.add(
            "COMPILER_CONTRACT_VERSION_MISMATCH",
            "artifact_manifest.json",
            "编译契约版本不匹配",
        )
        return
    expected_run_id = stable_id(
        "trajectory-compile-run-v1",
        {
            "compiler_contract_version": COMPILER_CONTRACT_VERSION,
            "dataset_id": source.get("dataset_id"),
            "dataset_sha256": source.get("dataset_sha256"),
            "source_schema": source.get("source_schema"),
        },
    )
    if artifact.get("run_id") != expected_run_id:
        issues.add("RUN_ID_MISMATCH", "artifact_manifest.json", "内容寻址 run_id 不匹配")
    if root.name != expected_run_id:
        issues.add("RUN_DIRECTORY_ID_MISMATCH", "run", "目录名不等于内容寻址 run_id")


def _read_sources(
    root: Path,
    manifest: dict[str, Any] | None,
    state: _State,
    issues: _Issues,
) -> None:
    expected_dataset_id = manifest.get("dataset_id") if isinstance(manifest, dict) else None
    expected_dataset_sha256 = manifest.get("dataset_sha256") if isinstance(manifest, dict) else None
    if isinstance(manifest, dict) and manifest.get("source_format") != "jsonl":
        issues.add(
            "SOURCE_FORMAT_UNSUPPORTED",
            "source_manifest.json/source_format",
            "source_format 必须为 jsonl",
        )
    byte_length = 0
    for ordinal, record in enumerate(_iter_jsonl(root, "private/source_records.jsonl", issues), 1):
        location = f"private/source_records.jsonl:{ordinal}"
        source_id = record.get("source_record_id")
        if not isinstance(source_id, str) or source_id in state.sources:
            issues.add("SOURCE_ID_INVALID", location, "source_record_id 缺失或重复")
            continue
        status = record.get("ingestion_status")
        if not isinstance(status, str) or status not in {"PARSED", "QUARANTINED"}:
            issues.add("SOURCE_STATUS_INVALID", location, "ingestion_status 非法")
        state.sources[source_id] = str(status)
        if (
            record.get("dataset_id") != expected_dataset_id
            or record.get("dataset_sha256") != expected_dataset_sha256
        ):
            issues.add(
                "SOURCE_RECORD_DATASET_MISMATCH",
                location,
                "source record 与 source manifest 的数据集身份不一致",
            )
        if all(
            (
                isinstance(record.get("dataset_id"), str),
                _is_sha256(record.get("dataset_sha256")),
                _is_int(record.get("line_number")),
                _is_sha256(record.get("line_sha256")),
            )
        ):
            expected_id = source_record_id(
                dataset_id=record["dataset_id"],
                dataset_sha256=record["dataset_sha256"],
                line_number=record["line_number"],
                line_sha256=record["line_sha256"],
            )
            _check_stable_id(source_id, expected_id, location, issues)
        state.counts["physical_line_count"] += 1
        state.counts[
            "parsed_source_record_count"
            if status == "PARSED"
            else "quarantined_source_record_count"
        ] += 1
        if record.get("line_number") != ordinal or record.get("byte_offset") != byte_length:
            issues.add("SOURCE_POSITION_INVALID", location, "line_number 或 byte_offset 不连续")
        length = record.get("byte_length")
        if _is_int(length) and length >= 0:
            byte_length += length
    if isinstance(manifest, dict):
        if manifest.get("physical_line_count") != state.counts["physical_line_count"]:
            issues.add("SOURCE_LINE_COUNT_MISMATCH", "source_manifest.json", "物理行数不匹配")
        if manifest.get("byte_length") != byte_length:
            issues.add("SOURCE_BYTE_LENGTH_MISMATCH", "source_manifest.json", "来源字节数不匹配")


def _read_captures(root: Path, state: _State, issues: _Issues) -> None:
    for line_number, record in enumerate(_iter_jsonl(root, "private/captures.jsonl", issues), 1):
        location = f"private/captures.jsonl:{line_number}"
        capture_id = record.get("capture_occurrence_id")
        source_id = record.get("source_record_id")
        boundary_ids = record.get("request_boundary_ids")
        catalog_id = record.get("tool_catalog_id")
        if not isinstance(capture_id, str) or capture_id in state.captures:
            issues.add("CAPTURE_ID_INVALID", location, "capture_occurrence_id 缺失或重复")
            continue
        if not isinstance(source_id, str) or source_id not in state.sources:
            issues.add("CAPTURE_SOURCE_NOT_FOUND", location, "source_record_id 不存在")
            source_id = ""
        if not isinstance(boundary_ids, list) or not all(
            isinstance(item, str) for item in boundary_ids
        ):
            issues.add("CAPTURE_BOUNDARY_IDS_INVALID", location, "request_boundary_ids 非法")
            boundary_ids = []
        if not isinstance(catalog_id, str):
            issues.add("CAPTURE_CATALOG_ID_INVALID", location, "tool_catalog_id 非法")
            catalog_id = ""
        has_compaction = record.get("has_compaction") is True
        compaction_count = record.get("compaction_count")
        compaction_count = compaction_count if _is_int(compaction_count) else 0
        compaction_hashes = record.get("compaction_hashes")
        if not isinstance(compaction_hashes, list):
            issues.add(
                "CAPTURE_COMPACTION_HASHES_INVALID",
                location,
                "compaction_hashes 必须是数组",
            )
            compaction_hashes = []
        source_capture_id = record.get("source_capture_id")
        if isinstance(source_capture_id, str):
            expected_id = stable_id(
                "capture-occurrence-v1",
                {
                    "source_record_id": source_id,
                    "source_capture_id": source_capture_id,
                },
            )
            _check_stable_id(capture_id, expected_id, location, issues)
        state.captures[capture_id] = _Capture(
            source_id=str(source_id),
            boundary_ids=tuple(boundary_ids),
            catalog_id=catalog_id,
            has_compaction=has_compaction,
            compaction_count=compaction_count,
            has_compaction_hashes=bool(compaction_hashes),
        )
        state.counts["normalized_capture_count"] += 1
        state.counts["compaction_capture_count"] += (
            has_compaction or compaction_count > 0 or bool(compaction_hashes)
        )


def _read_boundaries(root: Path, state: _State, issues: _Issues) -> None:
    for line_number, record in enumerate(
        _iter_jsonl(root, "private/request_boundaries.jsonl", issues),
        1,
    ):
        location = f"private/request_boundaries.jsonl:{line_number}"
        boundary_id = record.get("request_boundary_id")
        capture_id = record.get("capture_occurrence_id")
        terminal_id = record.get("terminal_event_id")
        request_id = record.get("source_request_id")
        if not all(
            isinstance(item, str) for item in (boundary_id, capture_id, terminal_id, request_id)
        ):
            issues.add("BOUNDARY_ID_INVALID", location, "boundary 外键字段非法")
            continue
        if boundary_id in state.boundaries:
            issues.add("BOUNDARY_ID_DUPLICATE", location, "request_boundary_id 重复")
            continue
        if capture_id not in state.captures:
            issues.add("BOUNDARY_CAPTURE_NOT_FOUND", location, "capture_occurrence_id 不存在")
        state.boundaries[boundary_id] = _Boundary(capture_id, terminal_id, request_id)
        if _is_int(record.get("boundary_ordinal")) and _is_int(record.get("terminal_prefix_depth")):
            expected_id = stable_id(
                "request-boundary-v1",
                {
                    "capture_occurrence_id": capture_id,
                    "boundary_ordinal": record["boundary_ordinal"],
                    "source_request_id": request_id,
                    "terminal_prefix_depth": record["terminal_prefix_depth"],
                },
            )
            _check_stable_id(boundary_id, expected_id, location, issues)
        state.boundary_ids_by_capture[capture_id].append(boundary_id)
        state.unique_request_ids.add(request_id)
        state.counts["request_boundary_count"] += 1
    for capture_id, capture in state.captures.items():
        if tuple(state.boundary_ids_by_capture[capture_id]) != capture.boundary_ids:
            issues.add("CAPTURE_BOUNDARY_EDGE_MISMATCH", capture_id, "capture/boundary 外键不闭合")


def _read_events(root: Path, state: _State, issues: _Issues) -> None:
    next_sequence: Counter[str] = Counter()
    for line_number, record in enumerate(
        _iter_jsonl(root, "private/event_occurrences.jsonl", issues),
        1,
    ):
        location = f"private/event_occurrences.jsonl:{line_number}"
        event_id = record.get("event_occurrence_id")
        capture_id = record.get("capture_occurrence_id")
        kind = record.get("event_kind")
        scope = record.get("event_scope")
        if not isinstance(event_id, str) or event_id in state.events:
            issues.add("EVENT_ID_INVALID", location, "event_occurrence_id 缺失或重复")
            continue
        if not isinstance(capture_id, str):
            issues.add("EVENT_CAPTURE_NOT_FOUND", location, "capture_occurrence_id 非法")
            capture_id = ""
        capture = state.captures.get(capture_id)
        if capture is None:
            issues.add("EVENT_CAPTURE_NOT_FOUND", location, "capture_occurrence_id 不存在")
        elif record.get("source_record_id") != capture.source_id:
            issues.add("EVENT_SOURCE_EDGE_MISMATCH", location, "event/source 外键不一致")
        if (
            not isinstance(kind, str)
            or not isinstance(scope, str)
            or kind not in _EVENT_COUNT_KEYS
            or scope not in _SCOPE_COUNT_KEYS
        ):
            issues.add("EVENT_ENUM_INVALID", location, "event kind 或 scope 非法")
            continue
        sequence = record.get("sequence_number")
        if sequence != next_sequence[str(capture_id)]:
            issues.add("EVENT_SEQUENCE_INVALID", location, "capture 内 sequence_number 不连续")
        next_sequence[str(capture_id)] += 1
        message_index = record.get("message_index")
        sub_index = record.get("sub_index")
        expected_pointer = (
            f"/messages/{message_index}/tool_calls/{sub_index}"
            if kind == "TOOL_CALL"
            else f"/messages/{message_index}"
        )
        if record.get("source_json_pointer") != expected_pointer:
            issues.add("EVENT_POINTER_MISMATCH", location, "source_json_pointer 不匹配")
        if _is_int(message_index) and (sub_index is None or _is_int(sub_index)):
            expected_id = stable_id(
                "event-occurrence-v1",
                {
                    "capture_occurrence_id": capture_id,
                    "message_index": message_index,
                    "event_kind": kind,
                    "sub_index": sub_index,
                },
            )
            _check_stable_id(event_id, expected_id, location, issues)
        payload = record.get("payload")
        if not isinstance(payload, dict):
            issues.add("EVENT_PAYLOAD_INVALID", location, "payload 必须是对象")
            continue
        visible = _visible_event_payload(kind, payload)
        encoded = canonical_json_bytes(visible)
        if record.get("visible_payload_utf8_byte_length") != len(encoded):
            issues.add("EVENT_VISIBLE_LENGTH_MISMATCH", location, "visible payload 长度不匹配")
        if record.get("visible_payload_sha256") != sha256_bytes(encoded):
            issues.add("EVENT_VISIBLE_HASH_MISMATCH", location, "visible payload hash 不匹配")
        if record.get("integrity_status") != "COMPLETE":
            issues.add(
                "EVENT_INTEGRITY_STATUS_INVALID",
                location,
                "integrity_status 必须为 COMPLETE",
            )
        if kind == "ASSISTANT_MESSAGE":
            _check_reasoning_summary(
                payload.get("reasoning_content"),
                message_index,
                location,
                issues,
            )
        boundary_id = record.get("request_boundary_id")
        if scope == "PRE_FIRST_OBSERVED_TERMINAL" and boundary_id is not None:
            issues.add("EVENT_PREFIX_BOUNDARY_INVALID", location, "prefix event 不应绑定 boundary")
        if scope == "OBSERVED_REQUEST_WINDOW":
            boundary = state.boundaries.get(boundary_id) if isinstance(boundary_id, str) else None
            if boundary is None or boundary.capture_id != capture_id:
                issues.add("EVENT_BOUNDARY_NOT_FOUND", location, "event/boundary 外键不闭合")
        tool_call_id = assistant_id = None
        if kind == "TOOL_CALL":
            tool_call_id = payload.get("tool_call_id")
            assistant_id = payload.get("assistant_event_id")
            state.call_count_by_capture[str(capture_id)] += 1
        elif kind == "TOOL_RESULT":
            tool_call_id = payload.get("tool_call_id")
            state.result_count_by_capture[str(capture_id)] += 1
        state.events[event_id] = _Event(
            capture_id=str(capture_id),
            kind=kind,
            boundary_id=boundary_id if isinstance(boundary_id, str) else None,
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
            assistant_event_id=assistant_id if isinstance(assistant_id, str) else None,
        )
        state.event_count_by_capture[str(capture_id)] += 1
        state.counts["event_occurrence_count"] += 1
        state.counts[_EVENT_COUNT_KEYS[kind]] += 1
        state.counts[_SCOPE_COUNT_KEYS[scope]] += 1
    for event_id, event in state.events.items():
        if event.kind != "TOOL_CALL":
            continue
        assistant = state.events.get(event.assistant_event_id or "")
        if (
            assistant is None
            or assistant.kind != "ASSISTANT_MESSAGE"
            or assistant.capture_id != event.capture_id
        ):
            issues.add("TOOL_CALL_ASSISTANT_NOT_FOUND", event_id, "assistant 外键不闭合")


def _check_reasoning_summary(
    summary: Any,
    message_index: Any,
    location: str,
    issues: _Issues,
) -> None:
    keys = {"present", "utf8_byte_length", "sha256", "source_json_pointer"}
    if not isinstance(summary, dict) or set(summary) != keys:
        issues.add("REASONING_SUMMARY_INVALID", location, "reasoning 只能保存固定审计摘要")
        return
    if summary.get("present") is True:
        if not _is_int(summary.get("utf8_byte_length")) or not _is_sha256(summary.get("sha256")):
            issues.add("REASONING_SUMMARY_INVALID", location, "reasoning 摘要长度或 hash 非法")
        if summary.get("source_json_pointer") != f"/messages/{message_index}/reasoning_content":
            issues.add("REASONING_POINTER_MISMATCH", location, "reasoning pointer 不匹配")
    elif summary != {
        "present": False,
        "utf8_byte_length": 0,
        "sha256": None,
        "source_json_pointer": None,
    }:
        issues.add("REASONING_SUMMARY_INVALID", location, "不存在的 reasoning 摘要必须为空")


def _visible_event_payload(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """按 compiler 契约移除只用于审计的 reasoning 摘要。"""

    visible = dict(payload)
    if kind == "ASSISTANT_MESSAGE":
        visible.pop("reasoning_content", None)
    _remove_extension_reasoning_summary(visible)
    if kind == "TOOL_CALL" and isinstance(visible.get("function"), dict):
        function = dict(visible["function"])
        _remove_extension_reasoning_summary(function)
        visible["function"] = function
    return visible


def _remove_extension_reasoning_summary(container: dict[str, Any]) -> None:
    extensions = container.get("extensions")
    if not isinstance(extensions, dict) or "reasoning_content" not in extensions:
        return
    visible_extensions = {
        key: value for key, value in extensions.items() if key != "reasoning_content"
    }
    if visible_extensions:
        container["extensions"] = visible_extensions
    else:
        container.pop("extensions")


def _check_boundary_edges(state: _State, issues: _Issues) -> None:
    for boundary_id, boundary in state.boundaries.items():
        event = state.events.get(boundary.terminal_event_id)
        if (
            event is None
            or event.kind != "ASSISTANT_MESSAGE"
            or event.capture_id != boundary.capture_id
            or event.boundary_id != boundary_id
        ):
            issues.add("BOUNDARY_TERMINAL_EDGE_MISMATCH", boundary_id, "terminal event 外键不闭合")


def _read_action_batches(root: Path, state: _State, issues: _Issues) -> None:
    covered_calls: Counter[str] = Counter()
    assistants: set[str] = set()
    for line_number, record in enumerate(
        _iter_jsonl(root, "private/action_batches.jsonl", issues),
        1,
    ):
        location = f"private/action_batches.jsonl:{line_number}"
        capture_id = record.get("capture_occurrence_id")
        assistant_id = record.get("assistant_event_id")
        assistant = state.events.get(assistant_id) if isinstance(assistant_id, str) else None
        if (
            assistant is None
            or assistant.kind != "ASSISTANT_MESSAGE"
            or assistant.capture_id != capture_id
        ):
            issues.add("ACTION_ASSISTANT_NOT_FOUND", location, "assistant 外键不闭合")
        if isinstance(assistant_id, str) and assistant_id in assistants:
            issues.add("ACTION_ASSISTANT_DUPLICATE", location, "同一 assistant 有多个 batch")
        if isinstance(assistant_id, str):
            assistants.add(assistant_id)
        boundary_id = record.get("request_boundary_id")
        if boundary_id is not None:
            boundary = state.boundaries.get(boundary_id) if isinstance(boundary_id, str) else None
            if boundary is None or boundary.capture_id != capture_id:
                issues.add("ACTION_BOUNDARY_NOT_FOUND", location, "boundary 外键不闭合")
        call_ids = record.get("tool_call_event_ids")
        if not isinstance(call_ids, list) or not all(isinstance(item, str) for item in call_ids):
            issues.add("ACTION_TOOL_CALLS_INVALID", location, "tool_call_event_ids 必须是数组")
            call_ids = []
        batch_id = record.get("action_batch_id")
        if all(isinstance(value, str) for value in (batch_id, capture_id, assistant_id)):
            expected_id = stable_id(
                "action-batch-v1",
                {
                    "capture_occurrence_id": capture_id,
                    "assistant_event_id": assistant_id,
                    "tool_call_event_ids": call_ids,
                },
            )
            _check_stable_id(batch_id, expected_id, location, issues)
        for event_id in call_ids:
            event = state.events.get(event_id)
            if (
                event is None
                or event.kind != "TOOL_CALL"
                or event.capture_id != capture_id
                or event.assistant_event_id != assistant_id
            ):
                issues.add("ACTION_TOOL_CALL_EDGE_MISMATCH", location, "tool call 外键不闭合")
            if isinstance(event_id, str):
                covered_calls[event_id] += 1
        state.batch_count_by_capture[str(capture_id)] += 1
        state.counts["action_batch_count"] += 1
    expected_calls = {
        event_id for event_id, event in state.events.items() if event.kind == "TOOL_CALL"
    }
    if set(covered_calls) != expected_calls or any(value != 1 for value in covered_calls.values()):
        issues.add(
            "ACTION_BATCH_COVERAGE_MISMATCH",
            "private/action_batches.jsonl",
            "ActionBatch 未一一覆盖 tool call",
        )


def _read_pairings(root: Path, state: _State, issues: _Issues) -> None:
    covered: Counter[str] = Counter()
    groups: set[tuple[str, str]] = set()
    for line_number, record in enumerate(
        _iter_jsonl(root, "private/tool_pairings.jsonl", issues),
        1,
    ):
        location = f"private/tool_pairings.jsonl:{line_number}"
        capture_id = record.get("capture_occurrence_id")
        call_id = record.get("tool_call_id")
        if not isinstance(capture_id, str) or not isinstance(call_id, str):
            issues.add("PAIRING_GROUP_INVALID", location, "capture/tool call ID 必须是字符串")
            capture_id = capture_id if isinstance(capture_id, str) else ""
            call_id = call_id if isinstance(call_id, str) else ""
        group = (str(capture_id), str(call_id))
        if group in groups:
            issues.add("PAIRING_GROUP_DUPLICATE", location, "pairing group 重复")
        groups.add(group)
        pairing_id = record.get("pairing_id")
        if all(isinstance(value, str) for value in (pairing_id, capture_id, call_id)):
            expected_id = stable_id(
                "tool-pairing-v1",
                {"capture_occurrence_id": capture_id, "tool_call_id": call_id},
            )
            _check_stable_id(pairing_id, expected_id, location, issues)
        call_events = record.get("call_event_ids")
        result_events = record.get("result_event_ids")
        if (
            not isinstance(call_events, list)
            or not all(isinstance(item, str) for item in call_events)
            or not isinstance(result_events, list)
            or not all(isinstance(item, str) for item in result_events)
        ):
            issues.add("PAIRING_EVENT_IDS_INVALID", location, "call/result event IDs 必须是数组")
            continue
        _check_pairing_events(
            call_events,
            "TOOL_CALL",
            capture_id,
            call_id,
            state,
            covered,
            location,
            issues,
        )
        _check_pairing_events(
            result_events,
            "TOOL_RESULT",
            capture_id,
            call_id,
            state,
            covered,
            location,
            issues,
        )
        if (
            record.get("matched_call_event_id") is not None
            and record.get("matched_call_event_id") not in call_events
        ):
            issues.add("PAIRING_MATCHED_CALL_INVALID", location, "matched call 不在 occurrence 中")
        if (
            record.get("matched_result_event_id") is not None
            and record.get("matched_result_event_id") not in result_events
        ):
            issues.add(
                "PAIRING_MATCHED_RESULT_INVALID",
                location,
                "matched result 不在 occurrence 中",
            )
        statuses = record.get("statuses")
        if (
            not isinstance(statuses, list)
            or not all(isinstance(status, str) for status in statuses)
            or statuses != [status for status in _PAIRING_ORDER if status in statuses]
        ):
            issues.add("PAIRING_STATUSES_INVALID", location, "statuses 非法或顺序不稳定")
            statuses = []
        state.pair_statuses_by_capture[str(capture_id)].update(statuses)
        for status in statuses:
            count_key = _PAIRING_COUNT_KEYS.get(status)
            if count_key is None:
                issues.add("PAIRING_STATUS_UNKNOWN", location, "出现未知 pairing status")
            else:
                state.counts[count_key] += 1
        if "RESULT_NOT_OBSERVED" in statuses:
            state.missing_count_by_capture[str(capture_id)] += 1
        if "DUPLICATE_RESULT" in statuses:
            extra = max(0, len(result_events) - 1)
            state.duplicate_result_count_by_capture[str(capture_id)] += extra
            state.counts["extra_result_occurrence_count"] += extra
        state.counts["tool_pairing_record_count"] += 1
    expected = {
        event_id
        for event_id, event in state.events.items()
        if event.kind in {"TOOL_CALL", "TOOL_RESULT"}
    }
    if set(covered) != expected or any(value != 1 for value in covered.values()):
        issues.add(
            "PAIRING_COVERAGE_MISMATCH",
            "private/tool_pairings.jsonl",
            "pairing 未一一覆盖 tool occurrence",
        )


def _check_pairing_events(
    event_ids: list[Any],
    expected_kind: str,
    capture_id: Any,
    call_id: Any,
    state: _State,
    covered: Counter[str],
    location: str,
    issues: _Issues,
) -> None:
    for event_id in event_ids:
        if not isinstance(event_id, str):
            issues.add("PAIRING_EVENT_EDGE_MISMATCH", location, "tool occurrence ID 非法")
            continue
        event = state.events.get(event_id)
        if (
            event is None
            or event.kind != expected_kind
            or event.capture_id != capture_id
            or event.tool_call_id != call_id
        ):
            issues.add("PAIRING_EVENT_EDGE_MISMATCH", location, "tool occurrence 外键不闭合")
        if isinstance(event_id, str):
            covered[event_id] += 1


def _read_catalogs(root: Path, state: _State, issues: _Issues) -> None:
    by_capture: dict[str, str] = {}
    for line_number, record in enumerate(
        _iter_jsonl(root, "private/tool_catalogs.jsonl", issues),
        1,
    ):
        location = f"private/tool_catalogs.jsonl:{line_number}"
        catalog_id = record.get("tool_catalog_id")
        capture_id = record.get("capture_occurrence_id")
        if not isinstance(catalog_id, str) or catalog_id in state.catalogs:
            issues.add("CATALOG_ID_INVALID", location, "tool_catalog_id 缺失或重复")
            continue
        if not isinstance(capture_id, str) or capture_id not in state.captures:
            issues.add("CATALOG_CAPTURE_NOT_FOUND", location, "capture 外键不存在")
            capture_id = ""
        state.catalogs[catalog_id] = str(capture_id)
        by_capture[str(capture_id)] = catalog_id
        definitions = record.get("definitions")
        inferred_names = record.get("inferred_tool_names")
        if not isinstance(definitions, list) or not isinstance(inferred_names, list):
            issues.add("CATALOG_CONTENT_INVALID", location, "definitions/inferred names 必须是数组")
            continue
        catalog_sha256 = sha256_bytes(canonical_json_bytes(definitions))
        if record.get("catalog_sha256") != catalog_sha256:
            issues.add("CATALOG_HASH_MISMATCH", location, "catalog_sha256 不匹配")
        expected_id = stable_id(
            "tool-catalog-v1",
            {
                "capture_occurrence_id": capture_id,
                "catalog_sha256": catalog_sha256,
            },
        )
        _check_stable_id(catalog_id, expected_id, location, issues)
        state.counts["tool_definition_count"] += len(definitions)
        state.counts["inferred_tool_name_annotation_count"] += len(inferred_names)
    for capture_id, capture in state.captures.items():
        if by_capture.get(capture_id) != capture.catalog_id:
            issues.add("CAPTURE_CATALOG_EDGE_MISMATCH", capture_id, "capture/catalog 外键不闭合")


def _read_quality(root: Path, state: _State, issues: _Issues) -> None:
    seen_sources: set[str] = set()
    seen_captures: set[str] = set()
    for line_number, record in enumerate(
        _iter_jsonl(root, "private/capture_quality.jsonl", issues),
        1,
    ):
        location = f"private/capture_quality.jsonl:{line_number}"
        source_id = record.get("source_record_id")
        capture_id = record.get("capture_occurrence_id")
        if (
            not isinstance(source_id, str)
            or source_id not in state.sources
            or source_id in seen_sources
        ):
            issues.add("QUALITY_SOURCE_EDGE_MISMATCH", location, "source 外键缺失或重复")
        if isinstance(source_id, str):
            seen_sources.add(source_id)
        if capture_id is None:
            if record.get("processing_status") != "QUARANTINED" or not isinstance(
                record.get("processing_error"),
                dict,
            ):
                issues.add("QUALITY_QUARANTINE_INVALID", location, "隔离终态缺少 processing_error")
        elif not isinstance(capture_id, str):
            issues.add("QUALITY_CAPTURE_EDGE_MISMATCH", location, "capture_occurrence_id 非法")
        else:
            _check_capture_quality(record, capture_id, str(source_id), state, location, issues)
            seen_captures.add(capture_id)
        if capture_id is not None:
            _increment_enum_count(
                record.get("tool_schema_status"),
                _SCHEMA_COUNT_KEYS,
                state,
                location,
                issues,
            )
            _increment_enum_count(
                record.get("terminal_status"),
                _TERMINAL_COUNT_KEYS,
                state,
                location,
                issues,
            )
        _increment_enum_count(
            record.get("processing_status"),
            _PROCESSING_COUNT_KEYS,
            state,
            location,
            issues,
        )
        reasons = record.get("reason_codes")
        if isinstance(reasons, list) and "INPUT_TRUNCATED" in reasons:
            state.counts["input_truncated_capture_count"] += 1
    if seen_sources != set(state.sources):
        issues.add(
            "QUALITY_SOURCE_COVERAGE_MISMATCH",
            "private/capture_quality.jsonl",
            "source 终态未一一覆盖",
        )
    if seen_captures != set(state.captures):
        issues.add(
            "QUALITY_CAPTURE_COVERAGE_MISMATCH",
            "private/capture_quality.jsonl",
            "capture 终态未一一覆盖",
        )


def _check_capture_quality(
    record: Mapping[str, Any],
    capture_id: str,
    source_id: str,
    state: _State,
    location: str,
    issues: _Issues,
) -> None:
    capture = state.captures.get(capture_id)
    if capture is None or capture.source_id != source_id:
        issues.add("QUALITY_CAPTURE_EDGE_MISMATCH", location, "capture 外键不闭合")
        return
    if record.get("processing_error") is not None:
        issues.add(
            "QUALITY_PROCESSING_ERROR_INVALID",
            location,
            "完整 capture 不应有 processing_error",
        )
    structural_counts = {
        "boundary_count": len(state.boundary_ids_by_capture[capture_id]),
        "event_count": state.event_count_by_capture[capture_id],
        "action_batch_count": state.batch_count_by_capture[capture_id],
        "tool_call_count": state.call_count_by_capture[capture_id],
        "tool_result_count": state.result_count_by_capture[capture_id],
        "missing_result_count": state.missing_count_by_capture[capture_id],
        "duplicate_result_count": state.duplicate_result_count_by_capture[capture_id],
    }
    for key, expected in structural_counts.items():
        if record.get(key) != expected:
            issues.add("QUALITY_COUNT_MISMATCH", f"{location}/{key}", "计数与结构事实不一致")
    raw_statuses = record.get("tool_pairing_statuses")
    if not isinstance(raw_statuses, list) or not all(
        isinstance(status, str) for status in raw_statuses
    ):
        issues.add("QUALITY_PAIRING_STATUSES_INVALID", location, "tool_pairing_statuses 非法")
        observed_statuses: set[str] = set()
    else:
        observed_statuses = set(raw_statuses)
    if observed_statuses != state.pair_statuses_by_capture[capture_id]:
        issues.add("QUALITY_PAIRING_STATUS_MISMATCH", location, "pairing 状态聚合不一致")


def _increment_enum_count(
    value: Any,
    mapping: Mapping[str, str],
    state: _State,
    location: str,
    issues: _Issues,
) -> None:
    if not isinstance(value, str) or value not in mapping:
        issues.add("QUALITY_ENUM_INVALID", location, "quality 枚举值非法")
        return
    state.counts[mapping[value]] += 1


def _finish_counts(state: _State) -> None:
    state.counts["unique_source_request_count"] = len(state.unique_request_ids)
    state.counts["captures_with_unobserved_results"] = sum(
        count > 0 for count in state.missing_count_by_capture.values()
    )


def _compare_report(
    report: Mapping[str, Any] | None,
    observed: Mapping[str, int],
    issues: _Issues,
) -> None:
    if not isinstance(report, dict) or not isinstance(report.get("counts"), dict):
        return
    for key in sorted(_COUNT_KEYS):
        if report["counts"].get(key) != observed[key]:
            issues.add(
                "ATTRITION_COUNT_MISMATCH",
                f"reports/attrition_report.json/counts/{key}",
                f"报告={report['counts'].get(key)}，重算={observed[key]}",
            )


def _iter_jsonl(root: Path, relative: str, issues: _Issues) -> Iterable[dict[str, Any]]:
    path = root / relative
    if not path.is_file():
        return
    schema, contract = _PRIVATE_CONTRACTS[relative]
    with path.open("rb") as file:
        for line_number, raw in enumerate(file, 1):
            location = f"{relative}:{line_number}"
            if not raw.endswith(b"\n"):
                issues.add("JSONL_FINAL_LF_MISSING", location, "物理行未以 LF 结束")
            try:
                value = strict_json_loads(raw)
            except StrictJsonError:
                issues.add("JSONL_STRICT_JSON_INVALID", location, "不是严格 UTF-8 JSON")
                continue
            if canonical_json_line(value) != raw:
                issues.add("JSONL_NOT_CANONICAL", location, "不是 canonical JSON + LF")
            if _check_contract(value, contract, schema, location, issues):
                yield value


def _read_json(path: Path, issues: _Issues) -> dict[str, Any] | None:
    location = path.name if path.parent.name != "reports" else f"reports/{path.name}"
    if not path.is_file():
        issues.add("JSON_FILE_MISSING", location, "文件不存在")
        return None
    raw = path.read_bytes()
    if b";base64," in raw.lower():
        issues.add("BASE64_DATA_URL_OBSERVED", location, "文件包含 ;base64, 标记")
    try:
        value = strict_json_loads(raw)
    except StrictJsonError:
        issues.add("JSON_FILE_INVALID", location, "不是严格 UTF-8 JSON")
        return None
    if canonical_json_line(value) != raw:
        issues.add("JSON_FILE_NOT_CANONICAL", location, "不是 canonical JSON + LF")
    if not isinstance(value, dict):
        issues.add("JSON_FILE_NOT_OBJECT", location, "顶层必须是对象")
        return None
    return value


def _check_contract(
    value: Any,
    contract: type[Any],
    schema: str | None,
    location: str,
    issues: _Issues,
) -> bool:
    if not isinstance(value, dict):
        issues.add("SCHEMA_NOT_OBJECT", location, "记录顶层必须是对象")
        return False
    expected_fields = {field.name for field in fields(contract)}
    if set(value) != expected_fields:
        issues.add("SCHEMA_FIELDS_MISMATCH", location, "字段集合与 v1 contract 不一致")
        return False
    if schema is not None and value.get("schema_version") != schema:
        issues.add("SCHEMA_VERSION_MISMATCH", location, "schema_version 不匹配")
        return False
    return True


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _check_stable_id(
    observed: str,
    expected: str,
    location: str,
    issues: _Issues,
) -> None:
    if observed != expected:
        issues.add("STABLE_ID_MISMATCH", location, "稳定 ID 与 v1 身份字段不匹配")
