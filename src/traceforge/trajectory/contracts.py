"""M1A、M1B 的稳定数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any

SOURCE_MANIFEST_SCHEMA = "traceforge.source-manifest.v1"
SOURCE_RECORD_SCHEMA = "traceforge.source-record.v1"
CAPTURE_SCHEMA = "traceforge.normalized-capture.v2"
REQUEST_BOUNDARY_SCHEMA = "traceforge.request-boundary.v1"
EVENT_SCHEMA = "traceforge.event-occurrence.v2"
ACTION_BATCH_SCHEMA = "traceforge.action-batch.v2"
TOOL_PAIRING_SCHEMA = "traceforge.tool-pairing.v3"
TOOL_CATALOG_SCHEMA = "traceforge.tool-catalog.v2"
CAPTURE_QUALITY_SCHEMA = "traceforge.capture-quality.v2"
ARTIFACT_MANIFEST_SCHEMA = "traceforge.artifact-manifest.v1"
ATTRITION_REPORT_SCHEMA = "traceforge.attrition-report.v2"
RUN_RECEIPT_SCHEMA = "traceforge.run-receipt.v2"
COMPILER_CONTRACT_VERSION = "trajectory-compiler-m1ab-v3"


class ProcessingStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    QUARANTINED = "QUARANTINED"


class IngestionStatus(StrEnum):
    PARSED = "PARSED"
    QUARANTINED = "QUARANTINED"


class EventKind(StrEnum):
    SYSTEM = "SYSTEM"
    USER = "USER"
    ASSISTANT_MESSAGE = "ASSISTANT_MESSAGE"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"


class EventScope(StrEnum):
    PRE_FIRST_OBSERVED_TERMINAL = "PRE_FIRST_OBSERVED_TERMINAL"
    OBSERVED_REQUEST_WINDOW = "OBSERVED_REQUEST_WINDOW"


class EventIntegrityStatus(StrEnum):
    COMPLETE = "COMPLETE"


class ToolPairingStatus(StrEnum):
    MATCHED_ONE_TO_ONE = "MATCHED_ONE_TO_ONE"
    RESULT_NOT_OBSERVED = "RESULT_NOT_OBSERVED"
    DUPLICATE_CALL_ID = "DUPLICATE_CALL_ID"
    DUPLICATE_RESULT = "DUPLICATE_RESULT"
    ORPHAN_RESULT = "ORPHAN_RESULT"
    NAME_MISMATCH = "NAME_MISMATCH"
    RESULT_BEFORE_CALL = "RESULT_BEFORE_CALL"
    INVALID_CALL_ARGUMENTS = "INVALID_CALL_ARGUMENTS"


class BoundaryStatus(StrEnum):
    VALID = "VALID"
    INVALID = "INVALID"


class ToolSchemaStatus(StrEnum):
    CONSISTENT = "CONSISTENT"
    INFERRED = "INFERRED"
    CONFLICT_REPORTED = "CONFLICT_REPORTED"
    INFERRED_AND_CONFLICT = "INFERRED_AND_CONFLICT"
    INVALID = "INVALID"


class TerminalStatus(StrEnum):
    TEXT_OUTCOME = "TEXT_OUTCOME"
    TOOL_CALL_PENDING = "TOOL_CALL_PENDING"
    EMPTY_OUTCOME = "EMPTY_OUTCOME"
    INVALID = "INVALID"


class PrivacyStatus(StrEnum):
    DERIVATION_POLICY_APPLIED = "DERIVATION_POLICY_APPLIED"
    NOT_EVALUATED = "NOT_EVALUATED"


class CompactionStatus(StrEnum):
    NOT_OBSERVED = "NOT_OBSERVED"
    UNLOCALIZED_COMPACTION_EVIDENCE = "UNLOCALIZED_COMPACTION_EVIDENCE"


class InputTruncationStatus(StrEnum):
    OBSERVED_TRUNCATED = "OBSERVED_TRUNCATED"
    OBSERVED_NOT_TRUNCATED = "OBSERVED_NOT_TRUNCATED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SerializableContract:
    def to_dict(self) -> dict[str, Any]:
        """只展开当前契约字段，嵌套值由编译器统一预检和编码。"""

        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class SourceManifestV1(SerializableContract):
    schema_version: str
    source_format: str
    source_schema: str
    dataset_id: str
    dataset_sha256: str
    byte_length: int
    physical_line_count: int


@dataclass(frozen=True, slots=True)
class SourceRecordRefV1(SerializableContract):
    schema_version: str
    source_record_id: str
    dataset_id: str
    dataset_sha256: str
    line_number: int
    byte_offset: int
    byte_length: int
    line_sha256: str
    ingestion_status: str
    parse_error: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class NormalizedCaptureV2(SerializableContract):
    schema_version: str
    capture_occurrence_id: str
    source_record_id: str
    source_capture_id: str
    candidate_group_id: str
    thread_id: str
    account_id: str
    representation: str
    adapter: Any
    normalization_version: Any
    model: Any
    source_models: Any
    source_request_count: int
    request_boundary_ids: tuple[str, ...]
    message_count: int
    tool_catalog_id: str
    request_time_start: Any
    request_time_end: Any
    response_time_end: Any
    raw_request_hash: Any
    target_hash: Any
    leaf_response_status: Any
    usage: Any
    protocol_adapters: Any
    sequence_repairs: Any
    input_truncation_status: str
    has_compaction: bool
    compaction_count: int
    compaction_hashes: Any
    domain_meta_sha256: str
    meta_sha256: str


@dataclass(frozen=True, slots=True)
class RequestBoundaryV1(SerializableContract):
    schema_version: str
    request_boundary_id: str
    capture_occurrence_id: str
    boundary_ordinal: int
    source_request_id: str
    owned_message_start_index: int
    terminal_message_index: int
    terminal_prefix_depth: int
    terminal_event_id: str


@dataclass(frozen=True, slots=True)
class EventOccurrenceV2(SerializableContract):
    schema_version: str
    event_occurrence_id: str
    capture_occurrence_id: str
    source_record_id: str
    sequence_number: int
    event_kind: str
    event_scope: str
    request_boundary_id: str | None
    message_index: int
    sub_index: int | None
    source_json_pointer: str
    visible_payload_utf8_byte_length: int
    visible_payload_sha256: str
    integrity_status: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ActionBatchV2(SerializableContract):
    schema_version: str
    action_batch_id: str
    capture_occurrence_id: str
    request_boundary_id: str | None
    assistant_event_id: str
    tool_call_event_ids: tuple[str, ...]
    execution_semantics: str


@dataclass(frozen=True, slots=True)
class ToolPairingRecordV3(SerializableContract):
    schema_version: str
    pairing_id: str
    capture_occurrence_id: str
    tool_call_id: str
    call_event_ids: tuple[str, ...]
    result_event_ids: tuple[str, ...]
    matched_call_event_id: str | None
    matched_result_event_id: str | None
    statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolCatalogV2(SerializableContract):
    schema_version: str
    tool_catalog_id: str
    capture_occurrence_id: str
    definitions: tuple[dict[str, Any], ...]
    definition_conflict: bool
    inferred_tool_names: tuple[str, ...]
    catalog_input_valid: bool
    catalog_sha256: str


@dataclass(frozen=True, slots=True)
class CaptureQualityV2(SerializableContract):
    schema_version: str
    source_record_id: str
    capture_occurrence_id: str | None
    processing_status: str
    boundary_status: str
    tool_pairing_applicable: bool
    tool_pairing_statuses: tuple[str, ...]
    tool_schema_status: str
    terminal_status: str
    privacy_status: str
    compaction_status: str
    processing_error: dict[str, Any] | None
    reason_codes: tuple[str, ...]
    boundary_count: int
    event_count: int
    action_batch_count: int
    tool_call_count: int
    tool_result_count: int
    missing_result_count: int
    duplicate_result_count: int


@dataclass(frozen=True, slots=True)
class AttritionReportV2(SerializableContract):
    schema_version: str
    dataset_id: str
    dataset_sha256: str
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class ArtifactEntryV1(SerializableContract):
    relative_path: str
    sha256: str
    byte_length: int
    record_count: int | None


@dataclass(frozen=True, slots=True)
class ArtifactManifestV1(SerializableContract):
    schema_version: str
    run_id: str
    source_schema: str
    dataset_id: str
    dataset_sha256: str
    compiler_contract_version: str
    files: tuple[dict[str, Any], ...]
