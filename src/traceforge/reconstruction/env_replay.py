"""Terminal-Universe Stage 1：从原始 tool_timeline 还原初始 workspace。

论文 B.1：不同来源把文件操作编成不同工具，先归一成 read/write/edit，
再按路径保留「被改之前最早看到的内容」。Agent 新建文件和之后的改动扣下。
不执行历史命令。未解析的潜在写操作是信任屏障：保留屏障前的观察，屏障后的新内容仅作私有证据。

真实 R01 里大量 exec 包在 JS ``tools.exec_command({cmd:...})`` / PowerShell
``Get-Content`` / ``sed -n`` 里，本模块把这些收成同一条 read 流。

重建主链用本模块。`trajectory_replay` / 旧 workflow 不再当入口。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from traceforge.reconstruction.terminal_universe_environment import (
    ReplayedFile,
    ReplayResult,
    WithheldChange,
)

REPLAY_FROM_SOURCE_SCHEMA = "traceforge.reconstruction-env-replay.v1"
_IGNORE_COMMANDS = frozenset({"wait", "sleep", "true", "false", "echo"})
_READ_COMMANDS = frozenset({"cat", "head", "tail", "type"})
_JS_CALL = re.compile(r"tools\.(?:exec_command|shell_command)\s*\(\s*\{", re.I)
_JS_CMD_KEY = re.compile(r"""(?:['\"]?(?:cmd|command)['\"]?)\s*:\s*(['\"])""")
_GET_CONTENT = re.compile(r"(?i)\bGet-Content\b")
_GET_CONTENT_PATH = re.compile(
    r"(?i)Get-Content\b[^|;]*?-(?:LiteralPath|Path)\s+(?P<path>'[^']+'|\"[^\"]+\"|[^\s|;]+)"
)
_GET_CONTENT_BARE = re.compile(
    r"(?i)\bGet-Content\s+(?P<path>'[^']+'|\"[^\"]+\"|[^\s|;]+)"
)
_PS_VAR_ASSIGN = re.compile(
    r"""(?i)\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['\"])([^'\"]+)\2"""
)
_PS_VAR_REF = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)")
_MUTATION_TOKEN = re.compile(
    r"(?i)(?:write_text|write_bytes|Set-Content|Add-Content|Out-File|"
    r"Remove-Item|Move-Item|Copy-Item|New-Item|"
    r"(?:os\.(?:remove|unlink|replace))|"
    r"Path\s*\([^)]*\)\s*\.\s*write|"
    r"open\s*\([^)]*['\"][abwx+]+['\"]|"
    r"(?:^|[;&|])\s*(?:cp|mv|install|rsync|touch|mkdir|truncate|tee)\b|"
    r"\b(?:sed|perl)\b[^\n;]*\s-[A-Za-z]*i(?:[^A-Za-z]|$))"
)
_FRAMEWORK_PARTS = frozenset({".git", "hidden_control", ".codex"})
_UNKNOWN_PATH_CTOR = re.compile(
    r"""(?:Path|open|write_text|write_bytes)\(\s*(['\"])(?P<path>[^'\"]+)\1""",
    re.I,
)
_UNKNOWN_QUOTED = re.compile(r"""(['\"])([^'\"]+)\1""")
_UNKNOWN_BARE_FILE = re.compile(r"""(?<![A-Za-z0-9_])((?:[\w./\\-]+)\.[A-Za-z0-9]{1,8})\b""")
_UNKNOWN_SKIP_TOKENS = frozenset(
    {"utf-8", "utf8", "ascii", "w", "wb", "r", "rb", "a", "wt", "x", "n", "t"}
)
_VERSION_OR_FLOAT = re.compile(r"^\d+(?:\.\d+){1,3}[A-Za-z]?$")
_DOI_PREFIX = re.compile(r"^\d{2}\.\d{4,}")
_LINE_RANGE = re.compile(r"\d+\.\.\d+")
_EXEC_WRAPPER = re.compile(
    r"\A(?:"
    r"(?:Script (?:completed|failed)|Exit code:\s*\d+)\r?\n"
    r"Wall time:?[^\r\n]+\r?\n"
    r"(?:(?:Total output lines|Warning):[^\r\n]*\r?\n)*"
    r"Output:\r?\n"
    r")",
    re.I,
)
_JSON_TRANSFORM_PIPE = re.compile(r"(?i)\|\s*Convert(?:From|To)-Json")
_RG_HIT = re.compile(r"^(?P<path>\S[^:\r\n]*):(?P<line>\d+):", re.M)
_NUMBERED_LINE = re.compile(r"^\s*\d+:")
_RESULT_SEGMENT = re.compile(r"^---\s*(?:result\s+)?(\d+)\s*---[ \t]*", re.M | re.I)
_LISTING_NAME_HEADER = re.compile(r"(?m)^Name\s+")
_GENERIC_C_NAMES = frozenset({"name.c", "path.c", "value.c", "v2.c", "v3.c"})
_CAMEL_CASE_C = re.compile(r"^[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*\.c$")
_COMPOUND_SUFFIXES = (".d.ts", ".min.js", ".spec.ts", ".test.ts", ".test.js", ".mod.c")


class EnvironmentReplayError(ValueError):
    """无法从 tool_timeline 做 Stage 1 回放。"""


def _decode_js_string(source: str, start: int, quote: str) -> tuple[str, int]:
    out: list[str] = []
    index = start
    mapping = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "'": "'", "\\": "\\"}
    while index < len(source):
        char = source[index]
        if char == "\\" and index + 1 < len(source):
            out.append(mapping.get(source[index + 1], source[index + 1]))
            index += 2
            continue
        if char == quote:
            return "".join(out), index + 1
        out.append(char)
        index += 1
    return "".join(out), index


def commands_from_js(text: str) -> list[str]:
    """从 Codex JS wrapper 里抽出真实 shell 命令。"""

    commands: list[str] = []
    for match in _JS_CALL.finditer(text or ""):
        window = text[match.end() : match.end() + 8000]
        key = _JS_CMD_KEY.search(window)
        if key is None:
            continue
        command, _ = _decode_js_string(window, key.end(), key.group(1))
        if command.strip():
            commands.append(command.strip())
    return commands


def _normalize_path_string(value: str) -> str:
    raw = value.strip().strip("\"'").replace("\\", "/")
    raw = re.sub(r"^[A-Za-z]:/", "", raw)
    if raw.startswith("~/"):
        raw = raw[2:]
    return raw


def _safe_relpath(value: str, workdir: str | None = None) -> str | None:
    raw = _normalize_path_string(value)
    if not raw or raw.startswith("-") or raw.startswith("$"):
        return None
    if workdir:
        root = _normalize_path_string(workdir).rstrip("/")
        if raw == root:
            return None
        prefix = root + "/"
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
        elif raw.startswith("/" + prefix):
            raw = raw[len(prefix) + 1 :]
    path = PurePosixPath(raw)
    if path.is_absolute():
        parts = [part for part in path.parts if part != "/"]
        if len(parts) >= 3 and parts[0] in {"Users", "home"}:
            parts = parts[2:]
        raw = "/".join(parts)
        path = PurePosixPath(raw)
    if not path.parts or ".." in path.parts:
        return None
    if any(part in _FRAMEWORK_PARTS for part in path.parts):
        return None
    normalized = path.as_posix()
    return normalized[2:] if normalized.startswith("./") else normalized


def _command(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments.strip()
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "cmd", "cmd_string"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _item_workdir(item: dict[str, Any]) -> str | None:
    arguments = item.get("arguments")
    if not isinstance(arguments, dict):
        return None
    for key in ("workdir", "working_directory", "cwd", "workingDirectory"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _item_commands(item: dict[str, Any]) -> list[str]:
    arguments = item.get("arguments")
    native = _command(arguments)
    extracted: list[str] = []
    if isinstance(arguments, dict) and isinstance(arguments.get("input"), str):
        extracted = commands_from_js(arguments["input"])
    elif native and ("tools.exec_command" in native or "tools.shell_command" in native):
        extracted = commands_from_js(native)
    if extracted:
        return extracted
    return [native] if native else []


def _path_from_args(arguments: Any, workdir: str | None = None) -> str | None:
    if not isinstance(arguments, dict):
        return None
    for key in ("path", "file_path", "filePath", "filename", "file"):
        value = arguments.get(key)
        if isinstance(value, str):
            return _safe_relpath(value, workdir)
    return None


def _redirect_target(command: str, workdir: str | None = None) -> str | None:
    """仅识别引号外的重定向，搜索模式里的尖括号不是写操作。"""

    quote: str | None = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char != ">" or (index and command[index - 1] == ">"):
            continue
        remainder = command[index + 1:].lstrip(">").lstrip()
        if not remainder or remainder.startswith("&"):
            continue
        try:
            target = shlex.split(remainder, posix=True)[0]
        except (ValueError, IndexError):
            continue
        if target.lower() not in {"/dev/null", "nul", "null"}:
            return _safe_relpath(target, workdir)
    return None


def is_identifier_filename(name: str) -> bool:
    """C/C++ 标识符或 ``foo.c_str()`` 切片，不能当成 listing 路径。"""

    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    basename = basename.replace(" (missing)", "").strip()
    if not basename:
        return False
    lowered = basename.lower()
    if lowered in _GENERIC_C_NAMES:
        return True
    if _CAMEL_CASE_C.fullmatch(basename):
        return True
    return basename.count(".") > 1 and not lowered.endswith(_COMPOUND_SUFFIXES)


def _looks_like_filename(name: str) -> bool:
    cleaned = name.replace(" (missing)", "").strip()
    if not cleaned or cleaned.startswith("$") or "$" in cleaned:
        return False
    if ".." in cleaned or _LINE_RANGE.search(cleaned):
        return False
    parts = [part for part in cleaned.replace("\\", "/").split("/") if part]
    if any(_VERSION_OR_FLOAT.fullmatch(part) or _DOI_PREFIX.match(part) for part in parts):
        return False
    if is_identifier_filename(cleaned):
        return False
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", cleaned) or "/" in cleaned or "\\" in cleaned)


def _unwrap_exec_result(text: str) -> str:
    """去掉 Hermes/exec 与 shell 的 Exit code / Wall time 包装头，只留命令 stdout。"""

    if not isinstance(text, str) or not text:
        return text
    previous = None
    current = text
    while previous != current:
        previous = current
        current = _EXEC_WRAPPER.sub("", current, count=1)
    return current


_READ_TOOL_CONTENT = re.compile(
    r"\A<path>[^\r\n]+</path>\r?\n<type>file</type>\r?\n"
    r"<content>\r?\n(?P<body>.*?)\r?\n</content>(?:\r?\n|$)",
    re.S,
)
_READ_TOOL_FOOTER = re.compile(
    r"\r?\n(?:\r?\n)?\((?:Showing lines [^\r\n]+|End of file[^\r\n]*)\)\s*$"
)
_NUMBERED_DISPLAY_LINE = re.compile(r"^[ \t]*\d+: ?")


_NATIVE_NUMBERED_LINE = re.compile(r"^[ \t]*(\d+)\t")
_HASH_NUMBERED_LINE = re.compile(r"^[ \t]*(\d+)#(?:[A-Z]{2}|\[[A-Z][A-Z0-9_]*\]):")
_PI_READ_FOOTER = re.compile(r"\r?\n(?:\r?\n)?\[Showing lines [^\r\n]+\]\s*$")
_READ_TOTAL = re.compile(r"(?:Showing lines (\d+)-(\d+) of (\d+)|End of file - total (\d+) lines)")


def _read_tool_observation(text: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """展示行号优先于请求 offset；只有明确总行数才能证明分段已覆盖整文件。"""

    partial = any(key in arguments for key in ("offset", "limit", "line_start", "line_end"))
    if arguments.get("raw") is True:
        return {"content": text, "partial": partial}
    text = _unwrap_exec_result(text)
    result: dict[str, Any] = {"content": text, "partial": partial}
    wrapped = text.startswith("<path>")
    total: int | None = None
    range_valid = True
    if wrapped:
        match = _READ_TOOL_CONTENT.match(text)
        if match is None:
            return {"content": None, "partial": True}
        body = match.group("body")
        footer = _READ_TOOL_FOOTER.search(body)
        declared = _READ_TOTAL.search(footer.group(0)) if footer else None
        if declared:
            total = int(declared.group(3) or declared.group(4))
        body = _READ_TOOL_FOOTER.sub("", body)
        prefix = re.compile(r"^[ \t]*(\d+): ?")
    elif _HASH_NUMBERED_LINE.match(text):
        footer = _PI_READ_FOOTER.search(text)
        declared = _READ_TOTAL.search(footer.group(0)) if footer else None
        if declared:
            total = int(declared.group(3))
        body = _PI_READ_FOOTER.sub("", text)
        prefix = _HASH_NUMBERED_LINE
    else:
        body = text
        declared = None
        prefix = _NATIVE_NUMBERED_LINE
    lines = body.splitlines(keepends=True)
    matches = [prefix.match(line) for line in lines]
    if not lines or not all(matches):
        if wrapped or prefix is _HASH_NUMBERED_LINE:
            return {"content": None, "partial": True}
        return result
    numbers = [int(match.group(1)) for match in matches if match is not None]
    if any(number < 1 for number in numbers) or any(
        right <= left for left, right in zip(numbers, numbers[1:])
    ):
        return {"content": None, "partial": True}
    contents = [line[match.end():] for line, match in zip(lines, matches) if match is not None]
    content = "".join(contents)
    if (wrapped or prefix is _HASH_NUMBERED_LINE) and content and not content.endswith("\n"):
        content += "\n"
    if total is not None:
        range_valid = numbers[-1] <= total
        if declared and declared.group(1):
            range_valid = range_valid and (
                numbers[0] == int(declared.group(1)) and numbers[-1] == int(declared.group(2))
            )
        elif declared:
            range_valid = range_valid and numbers[-1] == total
    complete = (
        range_valid and total is not None
        and numbers == list(range(1, total + 1))
    )
    result.update({
        "content": content,
        "partial": not complete,
        "line_numbers": numbers,
        "line_contents": [line.rstrip("\r\n") for line in contents],
        "total_lines": total,
        "range_valid": range_valid,
    })
    return result


def _merge_read_segments(
    segments: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """只合并连续、重叠一致且总行数一致的改动前观察；缺口绝不补造。"""

    observed: dict[int, str] = {}
    totals: set[int] = set()
    for segment in segments:
        if not segment.get("range_valid", True):
            return None, "read_segment_range_mismatch"
        total = segment.get("total_lines")
        if isinstance(total, int):
            totals.add(total)
        for number, content in zip(segment["line_numbers"], segment["line_contents"]):
            if number in observed and observed[number] != content:
                return None, "read_segment_conflict"
            observed[number] = content
    if len(totals) > 1:
        return None, "read_segment_total_conflict"
    if not totals:
        return None, None
    total = next(iter(totals))
    if any(number > total for number in observed):
        return None, "read_segment_range_mismatch"
    if sorted(observed) != list(range(1, total + 1)):
        return None, None
    return "".join(observed[number] + "\n" for number in range(1, total + 1)), None


def _normalize_numbered_display(text: str, command: str | None = None) -> str:
    """只对明确的 PowerShell 行号格式还原展示前缀，保留源码缩进。"""

    if not text or not isinstance(command, str):
        return text
    if not re.search(r"\{0(?:,\s*\d+)?\}: \{1\}.*?-f\b", command, re.I):
        return text
    lines = text.splitlines(keepends=True)
    numbered = [line for line in lines if line.strip()]
    if not numbered or not all(_NUMBERED_DISPLAY_LINE.match(line) for line in numbered):
        return text
    return "".join(_NUMBERED_DISPLAY_LINE.sub("", line, count=1) for line in lines)


def _looks_like_numbered_excerpt(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()][:8]
    if len(lines) < 3:
        return False
    return sum(1 for line in lines if _NUMBERED_LINE.match(line)) >= 3


def _isolate_single_file_content(path: str, text: str) -> str:
    """Promise.all 会把 Get-Content 和 rg/Get-Item 拼进同一段 result_text。"""

    text = _unwrap_exec_result(text)
    target = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for match in _RG_HIT.finditer(text):
        other = match.group("path").replace("\\", "/")
        other_name = other.rsplit("/", 1)[-1].lower()
        if other_name != target and not other.lower().endswith("/" + target):
            isolated = text[: match.start()].rstrip()
            return isolated + ("\n" if isolated else "")
    return text


def split_result_segments(text: str) -> list[str] | None:
    """拆 ``--- result 1 ---`` / ``--- 1 ---``。段数必须从 1 连续编号。"""

    if not isinstance(text, str) or "---" not in text:
        return None
    matches = list(_RESULT_SEGMENT.finditer(text))
    if len(matches) < 2:
        return None
    numbers = [int(match.group(1)) for match in matches]
    if numbers != list(range(1, len(numbers) + 1)):
        return None
    bodies: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        bodies.append(text[match.end() : end].lstrip("\r\n"))
    return bodies


def _looks_like_listing_blob(text: str) -> bool:
    """rg 报错或目录表不能当成唯一 Get-Content 的文件正文。"""

    body = (text or "").lstrip("\r\n")
    if not body:
        return False
    if body.startswith("rg: "):
        return True
    has_table = bool(
        _LISTING_NAME_HEADER.search(body)
        and ("LastWriteTime" in body or re.search(r"(?m)^----", body))
    )
    if not has_table:
        return False
    numbered = sum(1 for line in body.splitlines() if _NUMBERED_LINE.match(line))
    return numbered < 3


_NUMBERED_VALUE = re.compile(r"^\s*(\d+):")


def _numbered_line_values(text: str) -> list[int]:
    values: list[int] = []
    for line in (text or "").splitlines():
        match = _NUMBERED_VALUE.match(line)
        if match:
            values.append(int(match.group(1)))
    return values


def _has_numbered_line_reset(text: str) -> bool:
    """行号回跳说明 stdout 拼了多个 Get-Content 切片，不能绑到单一路径。"""

    values = _numbered_line_values(text)
    if len(values) < 6:
        return False
    previous = values[0]
    for value in values[1:]:
        if value < previous:
            return True
        previous = value
    return False


def _trim_trailing_unnumbered_noise(text: str) -> str:
    """具名 dump 后面的 dumpbin / rg 尾巴不属于最后一个文件。"""

    if not _looks_like_numbered_excerpt(text):
        return text
    lines = text.splitlines(keepends=True)
    last = -1
    for index, line in enumerate(lines):
        if _NUMBERED_LINE.match(line):
            last = index
    if last < 0:
        return text
    tail = lines[last + 1 :]
    if tail and any(line.strip() and not _NUMBERED_LINE.match(line) for line in tail):
        return "".join(lines[: last + 1])
    return text


def _untrusted_single_read(content: str) -> bool:
    return _looks_like_listing_blob(content) or _has_numbered_line_reset(content)


def _unknown_exec_op(
    event_id: str, commands: list[str], workdir: str | None
) -> list[dict[str, Any]]:
    return [
        {
            "kind": "unknown",
            "event_id": event_id,
            "command": "\n".join(commands),
            "workdir": workdir,
        }
    ]


def split_named_dumps(text: str) -> dict[str, str]:
    """拆 ``--- file.md ---`` 这种一次读多文件的回显。"""

    if not isinstance(text, str) or "---" not in text:
        return {}
    pattern = re.compile(r"^---\s+([^\r\n]+?)\s+---\s*$", re.M)
    matches = [
        match
        for match in pattern.finditer(text)
        if _looks_like_filename(match.group(1))
    ]
    if len(matches) < 2:
        return {}
    dumps: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1).replace(" (missing)", "").strip()
        if name.endswith("(missing)"):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = _trim_trailing_unnumbered_noise(text[match.end() : end].lstrip("\r\n"))
        if body:
            dumps[name] = body
    return dumps


def _unquote(value: str) -> str:
    return value.strip().strip("\"'")


def _powershell_assignments(command: str) -> dict[str, str]:
    return {
        match.group(1).lower(): match.group(3)
        for match in _PS_VAR_ASSIGN.finditer(command or "")
    }


def _resolve_ps_path(raw: str, assignments: dict[str, str]) -> str | None:
    token = _unquote(raw)
    if not token:
        return None
    if token.startswith("$"):
        match = _PS_VAR_REF.match(token)
        if match is None:
            return None
        return assignments.get(match.group(1).lower())
    return token


def _get_content_paths(command: str) -> list[str]:
    assignments = _powershell_assignments(command)
    raw: list[str] = []
    for match in _GET_CONTENT_PATH.finditer(command):
        raw.append(_unquote(match.group("path")))
    if not raw:
        for match in _GET_CONTENT_BARE.finditer(command):
            candidate = match.group("path")
            if candidate.startswith("-"):
                continue
            raw.append(_unquote(candidate))
    paths: list[str] = []
    seen: set[str] = set()
    for item in raw:
        resolved = _resolve_ps_path(item, assignments)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        paths.append(resolved)
    return paths


_READONLY_EXECUTABLES = frozenset(
    {
        "cat", "head", "tail", "type", "ls", "pwd", "rg", "grep", "find", "stat",
        "wc", "file", "sha256sum", "md5sum", "du", "sed", "echo", "printf", "sleep",
        "wait", "true", "false", "cd", "exit", "get-content", "get-childitem",
        "get-item", "get-command", "get-filehash", "get-location", "set-location",
        "select-object", "where-object", "foreach-object", "measure-object",
        "format-table", "format-list", "write-output", "convertfrom-json",
        "convertto-json", "for", "foreach", "if", "else", "dumpbin.exe", "dumpbin",
        "sort-object", "test-path", "out-null", "select-string",
    }
)
_CL_SYNTAX_CHECK = re.compile(r"(?i)\bcl(?:\.exe)?\b")
_ZS_FLAG = re.compile(r"(?i)(?:^|[\s/])Zs\b")
_READONLY_GIT = frozenset(
    {"diff", "status", "log", "show", "ls-files", "blame", "grep", "rev-parse"}
)
_COMMAND_HEAD = re.compile(
    r"(?:\A|[;|&{}(\n])\s*(?:\$[A-Za-z_]\w*\s*=\s*)?([A-Za-z_][A-Za-z0-9_.-]*)"
)
_NODE_INVOCATION = re.compile(r"(?i)\bnode(?:\.exe)?\s+(\S+)")
_GIT_SUBCOMMAND = re.compile(r"(?i)\bgit\s+([A-Za-z][A-Za-z0-9-]+)")
_EXPORT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_EXPORT_VALUE = re.compile(r"^(?:[A-Za-z0-9_./:+,@%=-]*|'[^'$]*'|\"[^\"$]*\")$")
_SHELL_SUBSTITUTION_MARKERS = ("$(", chr(96), "<(")


def _split_shell_separator(command: str) -> tuple[str, str] | None:
    """Split the first unquoted shell separator, retaining the right side."""

    quote: str | None = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in {";", "&", "|", "\n"}:
            right = command[index + 1 :]
            if char in {"&", "|"} and right.startswith(char):
                right = right[1:]
            return command[:index], right
    return None


def _strip_safe_export_prefixes(command: str) -> str | None:
    """Remove only literal export NAME=value prefixes."""

    text = (command or "").strip()
    while text.lower().startswith("export") and (
        len(text) == 6 or text[6].isspace()
    ):
        rest = text[6:].lstrip()
        split = _split_shell_separator(rest)
        if split is None:
            try:
                assignments = shlex.split(rest, posix=True)
            except ValueError:
                return None
            if not assignments or any(
                not _EXPORT_NAME.match(token)
                or any(marker in token for marker in _SHELL_SUBSTITUTION_MARKERS)
                or not _EXPORT_VALUE.fullmatch(token.split("=", 1)[1])
                for token in assignments
            ):
                return None
            return ""
        assignment_blob, remainder = split
        try:
            assignments = shlex.split(assignment_blob.strip(), posix=True)
        except ValueError:
            return None
        if not assignments or any(
            not _EXPORT_NAME.match(token)
            or any(marker in token for marker in _SHELL_SUBSTITUTION_MARKERS)
            or not _EXPORT_VALUE.fullmatch(token.split("=", 1)[1])
            for token in assignments
        ):
            return None
        text = remainder.lstrip()
    return text



def _has_write_signal(command: str) -> bool:
    """只有重定向或明确写工具才算写；node --check / git diff 不算。"""

    text = command or ""
    return _redirect_target(text) is not None or bool(_MUTATION_TOKEN.search(text))


def _is_msvc_syntax_check(command: str) -> bool:
    """cl /Zs 只做语法检查，不写源码；cmd 包一层也不算 mutation。"""

    text = command or ""
    return bool(_CL_SYNTAX_CHECK.search(text) and _ZS_FLAG.search(text) and not _has_write_signal(text))


def _head_is_readonly(head: str, command: str) -> bool:
    if head in _READONLY_EXECUTABLES:
        return True
    if head in {"node", "node.exe"}:
        invocations = _NODE_INVOCATION.findall(command)
        return bool(invocations) and all(
            token == "--check" for token in invocations
        )
    if head == "git":
        subs = [item.lower() for item in _GIT_SUBCOMMAND.findall(command)]
        return bool(subs) and all(item in _READONLY_GIT for item in subs)
    return False


def _unknown_looks_like_mutation(command: str) -> bool:
    """只有可识别的只读命令免于屏障；任意脚本不能靠缺少写关键词获信任。"""

    text = command or ""
    normalized = _strip_safe_export_prefixes(text)
    if normalized is None:
        return True
    if not normalized:
        return False
    # Keep substitutions inside quotes untrusted; they can still execute.
    if any(marker in text for marker in _SHELL_SUBSTITUTION_MARKERS):
        return True
    text = normalized
    if _has_write_signal(text):
        return True
    if _is_msvc_syntax_check(text):
        return False
    # 不解释命令替换、动态调用或对象方法；它们的副作用不能由路径抽取限定。
    unquoted = _UNKNOWN_QUOTED.sub('""', text)
    if re.search(r"\$\(|`|<\(|::|\.[A-Za-z_]\w*\s*\(", unquoted):
        return True
    heads = [match.group(1).lower() for match in _COMMAND_HEAD.finditer(unquoted)]
    if not heads or any(not _head_is_readonly(head, unquoted) for head in heads):
        return True
    if "find" in heads and re.search(r"-(?:exec|execdir|ok|okdir|delete|fprint|fprintf)\b", unquoted):
        return True
    # sed 只接受显式的区间打印。w/e/in-place 等脚本即使输出像文件也有副作用。
    if "sed" in heads:
        # 在引号外分段，避免把 rg 的模式或 sed 的脚本当成独立 shell 命令。
        lexer = shlex.shlex(text, posix=True, punctuation_chars="|;&")
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            tokens = list(lexer)
        except ValueError:
            return True
        segments: list[list[str]] = [[]]
        for token in tokens:
            if token in {"|", "||", ";", "&&", "&"}:
                segments.append([])
            else:
                segments[-1].append(token)
        for segment in segments:
            if not segment or Path(segment[0]).name != "sed":
                continue
            if not (
                len(segment) in {3, 4}
                and segment[1] == "-n"
                and re.fullmatch(r"[0-9]+(?:,[0-9]+)?p", segment[2])
                and (len(segment) == 3 or not segment[3].startswith("-"))
            ):
                return True
    return False


def _sed_path(command: str) -> str | None:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens or Path(tokens[0]).name != "sed":
        return None
    files = [
        token
        for token in tokens[1:]
        if token != "--"
        and not token.startswith("-")
        and not re.fullmatch(r"[\d,]+[pPd]", token)
    ]
    if len(files) != 1:
        return None
    return files[0]


def _posix_read_path(command: str) -> tuple[str | None, bool]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None, False
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None, False
    executable = Path(tokens[0]).name
    if executable not in _READ_COMMANDS:
        return None, False
    files = [token for token in tokens[1:] if token != "--" and not token.startswith("-")]
    if len(files) != 1:
        return None, False
    flagged = executable in {"head", "tail"} or any(
        token.startswith("-") and token != "--" for token in tokens[1:]
    )
    return files[0], flagged


def _classify_command(
    command: str,
    *,
    event_id: str,
    result_text: Any,
    workdir: str | None,
) -> list[dict[str, Any]]:
    normalized = _strip_safe_export_prefixes(command)
    if normalized is None:
        return _unknown_exec_op(event_id, [command], workdir)
    if not normalized:
        return []
    command = normalized
    redirect = _redirect_target(command, workdir)
    if redirect:
        return [
            {
                "kind": "write",
                "path": redirect,
                "event_id": event_id,
                "command": command,
                "workdir": workdir,
            }
        ]
    if _unknown_looks_like_mutation(command):
        return _unknown_exec_op(event_id, [command], workdir)
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = []
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens = tokens[1:]
    executable = Path(tokens[0]).name if tokens else ""
    if executable in _IGNORE_COMMANDS and not _GET_CONTENT.search(command):
        return []

    reads: list[tuple[str, bool]] = []
    posix_path, posix_partial = _posix_read_path(command)
    if posix_path:
        reads.append((posix_path, posix_partial))
    sed_path = _sed_path(command)
    if sed_path:
        reads.append((sed_path, True))
    if _GET_CONTENT.search(command) and _JSON_TRANSFORM_PIPE.search(command):
        return []
    if _GET_CONTENT.search(command):
        sliced = bool(_LINE_RANGE.search(command) or re.search(r"\$\w+\s*\[", command))
        for path in _get_content_paths(command):
            piped = "|" in command or "-Tail" in command or "-Head" in command
            reads.append((path, piped or sliced))

    unique: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for path, partial in reads:
        safe = _safe_relpath(path, workdir)
        if safe is None or safe in seen:
            continue
        seen.add(safe)
        unique.append((safe, partial))
    if len(unique) == 1:
        path, partial = unique[0]
        content = result_text if isinstance(result_text, str) else ""
        content = _isolate_single_file_content(path, content)
        return [
            {
                "kind": "read",
                "path": path,
                "event_id": event_id,
                "partial": partial or _looks_like_numbered_excerpt(content),
                "content": content,
                "command": command,
                "workdir": workdir,
            }
        ]
    if unique:
        return [{"kind": "unknown", "event_id": event_id, "command": command, "workdir": workdir}]
    if executable in _IGNORE_COMMANDS:
        return []
    return [{"kind": "unknown", "event_id": event_id, "command": command, "workdir": workdir}]


def _normalize_exec(item: dict[str, Any], workdir: str | None = None) -> list[dict[str, Any]]:
    event_id = str(item.get("call_id") or "unknown")
    workdir = workdir or _item_workdir(item)
    commands = _item_commands(item)
    raw_result = item.get("result_text") or ""
    if isinstance(raw_result, str):
        raw_result = _unwrap_exec_result(raw_result)
    # 真写（Set-Content / 重定向）后的具名 header 不能当初始文件。
    # 同 exec 里的只读探测（dumpbin / Select-String）不毒掉另一条
    # Get-Content 已经读出的 ``--- file ---`` 正文。
    dumps = (
        split_named_dumps(raw_result)
        if commands and not any(_has_write_signal(command) for command in commands)
        and any(re.search(r"\b(?:Get-Content|cat|head|tail|type|sed)\b", command, re.I) for command in commands)
        else {}
    )
    if dumps:
        named: list[dict[str, Any]] = []
        for name, content in dumps.items():
            path = _safe_relpath(name, workdir)
            if path is None:
                continue
            named.append(
                {
                    "kind": "read",
                    "path": path,
                    "event_id": event_id,
                    "partial": _looks_like_numbered_excerpt(content),
                    "content": content,
                    "command": commands[0] if commands else "",
                }
            )
        if named:
            return named
    if not commands:
        return [{"kind": "unknown", "event_id": event_id, "command": "", "workdir": workdir}]
    segments = split_result_segments(raw_result)
    if segments is not None:
        if len(segments) != len(commands):
            return _unknown_exec_op(event_id, commands, workdir)
        aligned: list[dict[str, Any]] = []
        for command, segment in zip(commands, segments):
            aligned.extend(
                _classify_command(
                    command,
                    event_id=event_id,
                    result_text=segment,
                    workdir=workdir,
                )
            )
        trusted = [
            op
            for op in aligned
            if not (
                op.get("kind") == "read"
                and _untrusted_single_read(
                    op["content"] if isinstance(op.get("content"), str) else ""
                )
            )
        ]
        if any(op.get("kind") in {"read", "write"} for op in trusted):
            return trusted
        if not aligned:
            return []
        return _unknown_exec_op(event_id, commands, workdir)
    ops: list[dict[str, Any]] = []
    for command in commands:
        ops.extend(
            _classify_command(
                command,
                event_id=event_id,
                result_text=item.get("result_text"),
                workdir=workdir,
            )
        )
    reads = [op for op in ops if op.get("kind") == "read"]
    writes = [op for op in ops if op.get("kind") == "write"]
    unknown_mutation = any(
        op.get("kind") == "unknown" and _unknown_looks_like_mutation(str(op.get("command") or ""))
        for op in ops
    )
    if unknown_mutation:
        return _unknown_exec_op(event_id, commands, workdir)
    if len(reads) == 1 and not writes:
        content = reads[0].get("content") if isinstance(reads[0].get("content"), str) else ""
        if _untrusted_single_read(content):
            return _unknown_exec_op(event_id, commands, workdir)
        reads[0]["content"] = _trim_trailing_unnumbered_noise(content)
        return reads
    if len(writes) == 1 and not reads:
        return writes
    if not ops:
        return []
    return _unknown_exec_op(event_id, commands, workdir)


def _session_workdir(timeline: list[dict[str, Any]]) -> str | None:
    counts: Counter[str] = Counter()
    for item in timeline:
        if not isinstance(item, dict):
            continue
        workdir = _item_workdir(item)
        if workdir:
            counts[workdir] += 1
    if counts:
        return counts.most_common(1)[0][0]
    return None


def _usable_tool_result(item: dict[str, Any]) -> bool:
    """只把已返回且非错误的工具结果作为环境事实。"""
    if item.get("pending") is True or item.get("is_error") is True or item.get("cleared") is True:
        return False
    status = str(item.get("status") or item.get("result_status") or "").lower()
    if status in {"error", "failed", "failure", "cancelled", "timeout", "cleared"}:
        return False
    result = item.get("result_text")
    if isinstance(result, str) and result.lstrip().lower().startswith(("error:", "file not found:", "command failed", "traceback", "<tool_use_error>", "[tool result content cleared]")):
        return False
    return True


def _infer_workspace_root(timeline: list[dict[str, Any]]) -> str | None:
    """Infer a common absolute project root before stripping host prefixes."""
    parents: list[str] = []
    for item in timeline:
        args = item.get("arguments") if isinstance(item, dict) else None
        if not isinstance(args, dict):
            continue
        for key in ("path", "file_path", "filePath", "filename", "file"):
            value = args.get(key)
            if not isinstance(value, str):
                continue
            raw = value.strip().replace("\\", "/")
            if not re.match(r"^(?:[A-Za-z]:/|/)", raw):
                continue
            parent = str(PurePosixPath(raw).parent)
            if parent not in {".", "/"} and parent not in parents:
                parents.append(parent)
    if len(parents) < 2:
        return None
    try:
        common = os.path.commonpath(parents).replace("\\", "/")
    except ValueError:
        return None
    if len(PurePosixPath(common).parts) < 3 or common in {"/", "."}:
        return None
    return common.rstrip("/")


def replay_workspace_root(
    timeline: list[dict[str, Any]], *, workspace_root: str | None = None,
) -> str | None:
    """Replay 和环境绑定共用工作目录规则，避免同一文件出现两套坐标。"""
    return _session_workdir(timeline) or workspace_root or _infer_workspace_root(timeline)


def normalize_file_ops(
    timeline: list[dict[str, Any]], *, workspace_root: str | None = None
) -> list[dict[str, Any]]:
    """把 Claude 文件工具和 exec cat/sed/Get-Content 收成同一条 read/write 流。"""

    # Some captured sessions carry absolute paths but omit cwd on every tool
    # event. Infer one common project root before normalising; otherwise the
    # host prefix becomes a fake workspace directory and every downstream
    # binding is wrong.
    session_workdir = replay_workspace_root(timeline, workspace_root=workspace_root)
    ops: list[dict[str, Any]] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        usable_result = _usable_tool_result(item)
        name = str(item.get("name") or "").lower()
        if not usable_result:
            if (name in {"read", "read_file"} and item.get("pending") is not True
                    and item.get("cleared") is not True
                    and str(item.get("result_text") or "").lstrip().lower().startswith("file not found:")):
                path = _path_from_args(item.get("arguments"), _item_workdir(item) or session_workdir)
                if path:
                    ops.append({"kind": "absent", "path": path,
                                "event_id": str(item.get("call_id") or "unknown")})
                continue
            if name in {"exec", "bash", "shell", "terminal", "command", "powershell"}:
                unknown = dict(item)
                unknown["result_text"] = ""
                candidates = _normalize_exec(unknown, _item_workdir(item) or session_workdir)
                ops.extend(
                    op for op in candidates
                    if op.get("kind") == "write"
                    or (op.get("kind") == "unknown" and _unknown_looks_like_mutation(str(op.get("command") or "")))
                )
                continue
            if not (any(token in name for token in ("write", "edit", "patch", "replace", "create")) or name in {"delete", "remove"}):
                continue
        event_id = str(item.get("call_id") or "unknown")
        workdir = _item_workdir(item) or session_workdir
        if name in {"read", "read_file"}:
            path = _path_from_args(item.get("arguments"), workdir)
            if path is None:
                continue
            args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            observation = (
                _read_tool_observation(item["result_text"], args)
                if isinstance(item.get("result_text"), str)
                else {"content": None, "partial": True}
            )
            ops.append(
                {
                    "kind": "read",
                    "path": path,
                    "event_id": event_id,
                    **observation,
                }
            )
            continue
        if any(token in name for token in ("write", "edit", "patch", "replace", "create")):
            args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            content = next(
                (args[key] for key in ("content", "contents", "new_content")
                 if isinstance(args.get(key), str)),
                None,
            )
            ops.append(
                {
                    "kind": "write",
                    "path": _path_from_args(args, workdir),
                    "event_id": event_id,
                    "final_content": content,
                }
            )
            continue
        if name in {"delete", "remove"}:
            ops.append(
                {
                    "kind": "write",
                    "path": _path_from_args(item.get("arguments"), workdir),
                    "event_id": event_id,
                }
            )
            continue
        if name in {"exec", "bash", "shell", "terminal", "command", "powershell"}:
            ops.extend(_normalize_exec(item, workdir))
            continue
        if name in {"wait"}:
            continue
    return ops


_WRITE_PATH_FLAG = re.compile(
    r"(?i)(?:Set-Content|Add-Content|Out-File|Remove-Item|New-Item|Move-Item|Copy-Item)"
    r"\b[^|;]*-(?:LiteralPath|Path)\s+(?P<path>'[^']+'|\"[^\"]+\"|\S+)"
)
_COPY_MOVE = re.compile(r"(?i)\b(?:cp|copy|mv|move|Copy-Item|Move-Item)\b")
_SED_INPLACE = re.compile(r"(?i)\bsed\b.*\s-i\b")


def _paths_from_unknown_command(command: str, workdir: str | None = None) -> list[str]:
    """只抽未知命令里的写目标。Get-Content / cl / rg 里出现的文件名不是 mutation。"""

    if not isinstance(command, str) or not command.strip():
        return []
    candidates: list[str] = []
    for match in _UNKNOWN_PATH_CTOR.finditer(command):
        candidates.append(match.group("path"))
    for match in _WRITE_PATH_FLAG.finditer(command):
        candidates.append(_unquote(match.group("path")))
    redirect = _redirect_target(command, workdir)
    if redirect:
        candidates.append(redirect)
    if _SED_INPLACE.search(command):
        for match in _UNKNOWN_QUOTED.finditer(command):
            token = match.group(2)
            if token in _UNKNOWN_SKIP_TOKENS:
                continue
            if _looks_like_filename(token):
                candidates.append(token)
        for match in _UNKNOWN_BARE_FILE.finditer(command):
            token = match.group(1)
            if token in _UNKNOWN_SKIP_TOKENS or not _looks_like_filename(token):
                continue
            candidates.append(token)
    if _COPY_MOVE.search(command):
        names = [
            _unquote(match.group(2))
            for match in _UNKNOWN_QUOTED.finditer(command)
            if _looks_like_filename(match.group(2))
        ]
        names.extend(
            match.group(1)
            for match in _UNKNOWN_BARE_FILE.finditer(command)
            if _looks_like_filename(match.group(1))
        )
        if names:
            candidates.append(names[-1])
    found: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        if isinstance(raw, str) and raw.lower() in {"/dev/null", "nul", "null"}:
            continue
        if not _looks_like_filename(str(raw)):
            continue
        safe = _safe_relpath(str(raw), workdir)
        if safe is None or safe in seen:
            continue
        seen.add(safe)
        found.append(safe)
    return found


def foreign_mutation_paths(
    timeline: list[dict[str, Any]], selected_span_ids: set[str]
) -> tuple[set[str], set[str]]:
    """测试夹具，非主路径。环境回放不按 span 切；主链用完整 tool_timeline 的全局 mutation。"""

    mutated: set[str] = set()
    untrusted: set[str] = set()
    foreign = [
        item
        for item in timeline
        if isinstance(item, dict)
        and item.get("span_id") is not None
        and str(item.get("span_id")) not in selected_span_ids
    ]
    for op in normalize_file_ops(foreign):
        path = op.get("path")
        if op.get("kind") == "write" and isinstance(path, str) and path:
            mutated.add(path)
            continue
        if op.get("kind") == "unknown":
            command = str(op.get("command") or "")
            if not _unknown_looks_like_mutation(command):
                continue
            workdir = op.get("workdir") if isinstance(op.get("workdir"), str) else None
            untrusted.update(_paths_from_unknown_command(command, workdir))
    return mutated, untrusted


def _events_before_selected(
    timeline: list[dict[str, Any]], selected_span_ids: set[str]
) -> list[dict[str, Any]]:
    prefix: list[dict[str, Any]] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        span_id = item.get("span_id")
        if span_id is not None and str(span_id) in selected_span_ids:
            break
        prefix.append(item)
    return prefix


def prior_visible_files(
    timeline: list[dict[str, Any]], selected_span_ids: set[str]
) -> tuple[ReplayedFile, ...]:
    """测试夹具，非主路径。环境回放不按 span 切；更早只读由完整 tool_timeline 直接种入。"""

    prefix = _events_before_selected(timeline, selected_span_ids)
    if not prefix:
        return ()
    prior = replay_from_timeline(prefix)
    blocked = {item.path for item in prior.withheld_changes if item.path}
    blocked.update(
        str(item.get("path"))
        for item in prior.partial_evidence
        if isinstance(item, dict)
        and item.get("reason")
        in {"unparsed_mutation_scope", "read_after_unparsed_mutation"}
        and isinstance(item.get("path"), str)
        and item.get("path")
    )
    visible: list[ReplayedFile] = []
    for item in prior.files:
        if item.path in blocked:
            continue
        visible.append(
            ReplayedFile(
                item.path,
                item.content,
                item.first_observation_event_id,
                item.completeness,
                "PRIOR_VISIBLE_OBSERVATION",
            )
        )
    return tuple(visible)


def replay_selected_environment(
    timeline: list[dict[str, Any]],
    selected_span_ids: set[str] | None = None,
    destination: str | Path | None = None,
) -> ReplayResult:
    """兼容入口：环境是 session 级的，不按 selected span 切。"""

    del selected_span_ids
    return replay_from_timeline(timeline, destination)


def replay_from_timeline(
    timeline: list[dict[str, Any]],
    destination: str | Path | None = None,
    *,
    prior_mutated_paths: set[str] | None = None,
    prior_untrusted_paths: set[str] | None = None,
    prior_files: tuple[ReplayedFile, ...] | list[ReplayedFile] | None = None,
) -> ReplayResult:
    """按论文 B.1 回放最早观察；仅用屏障前一致的分段补齐同一路径。"""

    observed: dict[str, ReplayedFile] = {}
    segments_by_path: dict[str, list[dict[str, Any]]] = {}
    for item in prior_files or ():
        if isinstance(item, ReplayedFile) and item.path:
            observed[item.path] = item
    mutations: list[WithheldChange] = []
    partial: list[dict[str, Any]] = []
    barriers: list[str] = []
    mutated: set[str] = set(prior_mutated_paths or ())
    untrusted: set[str] = set(prior_untrusted_paths or ())
    unresolved_mutation = False
    for op in normalize_file_ops(timeline, workspace_root=_infer_workspace_root(timeline)):
        kind = op["kind"]
        event_id = str(op.get("event_id") or "unknown")
        workdir = op.get("workdir") if isinstance(op.get("workdir"), str) else None
        if kind == "unknown":
            barriers.append(event_id)
            command = str(op.get("command") or "")
            if not _unknown_looks_like_mutation(command):
                partial.append(
                    {
                        "path": None,
                        "reason": "unparsed_readonly",
                        "source_event_id": event_id,
                    }
                )
                continue
            # 未知执行的提取路径不是完整写集合；新的观察只能留作待核实证据。
            unresolved_mutation = True
            extracted = _paths_from_unknown_command(command, workdir)
            if extracted:
                untrusted.update(extracted)
                for path in extracted:
                    partial.append(
                        {
                            "path": path,
                            "reason": "unparsed_mutation_scope",
                            "source_event_id": event_id,
                        }
                    )
            else:
                partial.append(
                    {
                        "path": None,
                        "reason": "unparsed_mutation_unscoped",
                        "source_event_id": event_id,
                    }
                )
            continue
        path = op.get("path")
        if not isinstance(path, str) or not path:
            barriers.append(event_id)
            continue
        if kind == "write":
            existed = path in observed
            mutations.append(
                WithheldChange(
                    path,
                    event_id,
                    "write",
                    "withheld_change" if existed else "agent_created_file",
                    existed,
                    final_content=(
                        op.get("final_content")
                        if isinstance(op.get("final_content"), str)
                        else None
                    ),
                )
            )
            if existed:
                # 论文的初始环境是改动前观察到的字节；后续写入属于 withheld
                # change，不能把一个完整观察降级为 PARTIAL。
                partial.append(
                    {
                        "path": path,
                        "reason": "modified_after_observation",
                        "source_event_id": event_id,
                    }
                )
            mutated.add(path)
            continue
        if path in mutated:
            partial.append(
                {
                    "path": path,
                    "reason": "read_after_first_mutation",
                    "source_event_id": event_id,
                }
            )
            continue
        if path in untrusted or unresolved_mutation:
            evidence: dict[str, Any] = {
                "path": path,
                "reason": "read_after_unparsed_mutation",
                "source_event_id": event_id,
            }
            text = op.get("content")
            if isinstance(text, str):
                evidence["observed_chars"] = len(text)
                evidence["content"] = text
            partial.append(evidence)
            continue
        if kind == "absent":
            if path not in observed:
                partial.append({"path": path, "reason": "initial_read_not_found",
                                "source_event_id": event_id})
            continue
        if isinstance(op.get("content"), str) and isinstance(op.get("line_numbers"), list):
            segment = {
                "path": path,
                "reason": "read_segment",
                "source_event_id": event_id,
                "content": op["content"],
                "line_numbers": op["line_numbers"],
                "line_contents": op["line_contents"],
                "total_lines": op.get("total_lines"),
                "range_valid": op.get("range_valid", True),
            }
            partial.append(segment)
            segments = segments_by_path.setdefault(path, [])
            segments.append(segment)
            merged, issue = _merge_read_segments(segments)
            prior = observed.get(path)
            if issue:
                partial.append({
                    "path": path, "reason": issue, "source_event_id": event_id,
                    "source_event_ids": [item["source_event_id"] for item in segments],
                })
                if prior is not None:
                    observed[path] = ReplayedFile(
                        path, prior.content, prior.first_observation_event_id, "PARTIAL",
                        prior.provenance,
                    )
            elif merged is not None and (
                prior is None or prior.first_observation_event_id == segments[0]["source_event_id"]
            ):
                observed[path] = ReplayedFile(
                    path, merged, segments[0]["source_event_id"], "COMPLETE",
                )
        if path in observed:
            continue
        text = op.get("content")
        if not isinstance(text, str):
            partial.append(
                {"path": path, "reason": "read_result_missing", "source_event_id": event_id}
            )
            continue
        # 工具适配阶段已处理输出包装；再次解包会改写 raw 读取的源码字节。
        text = _normalize_numbered_display(text, str(op.get("command") or ""))
        completeness = "PARTIAL" if op.get("partial") else "COMPLETE"
        if completeness == "PARTIAL":
            partial.append(
                {"path": path, "reason": "partial_read_range", "source_event_id": event_id}
            )
        observed[path] = ReplayedFile(path, text, event_id, completeness)

    result = ReplayResult(
        tuple(observed[path] for path in sorted(observed)),
        tuple(mutations),
        tuple(partial),
        tuple(barriers),
        _infer_workspace_root(timeline),
    )
    if destination is not None:
        root = Path(destination)
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        for item in result.files:
            target = root / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.content, encoding="utf-8")
    return result


def write_replay_artifacts(result: ReplayResult, output_dir: str | Path) -> Path:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["schema_version"] = REPLAY_FROM_SOURCE_SCHEMA
    payload["policy"] = {
        "engine": "stage1_from_raw_timeline",
        "compile": False,
        "trajectory_replay": False,
        "shell_execution": "FORBIDDEN",
        "barrier_scope": "unparsed_exec_only",
        "mutation_scope": "per_path",
        "untrusted_scope": "extracted_paths_from_unparsed_mutation_only",
        "js_exec_unwrap": True,
    }
    path = root / "replay.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
