"""对 M1A、M1B 编译产物进行独立、流式验收。"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
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
    RUN_RECEIPT_SCHEMA,
    SOURCE_MANIFEST_SCHEMA,
    SOURCE_RECORD_SCHEMA,
    TOOL_CATALOG_SCHEMA,
    TOOL_PAIRING_SCHEMA,
    ActionBatchV2,
    ArtifactEntryV1,
    ArtifactManifestV1,
    AttritionReportV2,
    BoundaryStatus,
    CaptureQualityV3,
    CompactionStatus,
    EventOccurrenceV3,
    InputTruncationStatus,
    NormalizedCaptureV3,
    PairingEndpointFacts,
    PrivacyStatus,
    ProcessingStatus,
    RequestBoundaryV1,
    SourceManifestV1,
    SourceRecordRefV1,
    TerminalStatus,
    ToolCatalogV2,
    ToolPairingRecordV3,
    ToolPairingStatus,
    ToolSchemaStatus,
    is_strict_one_to_one_match,
)
from traceforge.trajectory.event_payload import (
    AssistantMessagePayload,
    ContentBlocks,
    EventPayloadReadError,
    JsonObjectArguments,
    JsonValueArguments,
    TextContent,
    ToolCallPayload,
    ToolResultPayload,
    read_event_payload,
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
from traceforge.trajectory.privacy import (
    PrivacyTransformError,
    find_privacy_violations,
    visible_value_without_reasoning,
)
from traceforge.trajectory.run_validation import IssueCollector, ValidationIssue
from traceforge.trajectory.source import validate_dataset_id
from traceforge.trajectory.source_adapter import RESTORED_LONG_CAPTURE_SCHEMA

_PRIVATE_CONTRACTS = {
    "private/source_records.jsonl": (SOURCE_RECORD_SCHEMA, SourceRecordRefV1),
    "private/captures.jsonl": (CAPTURE_SCHEMA, NormalizedCaptureV3),
    "private/request_boundaries.jsonl": (REQUEST_BOUNDARY_SCHEMA, RequestBoundaryV1),
    "private/event_occurrences.jsonl": (EVENT_SCHEMA, EventOccurrenceV3),
    "private/action_batches.jsonl": (ACTION_BATCH_SCHEMA, ActionBatchV2),
    "private/tool_pairings.jsonl": (TOOL_PAIRING_SCHEMA, ToolPairingRecordV3),
    "private/tool_catalogs.jsonl": (TOOL_CATALOG_SCHEMA, ToolCatalogV2),
    "private/capture_quality.jsonl": (CAPTURE_QUALITY_SCHEMA, CaptureQualityV3),
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
        "git_provenance",
        "git_provenance_verified_at_completion",
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
        "source_reports_truncated_capture_count",
        "input_truncation_unknown_capture_count",
        "terminal_text_outcome_capture_count",
        "terminal_tool_call_pending_capture_count",
        "terminal_empty_outcome_capture_count",
        "terminal_invalid_capture_count",
        "processing_complete_count",
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
    "QUARANTINED": "processing_quarantined_count",
}
_PAIRING_ORDER = tuple(status.value for status in ToolPairingStatus)
_BOUNDARY_STATUSES = frozenset(status.value for status in BoundaryStatus)
_SCHEMA_STATUSES = frozenset(status.value for status in ToolSchemaStatus)
_TERMINAL_STATUSES = frozenset(status.value for status in TerminalStatus)
_PRIVACY_STATUSES = frozenset(status.value for status in PrivacyStatus)
_COMPACTION_STATUSES = frozenset(status.value for status in CompactionStatus)
_PROCESSING_STATUSES = frozenset(status.value for status in ProcessingStatus)


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


@dataclass(frozen=True, slots=True)
class _Capture:
    source_id: str
    source_capture_id: str
    source_request_count: int
    representation: str
    boundary_ids: tuple[str, ...]
    catalog_id: str
    has_compaction: bool
    compaction_count: int
    has_compaction_hashes: bool
    message_count: int
    input_truncation_status: str


@dataclass(frozen=True, slots=True)
class _Boundary:
    capture_id: str
    terminal_event_id: str
    source_request_id: str
    ordinal: int | None
    owned_message_start_index: int | None
    terminal_message_index: int | None
    terminal_prefix_depth: int | None


@dataclass(frozen=True, slots=True)
class _Event:
    capture_id: str
    kind: str
    scope: str
    boundary_id: str | None
    sequence_number: int | None
    message_index: int | None
    sub_index: int | None
    tool_call_id: str | None
    tool_name: str | None
    arguments_valid: bool | None
    assistant_event_id: str | None
    assistant_content_nonempty: bool | None


@dataclass(frozen=True, slots=True)
class _PairingFacts:
    call_event_ids: tuple[str, ...]
    result_event_ids: tuple[str, ...]
    matched_call_event_id: str | None
    matched_result_event_id: str | None
    statuses: tuple[str, ...]


@dataclass(slots=True)
class _State:
    counts: Counter[str]
    sources: dict[str, str]
    source_parse_errors: dict[str, dict[str, Any] | None]
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
    call_event_ids_by_group: dict[tuple[str, str], list[str]]
    result_event_ids_by_group: dict[tuple[str, str], list[str]]
    pairing_facts: dict[tuple[str, str], _PairingFacts]
    pairing_group_order: tuple[tuple[str, str], ...]
    boundary_valid_by_capture: dict[str, bool]
    schema_status_by_capture: dict[str, str]
    terminal_status_by_capture: dict[str, str]

    @classmethod
    def create(cls) -> _State:
        return cls(
            counts=Counter({key: 0 for key in _COUNT_KEYS}),
            sources={},
            source_parse_errors={},
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
            call_event_ids_by_group=defaultdict(list),
            result_event_ids_by_group=defaultdict(list),
            pairing_facts={},
            pairing_group_order=(),
            boundary_valid_by_capture={},
            schema_status_by_capture={},
            terminal_status_by_capture={},
        )


def validate_compiled_run(
    run_path: str | Path,
) -> ValidationResult:
    """流式校验一个 M1 run；只保留外键索引，不保留 event payload。"""

    root = Path(run_path)
    issues = IssueCollector()
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
    _finish_counts(state, issues)
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
    issues: IssueCollector,
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
            "manifest 文件集合与当前 M1 契约不一致",
        )
    return entries


def _check_artifact_bytes(
    root: Path,
    entries: Mapping[str, Mapping[str, Any]],
    issues: IssueCollector,
) -> int:
    checked = 0
    for relative, entry in sorted(entries.items()):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            issues.add("ARTIFACT_NOT_REGULAR_FILE", relative, "artifact 不存在或不是普通文件")
            continue
        digest = hashlib.sha256()
        size = lines = 0
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                lines += chunk.count(b"\n")
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
    return checked


def _check_inventory(root: Path, issues: IssueCollector) -> None:
    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if observed != _ALL_FILES:
        issues.add("RUN_FILE_SET_MISMATCH", "run", "run 文件集合与当前 M1 契约不一致")


def _check_public_report(report: dict[str, Any] | None, issues: IssueCollector) -> None:
    if not _check_contract(
        report,
        AttritionReportV2,
        ATTRITION_REPORT_SCHEMA,
        "reports/attrition_report.json",
        issues,
    ):
        return
    _check_dataset_id(
        report.get("dataset_id"),
        "reports/attrition_report.json/dataset_id",
        issues,
    )
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


def _check_dataset_id(value: Any, location: str, issues: IssueCollector) -> None:
    """复用冻结 slug 契约，错误不得回显不可信标识。"""

    try:
        validate_dataset_id(value)
    except ValueError:
        issues.add("DATASET_ID_INVALID", location, "dataset_id 不满足安全 slug 契约")


def _check_receipt(
    root: Path,
    receipt: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
    issues: IssueCollector,
) -> None:
    if not isinstance(receipt, dict):
        return
    if frozenset(receipt) != _RECEIPT_FIELDS:
        issues.add("RUN_RECEIPT_SCHEMA_MISMATCH", "run_receipt.json", "运行回执字段不匹配 v2")
    if receipt.get("schema_version") != RUN_RECEIPT_SCHEMA:
        issues.add("RUN_RECEIPT_SCHEMA_INVALID", "run_receipt.json", "schema_version 不匹配")
    _check_git_provenance(receipt.get("git_provenance"), issues)
    if receipt.get("git_provenance_verified_at_completion") is not True:
        issues.add(
            "RUN_RECEIPT_GIT_PROVENANCE_UNVERIFIED",
            "run_receipt.json/git_provenance_verified_at_completion",
            "正式 run 必须确认 Git 来源在完成时仍一致",
        )
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


def _check_git_provenance(value: Any, issues: IssueCollector) -> None:
    location = "run_receipt.json/git_provenance"
    if not isinstance(value, dict) or set(value) != {"available", "commit", "tree", "dirty"}:
        issues.add("RUN_RECEIPT_GIT_PROVENANCE_INVALID", location, "git_provenance 字段不闭合")
        return
    available = value.get("available")
    commit = value.get("commit")
    tree = value.get("tree")
    dirty = value.get("dirty")
    if available is True:
        if (
            not _is_git_object_id(commit)
            or not _is_git_object_id(tree)
            or len(commit) != len(tree)
            or not isinstance(dirty, bool)
        ):
            issues.add(
                "RUN_RECEIPT_GIT_PROVENANCE_INVALID",
                location,
                "available 来源必须携带同算法 commit/tree 与布尔 dirty",
            )
    elif available is False:
        if commit is not None or tree is not None or dirty is not None:
            issues.add(
                "RUN_RECEIPT_GIT_PROVENANCE_INVALID",
                location,
                "unavailable 来源必须显式使用 null",
            )
    else:
        issues.add("RUN_RECEIPT_GIT_PROVENANCE_INVALID", location, "available 必须是布尔值")


def _check_run_identity(
    root: Path,
    source: dict[str, Any] | None,
    artifact: dict[str, Any] | None,
    report: dict[str, Any] | None,
    issues: IssueCollector,
) -> None:
    if not isinstance(source, dict) or not isinstance(artifact, dict):
        return
    _check_dataset_id(source.get("dataset_id"), "source_manifest.json/dataset_id", issues)
    _check_dataset_id(
        artifact.get("dataset_id"),
        "artifact_manifest.json/dataset_id",
        issues,
    )
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
    issues: IssueCollector,
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
        parse_error = record.get("parse_error")
        if (status == "PARSED" and parse_error is not None) or (
            status == "QUARANTINED"
            and (not isinstance(parse_error, dict) or not isinstance(parse_error.get("code"), str))
        ):
            issues.add(
                "SOURCE_PARSE_ERROR_SEMANTICS_MISMATCH",
                location,
                "ingestion_status 与 parse_error 形态不一致",
            )
        state.sources[source_id] = str(status)
        state.source_parse_errors[source_id] = (
            parse_error if isinstance(parse_error, dict) else None
        )
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


def _read_captures(root: Path, state: _State, issues: IssueCollector) -> None:
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
        elif state.sources[source_id] != "PARSED":
            issues.add(
                "CAPTURE_SOURCE_STATUS_INVALID",
                location,
                "NormalizedCapture 只能引用 PARSED source",
            )
        if not isinstance(boundary_ids, list) or not all(
            isinstance(item, str) for item in boundary_ids
        ):
            issues.add("CAPTURE_BOUNDARY_IDS_INVALID", location, "request_boundary_ids 非法")
            boundary_ids = []
        if not isinstance(catalog_id, str):
            issues.add("CAPTURE_CATALOG_ID_INVALID", location, "tool_catalog_id 非法")
            catalog_id = ""
        raw_has_compaction = record.get("has_compaction")
        if not isinstance(raw_has_compaction, bool):
            issues.add("CAPTURE_COMPACTION_FACTS_INVALID", location, "has_compaction 必须是布尔值")
        has_compaction = raw_has_compaction is True
        compaction_count = record.get("compaction_count")
        if not _is_int(compaction_count) or compaction_count < 0:
            issues.add("CAPTURE_COMPACTION_FACTS_INVALID", location, "compaction_count 非法")
            compaction_count = 0
        compaction_hashes = record.get("compaction_hashes")
        if not isinstance(compaction_hashes, list) or not all(
            _is_sha256(item) for item in compaction_hashes
        ):
            issues.add(
                "CAPTURE_COMPACTION_HASHES_INVALID",
                location,
                "compaction_hashes 必须是 SHA-256 数组",
            )
            compaction_hashes = []
        message_count = record.get("message_count")
        if not _is_int(message_count) or message_count <= 0:
            issues.add("CAPTURE_MESSAGE_COUNT_INVALID", location, "message_count 必须是正整数")
            message_count = 0
        input_truncation_status = record.get("input_truncation_status")
        if input_truncation_status not in {status.value for status in InputTruncationStatus}:
            issues.add(
                "CAPTURE_INPUT_TRUNCATION_STATUS_INVALID",
                location,
                "input_truncation_status 不属于冻结三态",
            )
            input_truncation_status = InputTruncationStatus.UNKNOWN
        source_capture_id = record.get("source_capture_id")
        source_request_count = record.get("source_request_count")
        representation = record.get("representation")
        if not isinstance(source_capture_id, str) or not source_capture_id:
            issues.add("CAPTURE_SOURCE_IDENTITY_INVALID", location, "source_capture_id 非法")
            source_capture_id = ""
        if not _is_int(source_request_count) or source_request_count <= 0:
            issues.add("CAPTURE_SOURCE_IDENTITY_INVALID", location, "source_request_count 非法")
            source_request_count = 0
        if representation != "restored_long":
            issues.add(
                "CAPTURE_REPRESENTATION_INVALID",
                location,
                "representation 必须为 restored_long",
            )
            representation = str(representation)
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
            source_capture_id=source_capture_id,
            source_request_count=source_request_count,
            representation=representation,
            boundary_ids=tuple(boundary_ids),
            catalog_id=catalog_id,
            has_compaction=has_compaction,
            compaction_count=compaction_count,
            has_compaction_hashes=bool(compaction_hashes),
            message_count=message_count,
            input_truncation_status=str(input_truncation_status),
        )
        state.counts["normalized_capture_count"] += 1
        state.counts["compaction_capture_count"] += (
            has_compaction or compaction_count > 0 or bool(compaction_hashes)
        )
        state.counts["source_reports_truncated_capture_count"] += (
            input_truncation_status == InputTruncationStatus.SOURCE_REPORTS_TRUNCATED
        )
        state.counts["input_truncation_unknown_capture_count"] += (
            input_truncation_status == InputTruncationStatus.UNKNOWN
        )


def _read_boundaries(root: Path, state: _State, issues: IssueCollector) -> None:
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
        ordinal = record.get("boundary_ordinal")
        owned_start = record.get("owned_message_start_index")
        terminal_index = record.get("terminal_message_index")
        terminal_depth = record.get("terminal_prefix_depth")
        state.boundaries[boundary_id] = _Boundary(
            capture_id=capture_id,
            terminal_event_id=terminal_id,
            source_request_id=request_id,
            ordinal=ordinal if _is_int(ordinal) else None,
            owned_message_start_index=owned_start if _is_int(owned_start) else None,
            terminal_message_index=terminal_index if _is_int(terminal_index) else None,
            terminal_prefix_depth=terminal_depth if _is_int(terminal_depth) else None,
        )
        if _is_int(ordinal) and _is_int(terminal_depth):
            expected_id = stable_id(
                "request-boundary-v1",
                {
                    "capture_occurrence_id": capture_id,
                    "boundary_ordinal": ordinal,
                    "source_request_id": request_id,
                    "terminal_prefix_depth": terminal_depth,
                },
            )
            _check_stable_id(boundary_id, expected_id, location, issues)
        else:
            issues.add("BOUNDARY_WINDOW_SEMANTICS_MISMATCH", location, "边界窗口索引必须是整数")
        state.boundary_ids_by_capture[capture_id].append(boundary_id)
        state.unique_request_ids.add(request_id)
        state.counts["request_boundary_count"] += 1
    for capture_id, capture in state.captures.items():
        if tuple(state.boundary_ids_by_capture[capture_id]) != capture.boundary_ids:
            issues.add("CAPTURE_BOUNDARY_EDGE_MISMATCH", capture_id, "capture/boundary 外键不闭合")


def _read_events(root: Path, state: _State, issues: IssueCollector) -> None:
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
        if not _is_int(sequence) or sequence != next_sequence[str(capture_id)]:
            issues.add("EVENT_SEQUENCE_INVALID", location, "capture 内 sequence_number 不连续")
        next_sequence[str(capture_id)] += 1
        message_index = record.get("message_index")
        sub_index = record.get("sub_index")
        if not _is_int(message_index) or message_index < 0:
            issues.add("EVENT_MESSAGE_INDEX_INVALID", location, "message_index 必须是非负整数")
        if (
            capture is not None
            and _is_int(message_index)
            and message_index >= capture.message_count
        ):
            issues.add("EVENT_MESSAGE_INDEX_INVALID", location, "message_index 越出 capture")
        if kind == "TOOL_CALL":
            if not _is_int(sub_index) or sub_index < 0:
                issues.add("EVENT_SUB_INDEX_INVALID", location, "tool call sub_index 非法")
        elif sub_index is not None:
            issues.add("EVENT_SUB_INDEX_INVALID", location, "非 tool call 的 sub_index 必须为 null")
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
        try:
            typed_payload = read_event_payload(kind, payload)
        except EventPayloadReadError as exc:
            issues.add(
                "EVENT_PAYLOAD_INVALID",
                f"{location}{exc.path}",
                f"payload 不满足冻结契约：{exc.code}",
            )
            continue
        assert isinstance(payload, dict)
        try:
            visible = _visible_event_payload(kind, payload)
        except PrivacyTransformError:
            issues.add(
                "EVENT_VISIBLE_PROJECTION_INVALID",
                location,
                "payload 超出可见投影的冻结深度",
            )
            continue
        encoded = canonical_json_bytes(visible)
        if record.get("visible_payload_envelope_utf8_byte_length") != len(encoded):
            issues.add("EVENT_VISIBLE_LENGTH_MISMATCH", location, "visible payload 长度不匹配")
        if record.get("visible_payload_sha256") != sha256_bytes(encoded):
            issues.add("EVENT_VISIBLE_HASH_MISMATCH", location, "visible payload hash 不匹配")
        if record.get("integrity_status") != "COMPLETE":
            issues.add(
                "EVENT_INTEGRITY_STATUS_INVALID",
                location,
                "integrity_status 必须为 COMPLETE",
            )
        if isinstance(typed_payload, AssistantMessagePayload):
            reasoning = typed_payload.reasoning_content
            if reasoning.present and reasoning.source_json_pointer != (
                f"/messages/{message_index}/reasoning_content"
            ):
                issues.add("REASONING_POINTER_MISMATCH", location, "reasoning pointer 不匹配")
        boundary_id = record.get("request_boundary_id")
        if scope == "PRE_FIRST_OBSERVED_TERMINAL" and boundary_id is not None:
            issues.add("EVENT_PREFIX_BOUNDARY_INVALID", location, "prefix event 不应绑定 boundary")
        if scope == "OBSERVED_REQUEST_WINDOW":
            boundary = state.boundaries.get(boundary_id) if isinstance(boundary_id, str) else None
            if boundary is None or boundary.capture_id != capture_id:
                issues.add("EVENT_BOUNDARY_NOT_FOUND", location, "event/boundary 外键不闭合")
        tool_call_id = assistant_id = tool_name = None
        arguments_valid: bool | None = None
        assistant_content_nonempty: bool | None = None
        if isinstance(typed_payload, ToolCallPayload):
            tool_call_id = typed_payload.tool_call_id
            assistant_id = typed_payload.assistant_event_id
            tool_name = typed_payload.function.name
            arguments = typed_payload.function.arguments
            if (
                arguments.source_message_index != message_index
                or arguments.source_sub_index != sub_index
            ):
                issues.add(
                    "TOOL_ARGUMENT_POINTER_MISMATCH",
                    location,
                    "arguments 来源指针与 tool call 事件位置不一致",
                )
            arguments_valid = isinstance(arguments, JsonObjectArguments) or (
                isinstance(arguments, JsonValueArguments) and isinstance(arguments.value, dict)
            )
            state.call_count_by_capture[str(capture_id)] += 1
        elif isinstance(typed_payload, ToolResultPayload):
            tool_call_id = typed_payload.tool_call_id
            tool_name = typed_payload.tool_name
            state.result_count_by_capture[str(capture_id)] += 1
        elif isinstance(typed_payload, AssistantMessagePayload):
            assistant_content_nonempty = _content_nonempty(typed_payload.content)
        state.events[event_id] = _Event(
            capture_id=str(capture_id),
            kind=kind,
            scope=scope,
            boundary_id=boundary_id if isinstance(boundary_id, str) else None,
            sequence_number=sequence if _is_int(sequence) else None,
            message_index=message_index if _is_int(message_index) else None,
            sub_index=sub_index if _is_int(sub_index) else None,
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
            tool_name=tool_name,
            arguments_valid=arguments_valid,
            assistant_event_id=assistant_id if isinstance(assistant_id, str) else None,
            assistant_content_nonempty=assistant_content_nonempty,
        )
        if isinstance(tool_call_id, str):
            group = (str(capture_id), tool_call_id)
            target = (
                state.call_event_ids_by_group
                if kind == "TOOL_CALL"
                else state.result_event_ids_by_group
            )
            target[group].append(event_id)
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
            or assistant.message_index != event.message_index
            or assistant.boundary_id != event.boundary_id
        ):
            issues.add("TOOL_CALL_ASSISTANT_NOT_FOUND", event_id, "assistant 外键不闭合")

    _check_event_structure(state, issues)
    _derive_pairing_facts(state)
    _derive_terminal_statuses(state)


def _check_event_structure(state: _State, issues: IssueCollector) -> None:
    """检查 message 主事件覆盖，以及同一 assistant 内 call sub-index 连续性。"""

    main_events: dict[str, Counter[int]] = defaultdict(Counter)
    calls_by_assistant: dict[str, list[_Event]] = defaultdict(list)
    for event in state.events.values():
        if event.message_index is None:
            continue
        if event.kind == "TOOL_CALL":
            if event.assistant_event_id is not None:
                calls_by_assistant[event.assistant_event_id].append(event)
        else:
            main_events[event.capture_id][event.message_index] += 1
    for capture_id, capture in state.captures.items():
        observed = main_events[capture_id]
        if set(observed) != set(range(capture.message_count)) or any(
            count != 1 for count in observed.values()
        ):
            issues.add(
                "EVENT_MESSAGE_COVERAGE_MISMATCH",
                capture_id,
                "每个 message_index 必须恰有一个非 TOOL_CALL 主事件",
            )
    for assistant_id, calls in calls_by_assistant.items():
        ordered = sorted(
            calls,
            key=lambda event: event.sequence_number if event.sequence_number is not None else -1,
        )
        if [event.sub_index for event in ordered] != list(range(len(ordered))):
            issues.add(
                "EVENT_TOOL_CALL_SUB_INDEX_MISMATCH",
                assistant_id,
                "同一 assistant 内 tool call sub_index 必须从零连续",
            )


def _visible_event_payload(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """复用 compiler 的递归规则，移除任意深度的 reasoning 摘要。"""

    visible = visible_value_without_reasoning(payload)
    if not isinstance(visible, dict):
        return {}
    # compiler 不发布只含 reasoning 的扩展容器；从完整 payload 重建时同步移除。
    if visible.get("extensions") == {}:
        visible.pop("extensions")
    if kind == "TOOL_CALL" and isinstance(visible.get("function"), dict):
        function = visible["function"]
        if function.get("extensions") == {}:
            function.pop("extensions")
    return visible


def _content_nonempty(content: TextContent | ContentBlocks) -> bool:
    """从 typed content 恢复 compiler 的终态空值语义。"""

    if isinstance(content, TextContent):
        if isinstance(content.value, str):
            return bool(content.value)
        # typed reader 只允许两类包含 Data URL 的严格 envelope；它们在语义上
        # 必然代表非空原文，不能依赖可被重签的长度摘要来判空。
        return True
    return content.block_count > 0


def _derive_pairing_facts(state: _State) -> None:
    """从 tool occurrence 重建 pairing；报告与 quality 均不读取自报 statuses。"""

    ordered: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for event in state.events.values():
        if event.kind not in {"TOOL_CALL", "TOOL_RESULT"} or event.tool_call_id is None:
            continue
        group = (event.capture_id, event.tool_call_id)
        if group not in seen:
            seen.add(group)
            ordered.append(group)
    ordered_groups = tuple(ordered)
    state.pairing_group_order = ordered_groups
    for group in ordered_groups:
        call_ids = tuple(state.call_event_ids_by_group.get(group, []))
        result_ids = tuple(state.result_event_ids_by_group.get(group, []))
        calls = [state.events[event_id] for event_id in call_ids]
        results = [state.events[event_id] for event_id in result_ids]
        observed: set[str] = set()
        if len(calls) > 1:
            observed.add(ToolPairingStatus.DUPLICATE_CALL_ID)
        if len(results) > 1:
            observed.add(ToolPairingStatus.DUPLICATE_RESULT)
        if calls and not results:
            observed.add(ToolPairingStatus.RESULT_NOT_OBSERVED)
        if results and not calls:
            observed.add(ToolPairingStatus.ORPHAN_RESULT)
        if calls and results:
            names = {event.tool_name for event in (*calls, *results)}
            if len(names) > 1:
                observed.add(ToolPairingStatus.NAME_MISMATCH)
            first_call_sequence = calls[0].sequence_number
            first_result_sequence = results[0].sequence_number
            if (
                first_call_sequence is not None
                and first_result_sequence is not None
                and first_result_sequence < first_call_sequence
            ):
                observed.add(ToolPairingStatus.RESULT_BEFORE_CALL)
        if any(event.arguments_valid is False for event in calls):
            observed.add(ToolPairingStatus.INVALID_CALL_ARGUMENTS)
        # 与 compiler 共享同一判定规则（contracts.is_strict_one_to_one_match）；输入事实仍由本模块
        # 从已发布 event 独立重建，独立性体现在输入重建而非规则复刻。
        has_strict_match = is_strict_one_to_one_match(
            [
                PairingEndpointFacts(event.tool_name, event.sequence_number, event.arguments_valid)
                for event in calls
            ],
            [
                PairingEndpointFacts(event.tool_name, event.sequence_number, event.arguments_valid)
                for event in results
            ],
        )
        if has_strict_match:
            observed.add(ToolPairingStatus.MATCHED_ONE_TO_ONE)
        statuses = tuple(status for status in _PAIRING_ORDER if status in observed)
        facts = _PairingFacts(
            call_event_ids=call_ids,
            result_event_ids=result_ids,
            matched_call_event_id=call_ids[0] if has_strict_match else None,
            matched_result_event_id=result_ids[0] if has_strict_match else None,
            statuses=statuses,
        )
        state.pairing_facts[group] = facts
        capture_id = group[0]
        state.pair_statuses_by_capture[capture_id].update(statuses)
        for status in statuses:
            state.counts[_PAIRING_COUNT_KEYS[status]] += 1
        if ToolPairingStatus.RESULT_NOT_OBSERVED in statuses:
            state.missing_count_by_capture[capture_id] += 1
        if ToolPairingStatus.DUPLICATE_RESULT in statuses:
            extra = len(result_ids) - 1
            state.duplicate_result_count_by_capture[capture_id] += extra
            state.counts["extra_result_occurrence_count"] += extra
    state.counts["tool_pairing_record_count"] = len(ordered_groups)


def _derive_terminal_statuses(state: _State) -> None:
    """从最后一个 boundary 的 terminal event 与同消息 tool call 恢复终态。"""

    for capture_id, capture in state.captures.items():
        if not capture.boundary_ids:
            state.terminal_status_by_capture[capture_id] = TerminalStatus.INVALID
            continue
        boundary = state.boundaries.get(capture.boundary_ids[-1])
        terminal = state.events.get(boundary.terminal_event_id) if boundary is not None else None
        if terminal is None or terminal.kind != "ASSISTANT_MESSAGE":
            state.terminal_status_by_capture[capture_id] = TerminalStatus.INVALID
            continue
        has_pending_call = any(
            event.kind == "TOOL_CALL"
            and event.capture_id == capture_id
            and event.message_index == terminal.message_index
            for event in state.events.values()
        )
        if has_pending_call:
            status = TerminalStatus.TOOL_CALL_PENDING
        elif terminal.assistant_content_nonempty is True:
            status = TerminalStatus.TEXT_OUTCOME
        elif terminal.assistant_content_nonempty is False:
            status = TerminalStatus.EMPTY_OUTCOME
        else:
            status = TerminalStatus.INVALID
        state.terminal_status_by_capture[capture_id] = status


def _check_boundary_edges(state: _State, issues: IssueCollector) -> None:
    events_by_capture: dict[str, list[_Event]] = defaultdict(list)
    for event in state.events.values():
        events_by_capture[event.capture_id].append(event)

    for capture_id, capture in state.captures.items():
        valid = bool(capture.boundary_ids)
        if capture.source_request_count != len(capture.boundary_ids):
            issues.add(
                "CAPTURE_REQUEST_COUNT_MISMATCH",
                capture_id,
                "source_request_count 与 boundary 数不一致",
            )
            valid = False
        depths: list[int] = []
        for expected_ordinal, boundary_id in enumerate(capture.boundary_ids):
            boundary = state.boundaries.get(boundary_id)
            if boundary is None or boundary.capture_id != capture_id:
                valid = False
                continue
            depth = boundary.terminal_prefix_depth
            terminal_index = boundary.terminal_message_index
            expected_owned_start = (
                terminal_index if expected_ordinal == 0 else (depths[-1] if depths else None)
            )
            window_valid = (
                boundary.ordinal == expected_ordinal
                and depth is not None
                and depth > 0
                and depth <= capture.message_count
                and terminal_index == depth - 1
                and boundary.owned_message_start_index == expected_owned_start
                and (not depths or depth > depths[-1])
            )
            if not window_valid:
                issues.add(
                    "BOUNDARY_WINDOW_SEMANTICS_MISMATCH",
                    boundary_id,
                    "边界序号、窗口或 terminal depth 与 capture 事实不一致",
                )
                valid = False
            if depth is not None:
                depths.append(depth)

            event = state.events.get(boundary.terminal_event_id)
            if (
                event is None
                or event.kind != "ASSISTANT_MESSAGE"
                or event.capture_id != capture_id
                or event.boundary_id != boundary_id
                or event.message_index != terminal_index
            ):
                issues.add(
                    "BOUNDARY_TERMINAL_EDGE_MISMATCH",
                    boundary_id,
                    "terminal event 外键或消息位置不闭合",
                )
                valid = False

        if not depths or depths[-1] != capture.message_count:
            issues.add(
                "BOUNDARY_WINDOW_SEMANTICS_MISMATCH",
                capture_id,
                "最后一个 terminal depth 必须等于 message_count",
            )
            valid = False
        final_boundary = (
            state.boundaries.get(capture.boundary_ids[-1]) if capture.boundary_ids else None
        )
        if final_boundary is None or final_boundary.source_request_id != capture.source_capture_id:
            issues.add(
                "CAPTURE_FINAL_REQUEST_ID_MISMATCH",
                capture_id,
                "source_capture_id 必须等于最后 boundary 的 source_request_id",
            )
            valid = False

        if valid and depths and len(depths) == len(capture.boundary_ids):
            prefix_end = depths[0] - 1
            for event in events_by_capture[capture_id]:
                message_index = event.message_index
                if message_index is None:
                    valid = False
                    continue
                if message_index < prefix_end:
                    expected_scope = "PRE_FIRST_OBSERVED_TERMINAL"
                    expected_boundary_id = None
                else:
                    ordinal = bisect_right(depths, message_index)
                    if ordinal >= len(capture.boundary_ids):
                        ordinal = len(capture.boundary_ids) - 1
                    expected_scope = "OBSERVED_REQUEST_WINDOW"
                    expected_boundary_id = capture.boundary_ids[ordinal]
                if event.scope != expected_scope or event.boundary_id != expected_boundary_id:
                    issues.add(
                        "EVENT_BOUNDARY_SEMANTICS_MISMATCH",
                        capture_id,
                        "event scope/boundary 与重算窗口不一致",
                    )
                    valid = False
        state.boundary_valid_by_capture[capture_id] = valid


def _read_action_batches(root: Path, state: _State, issues: IssueCollector) -> None:
    covered_calls: Counter[str] = Counter()
    assistants: set[str] = set()
    expected_calls_by_assistant: dict[str, list[str]] = defaultdict(list)
    for event_id, event in state.events.items():
        if event.kind == "TOOL_CALL" and event.assistant_event_id is not None:
            expected_calls_by_assistant[event.assistant_event_id].append(event_id)
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
        if assistant is not None and boundary_id != assistant.boundary_id:
            issues.add(
                "ACTION_BATCH_SEMANTICS_MISMATCH",
                location,
                "batch boundary 必须等于 assistant boundary",
            )
        if record.get("execution_semantics") != "UNKNOWN":
            issues.add(
                "ACTION_EXECUTION_SEMANTICS_MISMATCH",
                location,
                "execution_semantics 必须固定为 UNKNOWN",
            )
        call_ids = record.get("tool_call_event_ids")
        if not isinstance(call_ids, list) or not all(isinstance(item, str) for item in call_ids):
            issues.add("ACTION_TOOL_CALLS_INVALID", location, "tool_call_event_ids 必须是数组")
            call_ids = []
        if isinstance(assistant_id, str) and call_ids != expected_calls_by_assistant.get(
            assistant_id,
            [],
        ):
            issues.add(
                "ACTION_BATCH_SEMANTICS_MISMATCH",
                location,
                "tool_call_event_ids 必须完整、有序覆盖该 assistant 的调用",
            )
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
    if assistants != set(expected_calls_by_assistant):
        issues.add(
            "ACTION_BATCH_COVERAGE_MISMATCH",
            "private/action_batches.jsonl",
            "ActionBatch 未一一覆盖包含 tool call 的 assistant",
        )


def _read_pairings(root: Path, state: _State, issues: IssueCollector) -> None:
    covered: Counter[str] = Counter()
    groups: set[tuple[str, str]] = set()
    observed_group_order: list[tuple[str, str]] = []
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
        observed_group_order.append(group)
        facts = state.pairing_facts.get(group)
        if facts is None:
            issues.add("PAIRING_GROUP_SEMANTICS_MISMATCH", location, "pairing group 没有事件事实")
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
            call_events = []
            result_events = []
        else:
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
        if facts is not None:
            if call_events != list(facts.call_event_ids) or result_events != list(
                facts.result_event_ids
            ):
                issues.add(
                    "PAIRING_OCCURRENCE_SEMANTICS_MISMATCH",
                    location,
                    "call/result occurrence 与事件分组不一致",
                )
            if record.get("matched_call_event_id") != facts.matched_call_event_id:
                issues.add(
                    "PAIRING_MATCHED_CALL_SEMANTICS_MISMATCH",
                    location,
                    "matched call 必须等于独立重算值",
                )
            if record.get("matched_result_event_id") != facts.matched_result_event_id:
                issues.add(
                    "PAIRING_MATCHED_RESULT_SEMANTICS_MISMATCH",
                    location,
                    "matched result 必须等于独立重算值",
                )
        statuses = record.get("statuses")
        if (
            not isinstance(statuses, list)
            or not all(isinstance(status, str) for status in statuses)
            or statuses != [status for status in _PAIRING_ORDER if status in statuses]
        ):
            issues.add("PAIRING_STATUSES_INVALID", location, "statuses 非法或顺序不稳定")
            statuses = []
        for status in statuses:
            if status not in _PAIRING_COUNT_KEYS:
                issues.add("PAIRING_STATUS_UNKNOWN", location, "出现未知 pairing status")
        if facts is not None and statuses != list(facts.statuses):
            issues.add(
                "PAIRING_STATUS_SEMANTICS_MISMATCH",
                location,
                "statuses 与 tool occurrence 重算结果不一致",
            )
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
    if groups != set(state.pairing_facts):
        issues.add(
            "PAIRING_GROUP_COVERAGE_MISMATCH",
            "private/tool_pairings.jsonl",
            "pairing group 未一一覆盖事件中的 tool_call_id",
        )
    if tuple(observed_group_order) != state.pairing_group_order:
        issues.add(
            "PAIRING_GROUP_ORDER_MISMATCH",
            "private/tool_pairings.jsonl",
            "pairing group 顺序与首次事件出现顺序不一致",
        )


def _check_pairing_events(
    event_ids: list[Any],
    expected_kind: str,
    capture_id: Any,
    call_id: Any,
    state: _State,
    covered: Counter[str],
    location: str,
    issues: IssueCollector,
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


def _read_catalogs(root: Path, state: _State, issues: IssueCollector) -> None:
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
        if str(capture_id) in by_capture:
            issues.add("CATALOG_CAPTURE_DUPLICATE", location, "同一 capture 存在多个 catalog")
        by_capture[str(capture_id)] = catalog_id
        definitions = record.get("definitions")
        inferred_names = record.get("inferred_tool_names")
        conflict = record.get("definition_conflict")
        catalog_input_valid = record.get("catalog_input_valid")
        schema_status = _derive_schema_status(
            definitions,
            inferred_names,
            conflict,
            catalog_input_valid,
        )
        state.schema_status_by_capture[str(capture_id)] = schema_status
        if (
            not isinstance(definitions, list)
            or not isinstance(inferred_names, list)
            or not isinstance(conflict, bool)
            or not isinstance(catalog_input_valid, bool)
        ):
            issues.add("CATALOG_CONTENT_INVALID", location, "definitions/inferred names 必须是数组")
            continue
        try:
            visible_definitions = visible_value_without_reasoning(definitions)
        except PrivacyTransformError:
            issues.add(
                "CATALOG_VISIBLE_PROJECTION_INVALID",
                location,
                "definitions 超出可见投影的冻结深度",
            )
            continue
        catalog_sha256 = sha256_bytes(canonical_json_bytes(visible_definitions))
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


def _derive_schema_status(
    definitions: Any,
    inferred_names: Any,
    conflict: Any,
    catalog_input_valid: Any,
) -> str:
    """从 catalog 的可见事实恢复 schema status；损坏结构一律闭合为 INVALID。"""

    valid_inferred = (
        isinstance(inferred_names, list)
        and all(isinstance(item, str) and bool(item.strip()) for item in inferred_names)
        and len(inferred_names) == len(set(inferred_names))
    )
    inferred_set = set(inferred_names) if valid_inferred else set()
    valid_definitions = isinstance(definitions, list) and all(
        _published_definition_name(item, index, inferred_set) is not None
        for index, item in enumerate(definitions)
    )
    # catalog_input_valid 是 normalizer 对原始 annotation 形态的来源证明；validator
    # 可校验其类型与下游组合，但无法脱离原 source 复原“为何 invalid”。
    if (
        catalog_input_valid is not True
        or not valid_inferred
        or not valid_definitions
        or not isinstance(conflict, bool)
    ):
        return ToolSchemaStatus.INVALID
    if inferred_names and conflict:
        return ToolSchemaStatus.INFERRED_AND_CONFLICT
    if inferred_names:
        return ToolSchemaStatus.INFERRED
    if conflict:
        return ToolSchemaStatus.CONFLICT_REPORTED
    return ToolSchemaStatus.CONSISTENT


def _published_definition_name(
    item: Any,
    index: int,
    inferred_names: set[str],
) -> str | None:
    if not isinstance(item, dict) or set(item) != {
        "definition",
        "provenance",
        "source_json_pointer",
    }:
        return None
    definition = item.get("definition")
    if not isinstance(definition, dict) or definition.get("type") != "function":
        return None
    function = definition.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(function.get("parameters"), dict)
        or item.get("source_json_pointer") != f"/tools/{index}"
    ):
        return None
    expected_provenance = "NORMALIZER_INFERRED" if name in inferred_names else "SOURCE_DECLARED"
    return name if item.get("provenance") == expected_provenance else None


def _read_quality(root: Path, state: _State, issues: IssueCollector) -> None:
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
        _check_quality_enums(record, location, issues)
        if capture_id is None:
            _check_quarantined_quality(record, str(source_id), state, location, issues)
        elif not isinstance(capture_id, str):
            issues.add("QUALITY_CAPTURE_EDGE_MISMATCH", location, "capture_occurrence_id 非法")
        else:
            _check_capture_quality(record, capture_id, str(source_id), state, location, issues)
            if capture_id in seen_captures:
                issues.add("QUALITY_CAPTURE_EDGE_MISMATCH", location, "capture quality 重复")
            seen_captures.add(capture_id)
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
    issues: IssueCollector,
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
    expected_pairing_statuses = tuple(
        status for status in _PAIRING_ORDER if status in state.pair_statuses_by_capture[capture_id]
    )
    expected_schema_status = state.schema_status_by_capture.get(
        capture_id,
        ToolSchemaStatus.INVALID,
    )
    expected_terminal_status = state.terminal_status_by_capture.get(
        capture_id,
        TerminalStatus.INVALID,
    )
    expected_compaction_status = (
        CompactionStatus.UNLOCALIZED_COMPACTION_EVIDENCE
        if capture.has_compaction or capture.compaction_count > 0 or capture.has_compaction_hashes
        else CompactionStatus.NOT_OBSERVED
    )
    expected_fields = {
        "processing_status": ProcessingStatus.COMPLETE,
        "boundary_status": (
            BoundaryStatus.VALID
            if state.boundary_valid_by_capture.get(capture_id, False)
            else BoundaryStatus.INVALID
        ),
        "tool_pairing_applicable": bool(state.pair_statuses_by_capture[capture_id]),
        "tool_schema_status": expected_schema_status,
        "terminal_status": expected_terminal_status,
        "privacy_status": PrivacyStatus.DERIVATION_POLICY_APPLIED,
        "compaction_status": expected_compaction_status,
    }
    mismatch_codes = {
        "processing_status": "QUALITY_PROCESSING_STATUS_MISMATCH",
        "boundary_status": "QUALITY_BOUNDARY_STATUS_MISMATCH",
        "tool_pairing_applicable": "QUALITY_PAIRING_APPLICABILITY_MISMATCH",
        "tool_schema_status": "QUALITY_TOOL_SCHEMA_STATUS_MISMATCH",
        "terminal_status": "QUALITY_TERMINAL_STATUS_MISMATCH",
        "privacy_status": "QUALITY_PRIVACY_STATUS_MISMATCH",
        "compaction_status": "QUALITY_COMPACTION_STATUS_MISMATCH",
    }
    for key, expected in expected_fields.items():
        if record.get(key) != expected:
            issues.add(mismatch_codes[key], f"{location}/{key}", "字段与发布结构重算值不一致")
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
    if (
        not isinstance(raw_statuses, list)
        or not all(isinstance(status, str) for status in raw_statuses)
        or raw_statuses != [status for status in _PAIRING_ORDER if status in raw_statuses]
    ):
        issues.add("QUALITY_PAIRING_STATUSES_INVALID", location, "tool_pairing_statuses 非法")
        raw_statuses = []
    if tuple(raw_statuses) != expected_pairing_statuses:
        issues.add("QUALITY_PAIRING_STATUS_MISMATCH", location, "pairing 状态聚合不一致")
    _check_capture_reason_codes(
        record.get("reason_codes"),
        expected_pairing_statuses,
        str(expected_schema_status),
        str(expected_terminal_status),
        str(expected_compaction_status),
        capture.input_truncation_status,
        location,
        issues,
    )


def _check_quarantined_quality(
    record: Mapping[str, Any],
    source_id: str,
    state: _State,
    location: str,
    issues: IssueCollector,
) -> None:
    if any(capture.source_id == source_id for capture in state.captures.values()):
        issues.add("QUALITY_QUARANTINE_INVALID", location, "已有 capture 的 source 不得隔离")
    expected = {
        "processing_status": ProcessingStatus.QUARANTINED,
        "boundary_status": BoundaryStatus.INVALID,
        "tool_pairing_applicable": False,
        "tool_pairing_statuses": [],
        "tool_schema_status": ToolSchemaStatus.INVALID,
        "terminal_status": TerminalStatus.INVALID,
        "privacy_status": PrivacyStatus.NOT_EVALUATED,
        "compaction_status": CompactionStatus.NOT_OBSERVED,
        "boundary_count": 0,
        "event_count": 0,
        "action_batch_count": 0,
        "tool_call_count": 0,
        "tool_result_count": 0,
        "missing_result_count": 0,
        "duplicate_result_count": 0,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            issues.add("QUALITY_QUARANTINE_INVALID", f"{location}/{key}", "隔离终态字段非法")
    error = record.get("processing_error")
    reasons = record.get("reason_codes")
    if not isinstance(error, dict):
        issues.add("QUALITY_QUARANTINE_INVALID", location, "隔离终态缺少 processing_error")
        return
    error_code = error.get("code")
    if not isinstance(error_code, str) or reasons != [error_code]:
        issues.add(
            "QUALITY_QUARANTINE_ATTESTATION_INVALID",
            location,
            "source-attested reason_codes 必须绑定 processing_error.code",
        )
    source_error = state.source_parse_errors.get(source_id)
    if state.sources.get(source_id) == "QUARANTINED" and error != source_error:
        issues.add(
            "QUALITY_QUARANTINE_SOURCE_MISMATCH",
            location,
            "解析隔离的 processing_error 必须与 source parse_error 完全一致",
        )
    # PARSED 后发生的适配/编译失败缺少原 source，具体 cause 只能视为
    # source-attested；这里不声称独立重算，只校验 code/reason 自洽。


def _check_quality_enums(
    record: Mapping[str, Any],
    location: str,
    issues: IssueCollector,
) -> None:
    checks = (
        ("processing_status", _PROCESSING_STATUSES, "QUALITY_PROCESSING_STATUS_INVALID"),
        ("boundary_status", _BOUNDARY_STATUSES, "QUALITY_BOUNDARY_STATUS_INVALID"),
        ("tool_schema_status", _SCHEMA_STATUSES, "QUALITY_TOOL_SCHEMA_STATUS_INVALID"),
        ("terminal_status", _TERMINAL_STATUSES, "QUALITY_TERMINAL_STATUS_INVALID"),
        ("privacy_status", _PRIVACY_STATUSES, "QUALITY_PRIVACY_STATUS_INVALID"),
        ("compaction_status", _COMPACTION_STATUSES, "QUALITY_COMPACTION_STATUS_INVALID"),
    )
    for key, allowed, code in checks:
        value = record.get(key)
        if not isinstance(value, str) or value not in allowed:
            issues.add(code, f"{location}/{key}", "quality 枚举值非法")
            # 保留 v1 通用错误码，兼容现有调用方。
            issues.add("QUALITY_ENUM_INVALID", f"{location}/{key}", "quality 枚举值非法")


def _check_capture_reason_codes(
    raw_reasons: Any,
    pairing_statuses: tuple[str, ...],
    schema_status: str,
    terminal_status: str,
    compaction_status: str,
    input_truncation_status: str,
    location: str,
    issues: IssueCollector,
) -> None:
    if not isinstance(raw_reasons, list) or not all(
        isinstance(reason, str) for reason in raw_reasons
    ):
        issues.add("QUALITY_REASON_CODES_INVALID", location, "reason_codes 必须是字符串数组")
        return
    expected = [
        f"TOOL_PAIRING_{status}"
        for status in pairing_statuses
        if status != ToolPairingStatus.MATCHED_ONE_TO_ONE
    ]
    if schema_status != ToolSchemaStatus.CONSISTENT:
        expected.append(f"TOOL_SCHEMA_{schema_status}")
    if terminal_status != TerminalStatus.TEXT_OUTCOME:
        expected.append(f"TERMINAL_{terminal_status}")
    if compaction_status == CompactionStatus.UNLOCALIZED_COMPACTION_EVIDENCE:
        expected.append("UNLOCALIZED_COMPACTION_EVIDENCE")
    if input_truncation_status == InputTruncationStatus.SOURCE_REPORTS_TRUNCATED:
        expected.append("SOURCE_REPORTS_INPUT_TRUNCATED")
    elif input_truncation_status == InputTruncationStatus.UNKNOWN:
        expected.append("INPUT_TRUNCATION_UNKNOWN")
    if raw_reasons != expected:
        issues.add("QUALITY_REASON_CODES_MISMATCH", location, "reason_codes 与可重算事实不一致")


def _finish_counts(state: _State, issues: IssueCollector) -> None:
    state.counts["unique_source_request_count"] = len(state.unique_request_ids)
    state.counts["captures_with_unobserved_results"] = sum(
        count > 0 for count in state.missing_count_by_capture.values()
    )
    for mapping in (_SCHEMA_COUNT_KEYS, _TERMINAL_COUNT_KEYS, _PROCESSING_COUNT_KEYS):
        for count_key in mapping.values():
            state.counts[count_key] = 0
    for capture_id in state.captures:
        schema_status = str(
            state.schema_status_by_capture.get(capture_id, ToolSchemaStatus.INVALID)
        )
        terminal_status = str(
            state.terminal_status_by_capture.get(capture_id, TerminalStatus.INVALID)
        )
        state.counts[_SCHEMA_COUNT_KEYS[schema_status]] += 1
        state.counts[_TERMINAL_COUNT_KEYS[terminal_status]] += 1
    captured_sources = {
        capture.source_id
        for capture in state.captures.values()
        if capture.source_id in state.sources
    }
    state.counts["processing_complete_count"] = len(captured_sources)
    state.counts["processing_quarantined_count"] = len(state.sources) - len(captured_sources)
    _check_count_conservation(state, issues)


def _check_count_conservation(state: _State, issues: IssueCollector) -> None:
    checks = (
        (
            sum(state.counts[key] for key in _EVENT_COUNT_KEYS.values()),
            state.counts["event_occurrence_count"],
            "EVENT_KIND_CONSERVATION_MISMATCH",
        ),
        (
            sum(state.counts[key] for key in _SCOPE_COUNT_KEYS.values()),
            state.counts["event_occurrence_count"],
            "EVENT_SCOPE_CONSERVATION_MISMATCH",
        ),
        (
            sum(state.counts[key] for key in _PROCESSING_COUNT_KEYS.values()),
            state.counts["physical_line_count"],
            "PROCESSING_CONSERVATION_MISMATCH",
        ),
        (
            sum(state.counts[key] for key in _SCHEMA_COUNT_KEYS.values()),
            state.counts["normalized_capture_count"],
            "SCHEMA_CONSERVATION_MISMATCH",
        ),
        (
            sum(state.counts[key] for key in _TERMINAL_COUNT_KEYS.values()),
            state.counts["normalized_capture_count"],
            "TERMINAL_CONSERVATION_MISMATCH",
        ),
    )
    for observed, expected, code in checks:
        if observed != expected:
            issues.add(code, "run", f"守恒失败：observed={observed}，expected={expected}")
    derived_calls = sum(len(facts.call_event_ids) for facts in state.pairing_facts.values())
    derived_results = sum(len(facts.result_event_ids) for facts in state.pairing_facts.values())
    if (
        derived_calls != state.counts["tool_call_event_count"]
        or derived_results != state.counts["tool_result_event_count"]
    ):
        issues.add(
            "TOOL_OCCURRENCE_CONSERVATION_MISMATCH",
            "run",
            "pairing occurrence 未守恒覆盖 tool event",
        )


def _compare_report(
    report: Mapping[str, Any] | None,
    observed: Mapping[str, int],
    issues: IssueCollector,
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


def _iter_jsonl(root: Path, relative: str, issues: IssueCollector) -> Iterable[dict[str, Any]]:
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
            _check_privacy(value, location, issues)
            if _check_contract(value, contract, schema, location, issues):
                yield value


def _read_json(path: Path, issues: IssueCollector) -> dict[str, Any] | None:
    location = path.name if path.parent.name != "reports" else f"reports/{path.name}"
    if not path.is_file():
        issues.add("JSON_FILE_MISSING", location, "文件不存在")
        return None
    raw = path.read_bytes()
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
    _check_privacy(value, location, issues)
    return value


def _check_privacy(value: Any, location: str, issues: IssueCollector) -> None:
    """统一扫描结构化派生产物，不把孤立的 ``;base64,`` 当作 Data URL。"""

    for violation in find_privacy_violations(value):
        code = (
            "BASE64_DATA_URL_OBSERVED"
            if violation.code == "RAW_DATA_URL"
            else "RAW_REASONING_OBSERVED"
        )
        issues.add(code, f"{location}{violation.pointer}", "派生产物违反隐私摘要契约")


def _check_contract(
    value: Any,
    contract: type[Any],
    schema: str | None,
    location: str,
    issues: IssueCollector,
) -> bool:
    if not isinstance(value, dict):
        issues.add("SCHEMA_NOT_OBJECT", location, "记录顶层必须是对象")
        return False
    expected_fields = {field.name for field in fields(contract)}
    if set(value) != expected_fields:
        issues.add("SCHEMA_FIELDS_MISMATCH", location, "字段集合与当前 contract 不一致")
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


def _is_git_object_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _check_stable_id(
    observed: str,
    expected: str,
    location: str,
    issues: IssueCollector,
) -> None:
    if observed != expected:
        issues.add("STABLE_ID_MISMATCH", location, "稳定 ID 与固定身份公式不匹配")
