"""确定性 JSON、摘要和稳定标识。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

# 深度按当前同时打开的对象和数组层数计算：根标量为 0，根容器为 1。
MAX_JSON_NESTING_DEPTH = 256
# Python 对十进制整数转换保证不检查的阈值；冻结为项目契约以隔离全局配置。
MAX_JSON_INTEGER_DIGITS = 640


class StrictJsonError(ValueError):
    """输入不满足严格 JSON 契约。"""

    def __init__(self, code: str, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            **self.context,
        }


def _reject_constant(_value: str) -> None:
    raise StrictJsonError(
        "NON_FINITE_NUMBER",
        "JSON 不允许非有限数值",
    )


def _parse_float(value: str) -> float:
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise StrictJsonError(
            "JSON_VALUE_ERROR",
            "JSON 数值无法稳定解析",
            {"number_character_length": len(value)},
        ) from exc
    if not math.isfinite(parsed):
        raise StrictJsonError(
            "NON_FINITE_NUMBER",
            "JSON 不允许非有限数值",
        )
    canonical = json.dumps(parsed, allow_nan=False, separators=(",", ":"))
    try:
        is_lossless = Decimal(value) == Decimal(canonical)
    except InvalidOperation as exc:
        raise StrictJsonError(
            "JSON_VALUE_ERROR",
            "JSON 数值无法稳定解析",
            {"number_character_length": len(value)},
        ) from exc
    if not is_lossless:
        raise StrictJsonError(
            "LOSSY_NUMBER",
            "JSON 数值无法由 Python canonical JSON 忠实往返",
            {"number_character_length": len(value)},
        )
    return parsed


def _parse_integer(value: str) -> int:
    digits = value.removeprefix("-")
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise StrictJsonError(
            "INTEGER_TOO_LONG",
            "JSON 整数位数超过上限",
            {
                "digit_count": len(digits),
                "maximum_digit_count": MAX_JSON_INTEGER_DIGITS,
            },
        )
    try:
        return int(value)
    except (OverflowError, ValueError) as exc:
        raise StrictJsonError(
            "INTEGER_PARSE_ERROR",
            "JSON 整数无法稳定解析",
            {"digit_count": len(digits)},
        ) from exc


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            key_bytes = key.encode("utf-8", errors="surrogatepass")
            raise StrictJsonError(
                "DUPLICATE_OBJECT_KEY",
                "JSON 对象包含重复字段",
                {
                    "key_character_length": len(key),
                    "key_sha256": sha256_bytes(key_bytes),
                },
            )
        result[key] = value
    return result


def _reject_unpaired_surrogates(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            surrogate_offset = _find_surrogate_offset(current)
            if surrogate_offset is not None:
                raise StrictJsonError(
                    "UNPAIRED_SURROGATE",
                    "JSON 字符串包含未配对的 Unicode 代理项",
                    {"character_offset": surrogate_offset},
                )
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())


def _find_surrogate_offset(value: str) -> int | None:
    for offset, character in enumerate(value):
        if 0xD800 <= ord(character) <= 0xDFFF:
            return offset
    return None


def _validate_json_nesting(text: str) -> int:
    """在递归 parser 前执行确定性的词法深度门控，并返回观测最大深度。"""

    depth = 0
    maximum_observed_depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            maximum_observed_depth = max(maximum_observed_depth, depth)
            if depth > MAX_JSON_NESTING_DEPTH:
                raise StrictJsonError(
                    "JSON_NESTING_TOO_DEEP",
                    "JSON 嵌套深度超过契约上限",
                    {
                        "maximum_depth": MAX_JSON_NESTING_DEPTH,
                        "observed_depth": depth,
                    },
                )
        elif character in "]}" and depth > 0:
            depth -= 1
    return maximum_observed_depth


def strict_json_loads(raw: bytes) -> Any:
    """解析严格 JSON，拒绝非确定或无法安全表示的值。"""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictJsonError(
            "INVALID_UTF8",
            "JSON 输入必须使用 UTF-8 编码",
            {"byte_end": exc.end, "byte_start": exc.start},
        ) from exc
    maximum_observed_depth = _validate_json_nesting(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
            parse_int=_parse_integer,
        )
    except StrictJsonError:
        raise
    except RecursionError as exc:
        raise StrictJsonError(
            "JSON_NESTING_TOO_DEEP",
            "JSON 嵌套深度超过契约上限",
            {
                "maximum_depth": MAX_JSON_NESTING_DEPTH,
                "observed_depth": maximum_observed_depth,
            },
        ) from exc
    except json.JSONDecodeError as exc:
        raise StrictJsonError(
            "JSON_SYNTAX_ERROR",
            "JSON 文本语法错误",
            {
                "character_offset": exc.pos,
                "column_number": exc.colno,
                "line_number": exc.lineno,
            },
        ) from exc
    except (OverflowError, ValueError) as exc:
        raise StrictJsonError(
            "JSON_VALUE_ERROR",
            "JSON 值无法稳定解析",
        ) from exc

    _reject_unpaired_surrogates(value)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """将对象编码为项目唯一的 canonical JSON。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_line(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_id(namespace: str, identity: Any) -> str:
    payload = namespace.encode("utf-8") + b"\0" + canonical_json_bytes(identity)
    return sha256_bytes(payload)


def source_record_id(
    *,
    dataset_id: str,
    dataset_sha256: str,
    line_number: int,
    line_sha256: str,
) -> str:
    payload = b"\0".join(
        (
            b"source-record-v1",
            dataset_id.encode("utf-8"),
            dataset_sha256.encode("ascii"),
            str(line_number).encode("ascii"),
            line_sha256.encode("ascii"),
        )
    )
    return sha256_bytes(payload)
