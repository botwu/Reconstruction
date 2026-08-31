"""显式 restored-long 来源适配契约测试。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from traceforge.trajectory.source_adapter import (
    RestoredLongCaptureV1,
    SourceRecordAdaptError,
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
