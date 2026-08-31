"""JSONL 来源的流式扫描、严格解析与逐行复核。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceforge.trajectory.contracts import (
    SOURCE_MANIFEST_SCHEMA,
    SOURCE_RECORD_SCHEMA,
    IngestionStatus,
    SourceManifestV1,
    SourceRecordRefV1,
)
from traceforge.trajectory.json_codec import (
    StrictJsonError,
    sha256_bytes,
    source_record_id,
    strict_json_loads,
)
from traceforge.trajectory.privacy import contains_data_url


class SourceError(RuntimeError):
    """来源扫描或复核失败。"""


class SourceReadError(SourceError):
    """来源不是可稳定读取的普通文件。"""


class SourceChangedError(SourceError):
    """来源在扫描或复核过程中发生变化。"""


class SourceDigestMismatchError(SourceError):
    """来源摘要与冻结值不一致。"""


@dataclass(frozen=True, slots=True)
class ParsedSourceRecord:
    """第二遍复核并严格解析后产生的一条来源记录。"""

    reference: SourceRecordRefV1
    value: Any | None


@dataclass(frozen=True, slots=True)
class _FileSignature:
    device: int
    inode: int
    mode: int
    byte_length: int
    modified_time_ns: int
    changed_time_ns: int


@dataclass(frozen=True, slots=True)
class SourceScan:
    """第一遍扫描结果；不包含原始行或已解析 JSON 对象。"""

    manifest: SourceManifestV1
    _file_signature: _FileSignature


def scan_jsonl_source(
    path: str | Path,
    *,
    dataset_id: str,
    source_schema: str,
    expected_sha256: str | None = None,
) -> SourceScan:
    """流式扫描 JSONL，建立数据集及物理行的不可变字节账本。"""

    source_path = Path(path)
    _validate_dataset_id(dataset_id)
    _validate_source_schema(source_schema)
    normalized_expected_sha256 = _normalize_expected_sha256(expected_sha256)

    dataset_hasher = hashlib.sha256()
    byte_offset = 0
    physical_line_count = 0

    try:
        with source_path.open("rb") as source:
            before = _file_signature(source.fileno())
            _require_regular_file(before, source_path)

            for raw_line in source:
                byte_length = len(raw_line)
                dataset_hasher.update(raw_line)
                byte_offset += byte_length
                physical_line_count += 1

            after = _file_signature(source.fileno())
    except OSError as exc:
        raise SourceReadError(f"无法读取来源文件 {source_path}：{exc}") from exc

    _require_same_signature(before, after, phase="第一遍扫描")
    if byte_offset != after.byte_length:
        raise SourceChangedError(
            f"第一遍扫描得到的字节数与文件状态不一致：扫描={byte_offset}，文件={after.byte_length}"
        )

    dataset_sha256 = dataset_hasher.hexdigest()
    if normalized_expected_sha256 is not None and dataset_sha256 != normalized_expected_sha256:
        raise SourceDigestMismatchError(
            f"来源 SHA-256 与冻结值不一致：期望={normalized_expected_sha256}，实际={dataset_sha256}"
        )

    manifest = SourceManifestV1(
        schema_version=SOURCE_MANIFEST_SCHEMA,
        source_format="jsonl",
        source_schema=source_schema,
        dataset_id=dataset_id,
        dataset_sha256=dataset_sha256,
        byte_length=byte_offset,
        physical_line_count=physical_line_count,
    )
    return SourceScan(manifest=manifest, _file_signature=after)


def iter_verified_records(
    path: str | Path,
    scan: SourceScan,
) -> Iterator[ParsedSourceRecord]:
    """第二遍复核并解析来源；调用方必须消费迭代器直至结束。"""

    source_path = Path(path)
    dataset_hasher = hashlib.sha256()
    observed_byte_length = 0
    observed_line_count = 0

    try:
        with source_path.open("rb") as source:
            before = _file_signature(source.fileno())
            _require_regular_file(before, source_path)
            _require_same_signature(
                scan._file_signature,
                before,
                phase="两遍扫描之间",
            )

            for line_number, raw_line in enumerate(source, start=1):
                line_offset = observed_byte_length
                dataset_hasher.update(raw_line)
                observed_byte_length += len(raw_line)
                observed_line_count = line_number
                line_sha256 = sha256_bytes(raw_line)
                yield _parse_record(
                    raw_line,
                    scan.manifest,
                    line_number=line_number,
                    byte_offset=line_offset,
                    line_sha256=line_sha256,
                )

            after = _file_signature(source.fileno())
    except SourceError:
        raise
    except OSError as exc:
        raise SourceReadError(f"无法复核来源文件 {source_path}：{exc}") from exc

    _require_same_signature(before, after, phase="第二遍复核")
    _verify_dataset(
        scan.manifest,
        dataset_hasher.hexdigest(),
        observed_byte_length,
        observed_line_count,
    )


def _parse_record(
    raw_line: bytes,
    manifest: SourceManifestV1,
    *,
    line_number: int,
    byte_offset: int,
    line_sha256: str,
) -> ParsedSourceRecord:
    try:
        value = strict_json_loads(raw_line)
        ingestion_status = IngestionStatus.PARSED
        parse_error = None
    except StrictJsonError as exc:
        value = None
        ingestion_status = IngestionStatus.QUARANTINED
        parse_error = exc.to_dict()

    reference = SourceRecordRefV1(
        schema_version=SOURCE_RECORD_SCHEMA,
        source_record_id=source_record_id(
            dataset_id=manifest.dataset_id,
            dataset_sha256=manifest.dataset_sha256,
            line_number=line_number,
            line_sha256=line_sha256,
        ),
        dataset_id=manifest.dataset_id,
        dataset_sha256=manifest.dataset_sha256,
        line_number=line_number,
        byte_offset=byte_offset,
        byte_length=len(raw_line),
        line_sha256=line_sha256,
        ingestion_status=ingestion_status,
        parse_error=parse_error,
    )
    return ParsedSourceRecord(reference=reference, value=value)


def _verify_dataset(
    manifest: SourceManifestV1,
    observed_sha256: str,
    observed_byte_length: int,
    observed_line_count: int,
) -> None:
    if observed_byte_length != manifest.byte_length:
        raise SourceChangedError(
            "第二遍复核得到的数据集字节数与第一遍不一致："
            f"实际={observed_byte_length}，期望={manifest.byte_length}"
        )
    if observed_sha256 != manifest.dataset_sha256:
        raise SourceChangedError(
            "第二遍复核得到的数据集 SHA-256 与第一遍不一致："
            f"实际={observed_sha256}，期望={manifest.dataset_sha256}"
        )
    if observed_line_count != manifest.physical_line_count:
        raise SourceChangedError(
            "第二遍复核得到的物理行数与第一遍不一致："
            f"实际={observed_line_count}，期望={manifest.physical_line_count}"
        )


def _file_signature(file_descriptor: int) -> _FileSignature:
    file_stat = os.fstat(file_descriptor)
    return _FileSignature(
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        mode=file_stat.st_mode,
        byte_length=file_stat.st_size,
        modified_time_ns=file_stat.st_mtime_ns,
        changed_time_ns=file_stat.st_ctime_ns,
    )


def _require_regular_file(signature: _FileSignature, path: Path) -> None:
    if not stat.S_ISREG(signature.mode):
        raise SourceReadError(f"来源必须是普通文件：{path}")


def _require_same_signature(
    expected: _FileSignature,
    observed: _FileSignature,
    *,
    phase: str,
) -> None:
    if observed != expected:
        raise SourceChangedError(f"来源在{phase}发生变化：期望={expected}，实际={observed}")


def _validate_dataset_id(dataset_id: str) -> None:
    if not isinstance(dataset_id, str) or not dataset_id:
        raise ValueError("dataset_id 必须是非空字符串")
    if contains_data_url(dataset_id):
        raise ValueError("dataset_id 不得包含 Data URL")


def _validate_source_schema(source_schema: str) -> None:
    if not isinstance(source_schema, str) or not source_schema:
        raise ValueError("source_schema 必须是非空字符串")
    if contains_data_url(source_schema):
        raise ValueError("source_schema 不得包含 Data URL")


def _normalize_expected_sha256(expected_sha256: str | None) -> str | None:
    if expected_sha256 is None:
        return None
    if not isinstance(expected_sha256, str):
        raise ValueError("expected_sha256 必须是 64 位十六进制字符串")
    normalized = expected_sha256.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("expected_sha256 必须是 64 位十六进制字符串")
    return normalized
