"""M1A 来源账本与变更检测测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from traceforge.trajectory.source import (
    SourceChangedError,
    SourceDigestMismatchError,
    iter_verified_records,
    scan_jsonl_source,
)
from traceforge.trajectory.source_adapter import RESTORED_LONG_CAPTURE_SCHEMA


def test_source_scan_preserves_every_physical_line(tmp_path: Path) -> None:
    raw = b"{}\n\n"
    source = tmp_path / "source.jsonl"
    source.write_bytes(raw)

    scan = scan_jsonl_source(
        source,
        dataset_id="fixture-lines-v1",
        source_schema=RESTORED_LONG_CAPTURE_SCHEMA,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    records = list(iter_verified_records(source, scan))

    assert scan.manifest.byte_length == 4
    assert scan.manifest.physical_line_count == 2
    assert [(record.reference.byte_offset, record.reference.byte_length) for record in records] == [
        (0, 3),
        (3, 1),
    ]
    assert records[0].reference.ingestion_status == "PARSED"
    assert records[0].value == {}
    assert records[1].reference.ingestion_status == "QUARANTINED"
    assert records[1].reference.parse_error["code"] == "JSON_SYNTAX_ERROR"


def test_source_stream_quarantines_unsafe_json_without_stopping_the_batch(tmp_path: Path) -> None:
    raw_lines = [
        b'{"sequence":1}\n',
        b'{"value":1e400}\n',
        b'{"value":' + b"9" * 5_000 + b"}\n",
        b'{"value":"\\ud800"}\n',
        b'{"sequence":2}\n',
    ]
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"".join(raw_lines))

    scan = scan_jsonl_source(
        source,
        dataset_id="fixture-quarantine-v1",
        source_schema=RESTORED_LONG_CAPTURE_SCHEMA,
    )
    records = list(iter_verified_records(source, scan))

    assert len(records) == len(raw_lines)
    assert [record.reference.ingestion_status for record in records] == [
        "PARSED",
        "QUARANTINED",
        "QUARANTINED",
        "QUARANTINED",
        "PARSED",
    ]
    assert [records[index].reference.parse_error["code"] for index in range(1, 4)] == [
        "NON_FINITE_NUMBER",
        "INTEGER_TOO_LONG",
        "UNPAIRED_SURROGATE",
    ]
    assert records[0].value == {"sequence": 1}
    assert records[-1].value == {"sequence": 2}


def test_source_stream_quarantines_extreme_nesting_without_stopping_the_batch(
    tmp_path: Path,
) -> None:
    deeply_nested = b"[" * 20_000 + b"0" + b"]" * 20_000 + b"\n"
    source = tmp_path / "deeply-nested.jsonl"
    source.write_bytes(deeply_nested + b'{"sequence":2}\n')

    scan = scan_jsonl_source(
        source,
        dataset_id="fixture-deeply-nested-v1",
        source_schema=RESTORED_LONG_CAPTURE_SCHEMA,
    )
    records = list(iter_verified_records(source, scan))

    assert [record.reference.ingestion_status for record in records] == [
        "QUARANTINED",
        "PARSED",
    ]
    assert records[0].reference.parse_error["code"] == "JSON_NESTING_TOO_DEEP"
    assert records[0].value is None
    assert records[1].value == {"sequence": 2}


@pytest.mark.parametrize("field", ["dataset_id", "source_schema"])
def test_source_scan_rejects_data_url_in_manifest_identity_without_echoing_payload(
    tmp_path: Path,
    field: str,
) -> None:
    source = tmp_path / "identity.jsonl"
    source.write_bytes(b"{}\n")
    secret = "U0VDUkVUX0lERU5USVRZ"
    arguments = {
        "dataset_id": "fixture-dataset-v1",
        "source_schema": RESTORED_LONG_CAPTURE_SCHEMA,
    }
    arguments[field] = f"prefix-data:image/png;base64 ,{secret}"

    with pytest.raises(ValueError) as captured:
        scan_jsonl_source(source, **arguments)

    assert "Data URL" in str(captured.value)
    assert secret not in str(captured.value)


def test_source_scan_rejects_wrong_frozen_digest(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"{}\n")

    with pytest.raises(SourceDigestMismatchError):
        scan_jsonl_source(
            source,
            dataset_id="fixture-digest-v1",
            source_schema=RESTORED_LONG_CAPTURE_SCHEMA,
            expected_sha256="0" * 64,
        )


def test_second_pass_rejects_source_changed_after_scan(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"{}\n")
    scan = scan_jsonl_source(
        source,
        dataset_id="fixture-change-v1",
        source_schema=RESTORED_LONG_CAPTURE_SCHEMA,
    )
    source.write_bytes(b"[]\n")

    with pytest.raises(SourceChangedError):
        list(iter_verified_records(source, scan))
