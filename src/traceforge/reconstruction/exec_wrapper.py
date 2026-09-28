"""只识别能静态证明输出顺序的并行 exec 包装，不执行 JavaScript。"""
from __future__ import annotations

import json
import math
import re
from typing import Any

_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_WRAPPER = re.compile(
    rf"\s*const\s+(?P<batch>{_IDENTIFIER})\s*=\s*await\s+"
    r"Promise\s*\.\s*all\s*\(\s*\[(?P<calls>[\s\S]*)\]\s*\)\s*;\s*"
    rf"for\s*\(\s*const\s+(?P<item>{_IDENTIFIER})\s+of\s+(?P=batch)\s*\)\s*"
    r"text\s*\(\s*(?:"
    r"(?P=item)(?P<stdout>\s*\.\s*output)?|"
    r"JSON\s*\.\s*stringify\s*\(\s*\{\s*"
    r"exit_code\s*:\s*(?P=item)\s*\.\s*exit_code\s*,\s*"
    r"output\s*:\s*(?P=item)\s*\.\s*output\s*\}\s*\)"
    r")\s*\)\s*;\s*"
)
_CALL = re.compile(r"\s*tools\s*\.\s*(?:exec_command|shell_command)\s*\(\s*\{")
_KEY = re.compile(rf'\s*({_IDENTIFIER}|"(?:[^"\\]|\\.)*")\s*:\s*')
_DECODER = json.JSONDecoder()
_RESERVED_BINDINGS = frozenset({
    "tools", "Promise", "JSON", "text", "await", "break", "case", "catch", "class", "const",
    "continue", "debugger", "default", "delete", "do", "else", "enum", "export", "extends",
    "false", "finally", "for", "function", "if", "implements", "import", "in", "instanceof",
    "interface", "let", "new", "null", "package", "private", "protected", "public", "return",
    "static", "super", "switch", "this", "throw", "true", "try", "typeof", "var", "void",
    "while", "with", "yield", "eval", "arguments",
})


def _literal_arguments(source: str, position: int) -> tuple[dict[str, Any], int] | None:
    arguments: dict[str, Any] = {}
    while True:
        closing = re.match(r"\s*\}", source[position:])
        if closing:
            return arguments, position + closing.end()
        key_match = _KEY.match(source, position)
        if key_match is None:
            return None
        raw_key = key_match.group(1)
        try:
            key = json.loads(raw_key) if raw_key.startswith('"') else raw_key
            value, end = _DECODER.raw_decode(source, key_match.end())
        except (json.JSONDecodeError, ValueError):
            return None
        # JS 的特殊原型属性不是普通参数；非有限数及复合值也不属于已支持的协议。
        if (
            key in arguments
            or key == "__proto__"
            or isinstance(value, (dict, list))
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            return None
        arguments[key] = value
        delimiter = re.match(r"\s*([,}])", source[end:])
        if delimiter is None:
            return None
        position = end + delimiter.end()
        if delimiter.group(1) == "}":
            return arguments, position


def _parse_parallel_exec(source: str) -> tuple[list[dict[str, Any]], bool] | None:
    """共用完整静态校验，输出模式不能脱离调用参数单独获信任。"""
    match = _WRAPPER.fullmatch(source)
    if match is None:
        return None
    batch, item = match.group("batch", "item")
    if batch == item or {batch, item} & _RESERVED_BINDINGS:
        return None
    body = match.group("calls")
    if not body.strip():
        return [], match.group("stdout") is not None
    calls: list[dict[str, Any]] = []
    position = 0
    while position < len(body):
        call = _CALL.match(body, position)
        if call is None:
            return None
        parsed = _literal_arguments(body, call.end())
        if parsed is None:
            return None
        arguments, position = parsed
        if "cmd" in arguments and "command" in arguments:
            return None
        command = arguments.get("cmd", arguments.get("command"))
        if not isinstance(command, str) or not command.strip():
            return None
        closing = re.match(r"\s*\)\s*", body[position:])
        if closing is None:
            return None
        position += closing.end()
        calls.append(arguments)
        if position == len(body):
            return calls, match.group("stdout") is not None
        if body[position] != ",":
            return None
        position += 1
    return None


def ordered_parallel_exec_calls(source: str) -> list[dict[str, Any]] | None:
    """返回有序字面量参数；不能静态证明时返回 None。

    只接受 const 绑定 Promise.all 后的固定 for-of text 输出：完整结果、
    item.output，或 JSON.stringify({exit_code:item.exit_code, output:item.output})。
    拒绝其他输出变换、动态调用参数、额外语句和重排，不推断并行完成时序。
    """
    parsed = _parse_parallel_exec(source)
    return parsed[0] if parsed is not None else None


def parallel_exec_prints_stdout(source: str) -> bool:
    """仅在完整包装通过校验且明确 text(item.output) 时返回 True。

    False 也可能表示不支持的包装；调用方须先用 ordered_parallel_exec_calls
    判定包装有效性，再决定结果块是原始 stdout 还是 JSON。
    """
    parsed = _parse_parallel_exec(source)
    return parsed is not None and parsed[1]
