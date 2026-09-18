"""Shared q↔Ê contract: obligation bindings to workspace paths.

Intent cites user text only. required_paths may come from that text or from
paths already visible in the timeline / replay. Completion, Sufficiency, and
Verifier read the same normalized list.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from traceforge.reconstruction.env_replay import (
    _looks_like_filename,
    _safe_relpath,
    normalize_file_ops,
)

FILE = "FILE"
NON_FILE = "NON_FILE"
VERIFIER_KINDS = frozenset({FILE, NON_FILE})
_FILENAME = re.compile(
    r"(?:^|[\s'\"`=:,(\[])((?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.[A-Za-z0-9]{1,8})"
)
_DIR = re.compile(r"(?:^|[\s'\"`=:,(\[])((?:[A-Za-z0-9._-]+/){1,6})")
_READ_CODE = re.compile(
    r"(?i)(读|讀|看懂|完全读|完全讀|read|inspect|understand).{0,24}(代码|代碼|code|codebase|注入|injector|项目|工程)"
)
_SOURCE_SUFFIXES = frozenset(
    {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".hpp"}
)
_STUB_MARKERS = ("body unobserved", "observed name", "unobserved body")


def normalize_binding_path(raw: str) -> str | None:
    text = str(raw or "").replace("\\", "/").strip()
    if not text:
        return None
    directory = text.endswith("/")
    path = _safe_relpath(text.rstrip("/"))
    if path is None:
        return None
    return path + "/" if directory else path


def _filename_tokens(text: str) -> set[str]:
    names: set[str] = set()
    for raw in _FILENAME.findall(text or ""):
        if not _looks_like_filename(raw):
            continue
        path = _safe_relpath(raw)
        if path:
            names.add(path)
    for raw in _DIR.findall(text or ""):
        path = _safe_relpath(raw.rstrip("/"))
        if path:
            names.add(path + "/")
    return names


def _directory_prefixes(paths: set[str]) -> set[str]:
    extras: set[str] = set()
    for path in paths:
        parent = PurePosixPath(path.rstrip("/")).parent
        while parent.as_posix() not in {".", ""}:
            extras.add(parent.as_posix() + "/")
            parent = parent.parent
    return extras


def collect_allowed_paths(
    source: dict[str, Any] | None,
    records: list[dict[str, Any]] | None = None,
    *,
    replay_files: list[str] | None = None,
) -> list[str]:
    """Paths the Intent agent may bind: user text + already-observed timeline/replay."""

    found: set[str] = set()
    for record in records or []:
        if isinstance(record, dict):
            found.update(_filename_tokens(str(record.get("text") or "")))
    timeline = list((source or {}).get("tool_timeline") or [])
    for item in timeline:
        if not isinstance(item, dict):
            continue
        arguments = item.get("arguments")
        if isinstance(arguments, dict):
            for key in ("path", "file_path", "filename", "file"):
                path = normalize_binding_path(str(arguments.get(key) or ""))
                if path:
                    found.add(path)
            blob = json.dumps(arguments, ensure_ascii=False)
        else:
            blob = str(arguments or "")
        found.update(_filename_tokens(blob))
        found.update(_filename_tokens(str(item.get("result_text") or "")))
    for op in normalize_file_ops(timeline):
        path = op.get("path")
        if isinstance(path, str) and path:
            found.add(path)
    for path in replay_files or []:
        normalized = normalize_binding_path(path)
        if normalized:
            found.add(normalized)
    found.update(_directory_prefixes(found))
    return sorted(found)


def observed_body_paths(source: dict[str, Any] | None) -> list[str]:
    """Paths whose file bodies were actually read, not merely listed."""

    found: list[str] = []
    timeline = list((source or {}).get("tool_timeline") or [])
    for op in normalize_file_ops(timeline):
        path = op.get("path")
        content = op.get("content")
        if op.get("kind") != "read" or not isinstance(path, str) or not path:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        if path not in found:
            found.append(path)
    return found


def collect_file_binding_paths(
    source: dict[str, Any] | None,
    records: list[dict[str, Any]] | None = None,
    *,
    replay_files: list[str] | None = None,
) -> list[str]:
    """FILE required_paths: observed bodies and their parent directories."""

    del records
    bodies = set(observed_body_paths(source))
    for path in replay_files or []:
        normalized = normalize_binding_path(path)
        if normalized:
            bodies.add(normalized)
    bodies.update(_directory_prefixes(bodies))
    return sorted(bodies)


def path_is_allowed(path: str, allowed: set[str] | list[str]) -> bool:
    allowed_set = set(allowed)
    if path in allowed_set:
        return True
    if path.endswith("/"):
        prefix = path
        return any(item == path.rstrip("/") or item.startswith(prefix) for item in allowed_set)
    # File bindings must use the exact normalized path.  Accepting a matching
    # basename silently maps ``foo.py`` to an unrelated ``src/foo.py`` and
    # lets the model bind evidence from another task or directory.
    return False


def mentioned_allowed_paths(text: str, allowed: list[str] | set[str]) -> list[str]:
    tokens = _filename_tokens(text or "")
    ordered: list[str] = []
    for path in sorted(allowed):
        if path in tokens or path_is_allowed(path, tokens):
            if path not in ordered:
                ordered.append(path)
    return ordered


def _source_allowed(allowed: list[str]) -> list[str]:
    return [
        path
        for path in allowed
        if not path.endswith("/") and PurePosixPath(path).suffix.lower() in _SOURCE_SUFFIXES
    ]


def derive_binding(
    obligation: dict[str, Any],
    allowed_paths: list[str],
    user_blob: str = "",
) -> dict[str, Any]:
    oid = str(obligation.get("id") or "")
    text = f"{obligation.get('text') or ''} {user_blob}"
    mentioned = mentioned_allowed_paths(text, allowed_paths)
    if not mentioned and _READ_CODE.search(text):
        mentioned = _source_allowed(allowed_paths)[:12]
        mentioned.extend(path for path in allowed_paths if path.endswith("/") and path not in mentioned)
    if mentioned:
        return {
            "obligation_id": oid,
            "required_paths": mentioned,
            "observable": str(obligation.get("text") or ""),
            "verifier_kind": FILE,
        }
    return {
        "obligation_id": oid,
        "required_paths": [],
        "observable": "",
        "verifier_kind": NON_FILE,
    }


def _normalize_one_binding(
    item: dict[str, Any],
    *,
    known_ids: set[str],
    allowed_paths: list[str],
    file_binding_paths: list[str] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    oid = item.get("obligation_id") or item.get("id")
    if not isinstance(oid, str) or not oid.strip():
        return None, ["BINDING_OBLIGATION_ID_INVALID"]
    if oid not in known_ids:
        return None, [f"BINDING_OBLIGATION_UNKNOWN:{oid}"]
    kind = str(item.get("verifier_kind") or "").strip().upper()
    if kind not in VERIFIER_KINDS:
        return None, [f"BINDING_VERIFIER_KIND_INVALID:{oid}"]
    raw_paths = item.get("required_paths")
    if raw_paths is None:
        raw_paths = []
    if not isinstance(raw_paths, list) or any(not isinstance(path, str) for path in raw_paths):
        return None, [f"BINDING_PATHS_INVALID:{oid}"]
    paths: list[str] = []
    for raw in raw_paths:
        path = normalize_binding_path(raw)
        if path is None:
            errors.append(f"BINDING_PATH_UNSAFE:{oid}:{raw}")
            continue
        if not path_is_allowed(path, allowed_paths):
            errors.append(f"BINDING_PATH_NOT_ALLOWED:{oid}:{path}")
            continue
        bindable = allowed_paths if file_binding_paths is None else file_binding_paths
        if kind == FILE and not path_is_allowed(path, bindable):
            # Listing-only names are tree shape, not FILE evidence. Drop them
            # so q stays solvable on observed excerpts.
            continue
        if path not in paths:
            paths.append(path)
    observable = item.get("observable")
    if observable is None:
        observable = ""
    if not isinstance(observable, str):
        errors.append(f"BINDING_OBSERVABLE_INVALID:{oid}")
        observable = ""
    if kind == FILE and (not observable.strip() or observable.strip().lower() == "replayed excerpts still present"):
        errors.append(f"BINDING_TASK_OUTCOME_REQUIRED:{oid}")
    if kind == FILE and not paths and file_binding_paths is None:
        errors.append(f"BINDING_FILE_PATHS_REQUIRED:{oid}")
    if kind == NON_FILE and paths:
        # Model sometimes attaches review-context files to a chat/research
        # obligation. Drop them so NON_FILE stays pathless; the repaired
        # binding is valid and must not abort the four-role pipeline.
        paths = []
    return (
        {
            "obligation_id": oid,
            "required_paths": paths,
            "observable": observable,
            "verifier_kind": kind,
        },
        errors,
    )


def normalize_environment_bindings(
    payload: dict[str, Any],
    obligations: list[dict[str, Any]],
    allowed_paths: list[str],
    *,
    user_blob: str = "",
    file_binding_paths: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    known_ids = {str(item.get("id")) for item in obligations if isinstance(item, dict) and item.get("id")}
    raw = payload.get("environment_bindings")
    by_id: dict[str, dict[str, Any]] = {}
    if raw is None or raw == []:
        raw = []
    elif not isinstance(raw, list):
        errors.append("ENVIRONMENT_BINDINGS_NOT_ARRAY")
        raw = []
    for item in raw:
        if not isinstance(item, dict):
            errors.append("BINDING_NOT_OBJECT")
            continue
        normalized, item_errors = _normalize_one_binding(
            item,
            known_ids=known_ids,
            allowed_paths=allowed_paths,
            file_binding_paths=file_binding_paths,
        )
        errors.extend(item_errors)
        if normalized is None:
            continue
        oid = normalized["obligation_id"]
        if oid in by_id:
            errors.append(f"BINDING_OBLIGATION_DUPLICATE:{oid}")
            continue
        by_id[oid] = normalized
    result: list[dict[str, Any]] = []
    for obligation in obligations:
        if not isinstance(obligation, dict) or not obligation.get("id"):
            continue
        oid = str(obligation["id"])
        bindable = file_binding_paths if file_binding_paths is not None else allowed_paths
        if oid in by_id:
            current = by_id[oid]
            if current.get("verifier_kind") == FILE and not current.get("required_paths"):
                derived = derive_binding(obligation, bindable, user_blob)
                if derived.get("required_paths"):
                    result.append(derived)
                    continue
                errors.append(f"BINDING_FILE_PATHS_REQUIRED:{oid}")
            result.append(current)
        else:
            result.append(derive_binding(obligation, bindable, user_blob))
    return result, errors


def attach_bindings_to_obligations(
    obligations: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {item["obligation_id"]: item for item in bindings if item.get("obligation_id")}
    attached: list[dict[str, Any]] = []
    for item in obligations:
        row = dict(item)
        binding = by_id.get(str(row.get("id") or ""))
        if binding:
            row["verifier_kind"] = binding["verifier_kind"]
            row["required_paths"] = list(binding["required_paths"])
            row["observable"] = binding.get("observable") or ""
        attached.append(row)
    return attached


def environment_bindings(task: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(task, dict):
        return []
    raw = task.get("environment_bindings")
    if isinstance(raw, list) and raw:
        return [item for item in raw if isinstance(item, dict)]
    out: list[dict[str, Any]] = []
    for obligation in task.get("acceptance_obligations") or []:
        if not isinstance(obligation, dict):
            continue
        kind = obligation.get("verifier_kind")
        if kind not in VERIFIER_KINDS:
            continue
        out.append(
            {
                "obligation_id": obligation.get("id"),
                "required_paths": list(obligation.get("required_paths") or []),
                "observable": obligation.get("observable") or "",
                "verifier_kind": kind,
            }
        )
    return out


def file_required_paths(task: dict[str, Any] | None) -> list[str]:
    paths: list[str] = []
    for binding in environment_bindings(task):
        if binding.get("verifier_kind") != FILE:
            continue
        for path in binding.get("required_paths") or []:
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    return paths


def file_obligation_ids(task: dict[str, Any] | None) -> list[str]:
    bindings = environment_bindings(task)
    obligations = [
        item
        for item in (task or {}).get("acceptance_obligations") or []
        if isinstance(item, dict) and item.get("id")
    ]
    if not bindings:
        return [str(item["id"]) for item in obligations]
    return [
        str(item["obligation_id"])
        for item in bindings
        if item.get("verifier_kind") == FILE and item.get("obligation_id")
    ]


def non_file_obligation_ids(task: dict[str, Any] | None) -> list[str]:
    bindings = environment_bindings(task)
    if not bindings:
        return []
    return [
        str(item["obligation_id"])
        for item in bindings
        if item.get("verifier_kind") == NON_FILE and item.get("obligation_id")
    ]


def expand_tree_paths(paths: set[str] | list[str]) -> set[str]:
    out = {str(path) for path in paths if path}
    out.update(_directory_prefixes(out))
    return out


def workspace_relpaths(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    found: set[str] = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if rel in {".", ""}:
            continue
        if path.is_dir():
            found.add(rel + "/")
        elif path.is_file():
            found.add(rel)
    return expand_tree_paths(found)


def path_present(required: str, present: set[str]) -> bool:
    if required in present:
        return True
    if required.endswith("/"):
        prefix = required
        return any(item == required.rstrip("/") or item.startswith(prefix) for item in present)
    return False


def missing_binding_paths(
    workspace_or_paths: Path | set[str] | list[str],
    task: dict[str, Any] | None,
) -> list[str]:
    if isinstance(workspace_or_paths, Path):
        present = workspace_relpaths(workspace_or_paths)
    else:
        present = expand_tree_paths(workspace_or_paths)
    return [path for path in file_required_paths(task) if not path_present(path, present)]


def looks_like_synthetic_stub(content: str) -> bool:
    lowered = (content or "").lower()
    return any(marker in lowered for marker in _STUB_MARKERS)


def workspace_is_stub_ensemble(root: Path) -> bool:
    files = [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
    if not files:
        return False
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        if not looks_like_synthetic_stub(text):
            return False
    return True


__all__ = [
    "FILE",
    "NON_FILE",
    "attach_bindings_to_obligations",
    "collect_allowed_paths",
    "collect_file_binding_paths",
    "derive_binding",
    "observed_body_paths",
    "environment_bindings",
    "expand_tree_paths",
    "file_obligation_ids",
    "file_required_paths",
    "looks_like_synthetic_stub",
    "missing_binding_paths",
    "non_file_obligation_ids",
    "normalize_binding_path",
    "normalize_environment_bindings",
    "path_is_allowed",
    "path_present",
    "workspace_is_stub_ensemble",
    "workspace_relpaths",
]
