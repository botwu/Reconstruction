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
    r"text\s*\(\s*(?P=item)\s*\)\s*;\s*"
)
_CALL = re.compile(r"\s*tools\s*\.\s*(?:exec_command|shell_command)\s*\(\s*\{")
_KEY = re.compile(rf'\s*({_IDENTIFIER}|"(?:[^"\\]|\\.)*")\s*:\s*')
_DECODER = json.JSONDecoder()
_RESERVED_BINDINGS = frozenset({
    "tools", "Promise", "text", "await", "break", "case", "catch", "class", "const",
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


def ordered_parallel_exec_calls(source: str) -> list[dict[str, Any]] | None:
    """返回按原输出顺序排列的字面量参数；无法静态证明时返回 None。

    只接受 const 绑定 Promise.all([...]) 后直接 for-of text 的完整包装。
    每项只能是 tools.exec_command 或 tools.shell_command 的直接调用，参数值
    只接受 JSON 标量。拒绝动态表达式、额外语句和重排，不推断并行调用完成时序。
    """
    match = _WRAPPER.fullmatch(source)
    if match is None:
        return None
    batch, item = match.group("batch", "item")
    if batch == item or {batch, item} & _RESERVED_BINDINGS:
        return None
    body = match.group("calls")
    if not body.strip():
        return []
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
        if not isinstance(arguments.get("cmd", arguments.get("command")), str):
            return None
        closing = re.match(r"\s*\)\s*", body[position:])
        if closing is None:
            return None
        position += closing.end()
        calls.append(arguments)
        if position == len(body):
            return calls
        if body[position] != ",":
            return None
        position += 1
    return None
