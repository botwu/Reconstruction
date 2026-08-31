"""派生数据隐私规则的纯函数测试。"""

from __future__ import annotations

import pytest

from traceforge.trajectory.json_codec import canonical_json_bytes, sha256_bytes
from traceforge.trajectory.privacy import (
    DATA_URL_SUMMARY_V1,
    ESCAPED_OBJECT_V1,
    PRIVACY_ENVELOPE_MARKER,
    PrivacyTransformError,
    find_data_url_spans,
    find_privacy_violations,
    is_reasoning_summary,
    privacy_envelope_kind,
    sanitize_value,
    visible_value_without_reasoning,
)


def test_nested_reasoning_fields_are_replaced_by_fixed_summaries() -> None:
    source = {
        "blocks": [
            {
                "metadata": {
                    "kept": "可见字段",
                    "reasoning_content": "内容块推理哨兵",
                }
            }
        ],
        "vendor": {
            "nested": {
                "reasoning_content": {"secret": "供应商推理哨兵"},
            }
        },
    }

    sanitized = sanitize_value(source, "/messages/0/content")
    encoded = canonical_json_bytes(sanitized)
    assert "内容块推理哨兵".encode() not in encoded
    assert "供应商推理哨兵".encode() not in encoded

    block_summary = sanitized["blocks"][0]["metadata"]["reasoning_content"]
    block_raw = "内容块推理哨兵".encode()
    assert block_summary == {
        "present": True,
        "utf8_byte_length": len(block_raw),
        "sha256": sha256_bytes(block_raw),
        "source_json_pointer": "/messages/0/content/blocks/0/metadata/reasoning_content",
    }

    visible = visible_value_without_reasoning(sanitized)
    assert visible["blocks"][0]["metadata"] == {"kept": "可见字段"}
    assert visible["vendor"]["nested"] == {}


@pytest.mark.parametrize(
    "value",
    [
        "前文 DATA:image/png;BaSe64 ,QUJD%0AREVG%3D 后文",
        "前文 data:image/png;base64,\nQUJD\nREVG== 后文",
        "前文 data:text/plain;charset=utf-8;BASE64 \n,QUJD%2BREVG%3D 后文",
        "前文 data:application/octet-stream;charset*=utf-8'';base64,QUJD%2FREVGRw== 后文",
    ],
)
def test_data_url_scanner_consumes_wrapped_and_percent_escaped_payload(value: str) -> None:
    spans = find_data_url_spans(value)

    assert len(spans) == 1
    matched = value[spans[0].start : spans[0].end]
    assert matched.casefold().startswith("data:")
    assert "REVG" in matched

    sanitized = sanitize_value(value, "/value")
    encoded = canonical_json_bytes(sanitized)
    assert b"QUJD" not in encoded
    assert b"REVG" not in encoded
    assert "前文".encode() in encoded
    assert "后文".encode() in encoded


def test_base64_parameter_fragment_without_data_scheme_is_plain_text() -> None:
    value = "普通文本只提到 ;base64, 并不是 Data URL"

    assert find_data_url_spans(value) == ()
    assert sanitize_value(value, "/value") == value
    assert find_privacy_violations(value) == ()


def test_recursive_privacy_validator_distinguishes_raw_and_summarized_values() -> None:
    raw = {
        "safe": "普通文本只提到 ;base64,",
        "nested": [
            {"reasoning_content": "推理原文"},
            "data:image/png;base64 ,U0VDUkVU",
        ],
    }
    sanitized = sanitize_value(raw, "/root")

    assert {violation.code for violation in find_privacy_violations(raw)} == {
        "RAW_DATA_URL",
        "RAW_REASONING",
    }
    assert find_privacy_violations(sanitized) == ()
    assert is_reasoning_summary(sanitized["nested"][0]["reasoning_content"])


def test_business_object_cannot_collide_with_internal_privacy_envelope() -> None:
    old_shape = {
        "kind": "OBJECT_ENTRIES",
        "entries": [{"key": "reasoning_content", "value": "合法业务值"}],
        "entry_count": 1,
    }
    sanitized_old_shape = sanitize_value(old_shape, "/business")

    assert privacy_envelope_kind(sanitized_old_shape) is None
    assert visible_value_without_reasoning(sanitized_old_shape) == old_shape

    reserved_shape = {
        PRIVACY_ENVELOPE_MARKER: DATA_URL_SUMMARY_V1,
        "mime_type": "业务字段",
        "source_json_pointer": "/业务路径",
    }
    sanitized_reserved_shape = sanitize_value(reserved_shape, "/reserved")
    assert privacy_envelope_kind(sanitized_reserved_shape) == ESCAPED_OBJECT_V1

    visible = visible_value_without_reasoning(sanitized_reserved_shape)
    assert privacy_envelope_kind(visible) == ESCAPED_OBJECT_V1
    visible_entries = {entry["key"]: entry["value"] for entry in visible["entries"]}
    assert visible_entries[PRIVACY_ENVELOPE_MARKER] == DATA_URL_SUMMARY_V1
    assert visible_entries["mime_type"] == "业务字段"


def test_data_url_key_uses_strict_escaped_object_envelope() -> None:
    source = {
        "data:image/png;base64,QUJD%2FREVGRw==": "值",
        "kind": "OBJECT_ENTRIES",
    }

    sanitized = sanitize_value(source, "/root")

    assert privacy_envelope_kind(sanitized) == ESCAPED_OBJECT_V1
    assert find_privacy_violations(sanitized) == ()
    assert "QUJD" not in canonical_json_bytes(sanitized).decode("utf-8")


def test_reasoning_summary_pointer_must_be_safe_json_pointer() -> None:
    summary = {
        "present": True,
        "utf8_byte_length": 1,
        "sha256": "a" * 64,
        "source_json_pointer": "/data:image/png;base64,U0VDUkVU",
    }
    value = {"reasoning_content": summary}

    assert is_reasoning_summary(summary) is False
    assert {violation.code for violation in find_privacy_violations(value)} == {
        "RAW_DATA_URL",
        "RAW_REASONING",
    }


def test_privacy_transform_has_stable_derived_depth_limit() -> None:
    nested: object = "data:image/png;base64,U0VDUkVU"
    for _ in range(180):
        nested = {"child": nested}

    with pytest.raises(PrivacyTransformError) as captured:
        sanitize_value(nested, "/root")

    assert captured.value.reason_code == "PRIVACY_TRANSFORM_DEPTH_EXCEEDED"
    assert "U0VDUkVU" not in str(captured.value)
