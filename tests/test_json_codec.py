"""严格 JSON 与稳定标识测试。"""

from __future__ import annotations

import hashlib
import math

import pytest

from traceforge.trajectory.json_codec import (
    StrictJsonError,
    canonical_json_bytes,
    source_record_id,
    strict_json_loads,
)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"same":1,"same":2}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        '{"value":"utf16"}'.encode("utf-16"),
    ],
)
def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers(raw: bytes) -> None:
    with pytest.raises(StrictJsonError):
        strict_json_loads(raw)


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        (b'{"value":1e400}', "NON_FINITE_NUMBER"),
        (b'{"value":' + b"9" * 5_000 + b"}", "INTEGER_TOO_LONG"),
        (b'{"value":"\\ud800"}', "UNPAIRED_SURROGATE"),
        (b'{"\\udfff":true}', "UNPAIRED_SURROGATE"),
    ],
)
def test_strict_json_converts_unsafe_values_to_stable_errors(
    raw: bytes,
    expected_code: str,
) -> None:
    with pytest.raises(StrictJsonError) as captured:
        strict_json_loads(raw)

    assert captured.value.code == expected_code
    assert captured.value.to_dict()["code"] == expected_code


def test_strict_json_accepts_a_valid_surrogate_pair() -> None:
    assert strict_json_loads(b'{"value":"\\ud83d\\ude00"}') == {"value": "😀"}


def test_duplicate_key_error_contains_only_length_and_digest() -> None:
    private_key = "private-access-token"
    raw = f'{{"{private_key}":1,"{private_key}":2}}'.encode()

    with pytest.raises(StrictJsonError) as captured:
        strict_json_loads(raw)

    error = captured.value.to_dict()
    assert error == {
        "code": "DUPLICATE_OBJECT_KEY",
        "message": "JSON 对象包含重复字段",
        "key_character_length": len(private_key),
        "key_sha256": hashlib.sha256(private_key.encode()).hexdigest(),
    }
    assert private_key not in str(captured.value)
    assert private_key not in str(error)


def test_surrogate_error_does_not_echo_the_malicious_value() -> None:
    private_value = "private-value"
    raw = f'{{"value":"{private_value}\\ud800"}}'.encode()

    with pytest.raises(StrictJsonError) as captured:
        strict_json_loads(raw)

    assert captured.value.code == "UNPAIRED_SURROGATE"
    assert private_value not in str(captured.value)
    assert private_value not in str(captured.value.to_dict())


def test_canonical_json_is_sorted_utf8_and_rejects_nan() -> None:
    assert canonical_json_bytes({"乙": 2, "a": 1}) == b'{"a":1,"\xe4\xb9\x99":2}'
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": math.nan})


def test_source_record_id_has_a_frozen_lf_sensitive_golden_value() -> None:
    # dataset 与 line 都覆盖原始物理行的 LF；这个值用于锁死来源公式。
    digest = "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
    assert (
        source_record_id(
            dataset_id="fixture-v1",
            dataset_sha256=digest,
            line_number=1,
            line_sha256=digest,
        )
        == "7a9a2a7d98bda30e39737a0726ee728ad504a8b3190e332beb858f2e07a77229"
    )
