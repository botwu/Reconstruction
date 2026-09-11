"""显式 restored-long 来源适配契约测试。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from traceforge.trajectory.source_adapter import (
    RestoredLongCaptureV1,
    SourceRecordAdaptError,
    R01_SESSIONS_SCHEMA,
    adapt_source_record,
)


def test_adapter_returns_typed_restored_long_envelope(
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )

    envelope = adapt_source_record(capture)

    assert isinstance(envelope, RestoredLongCaptureV1)
    assert envelope.messages is capture["messages"]
    assert envelope.meta is capture["meta"]


def test_adapter_rejects_other_representation_without_echoing_value(
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    untrusted_value = "future-format-sensitive-literal"
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    capture["meta"]["representation"] = untrusted_value

    with pytest.raises(SourceRecordAdaptError) as error:
        adapt_source_record(capture)

    assert error.value.reason_code == "SOURCE_REPRESENTATION_UNSUPPORTED"
    assert untrusted_value not in str(error.value)


def test_adapter_projects_r01_sessions_envelope(
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "完成。"}],
        terminal_prefix_depths=[1],
    )
    meta = dict(capture["meta"])
    meta.pop("capture_id")
    meta.pop("thread_id")
    r01 = {
        "record_id": "r01-fixture-record",
        "messages": capture["messages"],
        "tools": capture["tools"],
        "meta": meta,
        "turn_quality_meta": {"input_audit": {"source_line": 1}},
        "task_domain_annotations": [{"task_id": "task-fixture"}],
        "task_domain_session_meta": {"version": "1", "task_count": 1},
        "rubric_partition": {"primary_rubric": {"code": "R01"}},
    }

    envelope = adapt_source_record(r01, source_schema=R01_SESSIONS_SCHEMA)

    assert envelope.messages is r01["messages"]
    assert envelope.tools is r01["tools"]
    assert envelope.meta["capture_id"] == meta["source_request_ids"][-1]
    assert envelope.meta["thread_id"] == meta["account_id"]
    assert envelope.domain_meta["schema"] == R01_SESSIONS_SCHEMA
    assert envelope.domain_meta["input_audit"]["input_truncated"] is None


def test_r01_requires_explicit_schema(
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "完成。"}],
        terminal_prefix_depths=[1],
    )
    r01 = {
        "record_id": "r01-fixture-record",
        "messages": capture["messages"],
        "tools": capture["tools"],
        "meta": capture["meta"],
        "turn_quality_meta": {},
        "task_domain_annotations": [],
        "task_domain_session_meta": {},
        "rubric_partition": {},
    }

    with pytest.raises(SourceRecordAdaptError) as error:
        adapt_source_record(r01)

    assert error.value.reason_code == "TOP_LEVEL_SCHEMA_MISMATCH"
