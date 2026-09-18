"""Deterministic hole index for Terminal-Universe Stage 2 completion."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from traceforge.reconstruction.env_replay import (
    _looks_like_filename,
    _safe_relpath,
    normalize_file_ops,
)
from traceforge.reconstruction.environment_bindings import (
    file_required_paths,
    looks_like_synthetic_stub,
)
from traceforge.reconstruction.terminal_universe_environment import ReplayResult

SUPPORT_BASENAMES = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "requirements.in",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "environment.yml",
        "environment.yaml",
        "conda.yaml",
        "makefile",
        "cmakelists.txt",
        "tsconfig.json",
        "jsconfig.json",
        "tox.ini",
        "pytest.ini",
        "mypy.ini",
        ".editorconfig",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "manifest.in",
        ".python-version",
        "settings.yaml",
        "settings.yml",
        "settings.json",
        "settings.toml",
        "config.yaml",
        "config.yml",
        "config.json",
        "config.toml",
        "config.ini",
        ".env",
        ".env.example",
    }
)
SUPPORT_CANONICAL = {
    "dockerfile": "Dockerfile",
    "makefile": "Makefile",
    "cmakelists.txt": "CMakeLists.txt",
    "pipfile": "Pipfile",
    "pipfile.lock": "Pipfile.lock",
    "gemfile": "Gemfile",
    "gemfile.lock": "Gemfile.lock",
    "cargo.toml": "Cargo.toml",
    "cargo.lock": "Cargo.lock",
}
LISTING_COMMANDS = frozenset(
    {
        "ls",
        "dir",
        "tree",
        "find",
        "rg",
        "grep",
        "glob",
        "fd",
        "get-childitem",
        "gci",
        "list_dir",
    }
)
_FILENAME = re.compile(
    r"(?:^|[\s'\"`=:,(\[])((?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.[A-Za-z0-9]{1,8})"
)
_PATH_LIKE_SUFFIXES = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".m",
        ".mm",
        ".bat",
        ".cmd",
        ".sh",
        ".ps1",
        ".md",
        ".txt",
        ".toml",
        ".yml",
        ".yaml",
        ".json",
        ".ini",
        ".cfg",
        ".lock",
        ".in",
        ".example",
    }
)
_UNTRUSTED_HOLE_REASONS = frozenset(
    {"unparsed_mutation_scope", "read_after_unparsed_mutation"}
)
_DIR_TOKEN = re.compile(r"(?:^|[\s'\"`=:,(\[])((?:[A-Za-z0-9._-]+/){1,6})")
_NAMED_DUMP_HEADER = re.compile(r"^---\s+([^\r\n]+?)\s+---\s*$", re.M)
_RG_HIT_PATH = re.compile(r"^(?P<path>\S[^:\r\n]*):(?P<line>\d+):", re.M)
_SOLE_FILENAME = re.compile(
    r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\.[A-Za-z0-9]{1,8}$"
)
_TABLE_RULE = re.compile(r"^[- ]+$")
_LISTING_TABLE_HEADERS = frozenset(
    {"name", "mode", "lastwritetime", "length", "directory:", "----"}
)
_STUB_SOURCE_SUFFIXES = frozenset(
    {".py", ".js", ".ts", ".cpp", ".cc", ".c", ".h", ".hpp", ".bat", ".cmd", ".cs"}
)
_BINARY_SUFFIXES = frozenset({".obj", ".o", ".exe", ".dll", ".lib", ".pdb", ".a", ".so"})


@dataclass(frozen=True, slots=True)
class CompletionIndex:
    holes: tuple[dict[str, Any], ...]
    listing_names: frozenset[str]
    body_paths: frozenset[str]
    topic_card: dict[str, Any]

    def as_list(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.holes]


def is_support_file(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name in SUPPORT_BASENAMES or name.endswith(".lock")


def is_runtime_log(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() == ".log"


def _needs_real_generated_body(
    path: str,
    *,
    listing_names: set[str] | frozenset[str] | None,
    body_paths: set[str] | frozenset[str] | None = None,
    replay_paths: set[str] | frozenset[str] | None = None,
    required_paths: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None = None,
) -> bool:
    listing = set(listing_names or ())
    bodies = set(body_paths or ())
    replayed = set(replay_paths or ())
    required = {str(item) for item in (required_paths or ()) if not str(item).endswith("/")}
    if path in replayed or path in bodies or PurePosixPath(path).name in bodies:
        return False
    if is_support_file(path) or is_runtime_log(path):
        return False
    name = PurePosixPath(path).name
    if path in required:
        return True
    return bool(listing) and (path in listing or name in listing)


def listing_stub_error(
    path: str,
    content: str,
    *,
    listing_names: set[str] | frozenset[str] | None,
    body_paths: set[str] | frozenset[str] | None = None,
    replay_paths: set[str] | frozenset[str] | None = None,
    required_paths: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None = None,
) -> str | None:
    """Listing / FILE binding 路径必须是真正文；占位不得充数。"""

    if not _needs_real_generated_body(
        path,
        listing_names=listing_names,
        body_paths=body_paths,
        replay_paths=replay_paths,
        required_paths=required_paths,
    ):
        return None
    if not isinstance(content, str):
        return f"BINDING_PATH_STUB_ONLY:{path}"
    if looks_like_synthetic_stub(content) or not content.strip():
        return f"BINDING_PATH_STUB_ONLY:{path}"
    return None


def _canonical_support(name: str) -> str:
    lower = name.lower()
    return SUPPORT_CANONICAL.get(lower, name)


def _trusted_listing_name(raw: str) -> str | None:
    cleaned = (raw or "").replace(" (missing)", "").strip().strip("'\"")
    if not cleaned or ":" in cleaned or not _looks_like_filename(cleaned):
        return None
    path = _safe_relpath(cleaned)
    if path is None or ":" in path:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _BINARY_SUFFIXES:
        return None
    if "/" not in path and suffix not in _PATH_LIKE_SUFFIXES and not is_support_file(path):
        return None
    return path


def _filename_tokens(text: str) -> set[str]:
    names: set[str] = set()
    for raw in _FILENAME.findall(text or ""):
        path = _trusted_listing_name(raw)
        if path:
            names.add(path)
    return names


def _listing_result_names(text: str) -> set[str]:
    """只从目录表、具名 dump、rg 路径列和单独一行的真实文件名取 listing。"""

    names: set[str] = set()
    body = text or ""
    for match in _NAMED_DUMP_HEADER.finditer(body):
        path = _trusted_listing_name(match.group(1))
        if path:
            names.add(path)
    for match in _RG_HIT_PATH.finditer(body):
        path = _trusted_listing_name(match.group("path"))
        if path:
            names.add(path)
    has_table = "LastWriteTime" in body or bool(re.search(r"(?m)^----", body))
    for line in body.splitlines():
        stripped = line.strip().strip("'\"")
        if not stripped or _TABLE_RULE.fullmatch(stripped):
            continue
        if stripped.lower().split()[0] in _LISTING_TABLE_HEADERS:
            continue
        if has_table:
            token = stripped.replace("\\", "/").split()[-1]
            path = _trusted_listing_name(token)
            if path:
                names.add(path)
            continue
        candidate = stripped.replace("\\", "/")
        if _SOLE_FILENAME.fullmatch(candidate):
            path = _trusted_listing_name(candidate)
            if path:
                names.add(path)
    return names


def _directory_tokens(text: str) -> set[str]:
    found: set[str] = set()
    for raw in _DIR_TOKEN.findall(text or ""):
        path = _safe_relpath(raw.rstrip("/"))
        if path:
            found.add(path + "/")
    return found


def _is_project_stub_name(path: str) -> bool:
    posix = PurePosixPath(path)
    suffix = posix.suffix.lower()
    if ":" in path or suffix in _BINARY_SUFFIXES:
        return False
    return suffix in _STUB_SOURCE_SUFFIXES


def _command(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, dict):
        for key in ("command", "cmd", "cmd_string", "input"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _is_listing(name: str, command: str) -> bool:
    if name in {"glob", "grep", "list_dir"}:
        return True
    token = re.split(r"\s+", command.strip(), maxsplit=1)[0] if command.strip() else ""
    executable = PurePosixPath(token).name.lower()
    if executable in LISTING_COMMANDS:
        return True
    lowered = command.lower()
    return any(
        re.search(rf"(?<![A-Za-z0-9._-]){re.escape(item)}(?![A-Za-z0-9._-])", lowered)
        for item in LISTING_COMMANDS
    )


def _blob(task: dict[str, Any] | None, replay: ReplayResult, timeline: list[dict[str, Any]]) -> str:
    parts = [json.dumps(task or {}, ensure_ascii=False)]
    for item in replay.files:
        parts.append(item.path)
        parts.append(item.content)
    for item in timeline:
        if not isinstance(item, dict):
            continue
        parts.append(str(item.get("name") or ""))
        arguments = item.get("arguments")
        parts.append(json.dumps(arguments, ensure_ascii=False) if arguments else "")
        parts.append(str(item.get("result_text") or ""))
    return "\n".join(parts)


def _mentioned_support_paths(
    task: dict[str, Any] | None, replay: ReplayResult, timeline: list[dict[str, Any]]
) -> set[str]:
    text = _blob(task, replay, timeline)
    found: set[str] = set()
    lowered = text.lower()
    for name in SUPPORT_BASENAMES:
        if re.search(rf"(?<![A-Za-z0-9._-]){re.escape(name)}(?![A-Za-z0-9._-])", lowered):
            found.add(_canonical_support(name))
    for match in _FILENAME.finditer(text):
        path = _safe_relpath(match.group(1))
        if path and is_support_file(path):
            found.add(_canonical_support(PurePosixPath(path).as_posix()))
    return found


def topic_card(
    replay: ReplayResult,
    listing_names: set[str] | frozenset[str],
    directories: set[str] | frozenset[str],
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dirs = set(directories)
    names: set[str] = set(listing_names)
    topics: set[str] = set()
    for item in replay.files:
        names.add(item.path)
        parent = PurePosixPath(item.path).parent
        if parent.as_posix() not in {".", ""}:
            dirs.add(parent.as_posix() + "/")
        stem = PurePosixPath(item.path).stem
        if stem and stem.lower() not in {"build", "readme", "license"}:
            topics.add(stem)
    for path in file_required_paths(task):
        if path.endswith("/"):
            dirs.add(path)
        else:
            names.add(path)
            stem = PurePosixPath(path).stem
            if stem:
                topics.add(stem)
    for name in listing_names:
        if name.endswith("/"):
            dirs.add(name)
            continue
        stem = PurePosixPath(name).stem
        if stem and stem.lower() not in {"build", "readme"}:
            topics.add(stem)
    return {
        "directories": sorted(dirs)[:24],
        "names": sorted(names)[:40],
        "topics": sorted(topics)[:20],
    }


def index_completion_holes(
    replay: ReplayResult,
    timeline: list[dict[str, Any]],
    task: dict[str, Any] | None = None,
) -> CompletionIndex:
    """PARTIAL + unread bodies + support + listing/binding stubs."""

    complete = {item.path for item in replay.files if item.completeness == "COMPLETE"}
    untrusted = {
        str(item.get("path"))
        for item in getattr(replay, "partial_evidence", ()) or ()
        if isinstance(item, dict)
        and item.get("reason") in _UNTRUSTED_HOLE_REASONS
        and isinstance(item.get("path"), str)
        and item.get("path")
    }
    body_paths: set[str] = set()
    listing_names: set[str] = set()
    listing_from_commands: set[str] = set()
    directories: set[str] = set()
    for op in normalize_file_ops(timeline):
        path = op.get("path")
        if op.get("kind") == "read" and isinstance(path, str) and isinstance(op.get("content"), str):
            body_paths.add(path)
    for item in timeline:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lower()
        command = _command(item.get("arguments"))
        result = str(item.get("result_text") or "")
        if _is_listing(name, command):
            listing_from_commands.update(_filename_tokens(command))
            listing_from_commands.update(_listing_result_names(result))
            directories.update(_directory_tokens(command))
            directories.update(_directory_tokens(result))
    for item in replay.files:
        parent = PurePosixPath(item.path).parent
        if parent.as_posix() not in {".", ""}:
            directories.add(parent.as_posix() + "/")
    listing_names |= listing_from_commands
    listing_names -= body_paths

    holes: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(path: str, kind: str, reason: str) -> None:
        if path in seen or path.endswith("/"):
            return
        seen.add(path)
        holes.append({"path": path, "kind": kind, "reason": reason})

    for item in replay.files:
        if item.completeness == "PARTIAL" and not is_runtime_log(item.path):
            _add(item.path, "PARTIAL", "partial_read")
    for path in sorted(body_paths):
        if path in complete or path in untrusted or is_runtime_log(path):
            continue
        _add(path, "MISSING_BODY", "read_body_not_complete")
    if replay.files:
        for path in sorted(_mentioned_support_paths(task, replay, timeline)):
            if path in complete:
                continue
            _add(path, "SUPPORT", "mentioned_support_file")
        for path in sorted(listing_from_commands):
            if (
                path in complete
                or path in body_paths
                or is_runtime_log(path)
                or is_support_file(path)
                or not _is_project_stub_name(path)
            ):
                continue
            _add(path, "STUB", "listing_name_unobserved")
        for path in file_required_paths(task):
            if path.endswith("/") or path in complete or is_runtime_log(path):
                continue
            if path not in {item.path for item in replay.files}:
                _add(path, "STUB", "binding_required_path")
    return CompletionIndex(
        holes=tuple(holes),
        listing_names=frozenset(listing_names),
        body_paths=frozenset(body_paths),
        topic_card=topic_card(replay, listing_names, directories, task),
    )
