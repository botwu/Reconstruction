"""严格 JSON 与稳定标识测试。"""

from __future__ import annotations

import hashlib
import math
import sys

import pytest

from traceforge.trajectory.json_codec import (
    MAX_JSON_INTEGER_DIGITS,
    MAX_JSON_NESTING_DEPTH,
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


@pytest.mark.parametrize(
    "raw",
    [
        b'{"value":1e-400}',
        b'{"value":9007199254740993.0}',
    ],
)
def test_strict_json_rejects_float_values_changed_by_python_canonical_roundtrip(
    raw: bytes,
) -> None:
    with pytest.raises(StrictJsonError) as captured:
        strict_json_loads(raw)

    assert captured.value.code == "LOSSY_NUMBER"


def test_json_nesting_depth_has_an_explicit_inclusive_boundary() -> None:
    accepted = b"[" * MAX_JSON_NESTING_DEPTH + b"0" + b"]" * MAX_JSON_NESTING_DEPTH
    value = strict_json_loads(accepted)
    for _ in range(MAX_JSON_NESTING_DEPTH):
        assert isinstance(value, list) and len(value) == 1
        value = value[0]
    assert value == 0

    rejected = b"[" + accepted + b"]"
    with pytest.raises(StrictJsonError) as captured:
        strict_json_loads(rejected)

    assert captured.value.to_dict() == {
        "code": "JSON_NESTING_TOO_DEEP",
        "message": "JSON 嵌套深度超过契约上限",
        "maximum_depth": MAX_JSON_NESTING_DEPTH,
        "observed_depth": MAX_JSON_NESTING_DEPTH + 1,
    }


def test_integer_limit_is_independent_of_python_global_digit_limit() -> None:
    previous_limit = sys.get_int_max_str_digits()
    oversized = b'{"value":' + b"9" * 700 + b"}"
    errors: list[dict[str, object]] = []
    try:
        for global_limit in (0, sys.int_info.str_digits_check_threshold):
            sys.set_int_max_str_digits(global_limit)
            with pytest.raises(StrictJsonError) as captured:
                strict_json_loads(oversized)
            errors.append(captured.value.to_dict())

        sys.set_int_max_str_digits(sys.int_info.str_digits_check_threshold)
        boundary = b'{"value":' + b"9" * MAX_JSON_INTEGER_DIGITS + b"}"
        assert canonical_json_bytes(strict_json_loads(boundary)) == boundary
    finally:
        sys.set_int_max_str_digits(previous_limit)

    assert (
        errors
        == [
            {
                "code": "INTEGER_TOO_LONG",
                "message": "JSON 整数位数超过上限",
                "digit_count": 700,
                "maximum_digit_count": MAX_JSON_INTEGER_DIGITS,
            }
        ]
        * 2
    )


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
