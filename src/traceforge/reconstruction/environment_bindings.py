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
    replay_workspace_root,
)

FILE = "FILE"
NON_FILE = "NON_FILE"
VERIFIER_KINDS = frozenset({FILE, NON_FILE})
_FILENAME = re.compile(
    r"(?:^|[\s'\"`=:,(\[])"
    r"((?:[A-Za-z]:)?[/\\]?(?:[A-Za-z0-9._\[\]-]+[/\\])*"
    r"[A-Za-z0-9._-]+\.[A-Za-z0-9]{1,8})(?![A-Za-z0-9_/\\-])"
)
_DIR = re.compile(
    r"(?:^|[\s'\"`=:,(\[])((?:[A-Za-z0-9._-]+/){1,6})(?![A-Za-z0-9_./-])"
)
_ACCEPTANCE_EXAMPLE = re.compile(
    r"(?ms)^[ \t]*```(?:json[ \t]+)?acceptance-report[ \t]*\r?\n"
    r".*?^[ \t]*```[ \t]*(?=\r?\n|\Z)"
)
_READ_CODE = re.compile(
    r"(?i)(读|讀|看懂|完全读|完全讀|read|inspect|understand).{0,24}(代码|代碼|code|codebase|注入|injector|项目|工程)"
)
_SOURCE_SUFFIXES = frozenset(
    {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".hpp"}
)
_OUTPUT_ACTION = re.compile(
    r"(?i)(\u65b0\u589e|\u65b0\u5efa|\u521b\u5efa|\u751f\u6210|\u5199\u5165|\u5199\u51fa|\u4fdd\u5b58|\u8f93\u51fa|\u4ea7\u51fa|\badd\b|\bcreate\b|\bgenerate\b|\bwrite\b|\bsave\b|\boutput\b|\bproduce\b)"
)
_INPUT_ACTION = re.compile(
    r"(?i)(\u4fee\u6539|\u66f4\u65b0|\u4fee\u590d|\u7f16\u8f91|\u53d8\u66f4|\bmodify\b|\bupdate\b|\bfix\b|\bedit\b|\bchange\b)"
)
_STUB_MARKERS = ("body unobserved", "observed name", "unobserved body")


def normalize_binding_path(raw: str, workspace_root: str | None = None) -> str | None:
    text = str(raw or "").replace("\\", "/").strip()
    # Models often copy a path from a prose list with a trailing semicolon or
    # comma. Remove only punctuation outside the path; keep filename dots.
    text = text.rstrip(";,")
    if not text:
        return None
    directory = text.endswith("/")
    path = _safe_relpath(text.rstrip("/"), workspace_root)
    if path is None:
        return None
    return path + "/" if directory else path


def _filename_tokens(text: str) -> set[str]:
    names: set[str] = set()
    text = _ACCEPTANCE_EXAMPLE.sub("", text or "")
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


def _binding_context(
    text: str, file_paths: list[str], workspace_root: str | None,
) -> tuple[str, dict[str, str], list[str]]:
    """用多个完整相对路径的共同根对齐坐标；不按 basename 猜文件。"""
    text = _ACCEPTANCE_EXAMPLE.sub("", text)
    tokens = _filename_tokens(text)
    aliases = {path: normalize_binding_path(path, workspace_root) or path for path in tokens}
    bodies = {path for path in file_paths if not path.endswith("/")}
    anchors = [
        {body[:-len(path) - 1] for body in bodies if body.endswith("/" + path)}
        for path in aliases.values() if "/" in path and path not in bodies
    ]
    anchors = [roots for roots in anchors if roots]
    errors: list[str] = []
    prefix = None
    if anchors:
        common = set.intersection(*anchors)
        if len(anchors) >= 2 and len(common) == 1:
            prefix = common.pop()
            absolute = {
                normalize_binding_path(raw, workspace_root)
                for raw in _FILENAME.findall(text)
                if re.match(r"^(?:[A-Za-z]:)?[/\\]", raw)
            }
            if any(path and not path.startswith(prefix + "/") for path in absolute):
                errors.append("BINDING_PATH_ROOT_CONFLICT")
                prefix = None
        else:
            errors.append("BINDING_PATH_ROOT_AMBIGUOUS")
    if prefix:
        aliases = {
            raw: path if path in bodies or path.startswith(prefix + "/") else prefix + "/" + path
            for raw, path in aliases.items()
        }

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        return match.group(0).replace(raw, aliases.get(normalize_binding_path(raw), raw), 1)

    return _DIR.sub(replace, _FILENAME.sub(replace, text)), aliases, errors


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
    workspace_root = replay_workspace_root(timeline)
    found = {normalize_binding_path(path, workspace_root) or path for path in found}
    bindable = collect_file_binding_paths(source, replay_files=replay_files)
    for record in records or []:
        context, aliases, _ = _binding_context(
            str(record.get("text") or ""), bindable, workspace_root
        )
        found = {aliases.get(path, path) for path in found}
        found.update(_filename_tokens(context))
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
    # Once Replay has run, its COMPLETE first-observation files are the sole
    # initial-body authority. Falling back to the raw timeline here would let a
    # post-write read masquerade as task-start evidence.
    if replay_files is None:
        bodies = set(observed_body_paths(source))
    else:
        bodies = set()
        for path in replay_files:
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


def _explicit_output_paths(text: str, allowed_paths: list[str]) -> set[str]:
    """Return paths explicitly described as new/final outputs.

    Unknown or merely mentioned paths remain initial inputs by default. This
    conservative rule prevents a missing source file from being silently
    reclassified as a generated output.
    """
    outputs: set[str] = set()
    # Classify within a sentence/clause. A large character window causes a
    # source input in "modify src/a.py and add tests/test_a.py" to inherit the
    # action for the later output. Output status is a semantic property of the
    # user request, not of a nearby filename anywhere in the paragraph.
    clauses = re.split(
        r"[\n\u3002\uFF1B;,\uFF0C]", _ACCEPTANCE_EXAMPLE.sub("", text or "")
    )
    for clause in clauses:
        paths = mentioned_allowed_paths(clause, allowed_paths)
        actions = sorted(
            [*[(m.start(), m.end(), True) for m in _OUTPUT_ACTION.finditer(clause)],
             *[(m.start(), m.end(), False) for m in _INPUT_ACTION.finditer(clause)]],
            key=lambda item: item[0],
        )
        if not paths or not actions:
            continue
        for path in paths:
            match = re.search(re.escape(path), clause)
            if match is None:
                continue
            prior = [item for item in actions if item[1] <= match.start()]
            # An output action must govern the path immediately before it.
            # This keeps "update src/a.py and generate report.md" split into
            # an initial input and a post-task output.
            if prior and prior[-1][2] and match.start() - prior[-1][1] <= 48:
                outputs.add(path)
    return outputs


def derive_binding(
    obligation: dict[str, Any],
    allowed_paths: list[str],
    user_blob: str = "",
    *,
    file_binding_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Derive initial inputs and explicit final outputs from user evidence."""
    oid = str(obligation.get("id") or "")
    text = user_blob or ""
    explicit = _filename_tokens(text)
    mentioned = sorted(path for path in explicit if path_is_allowed(path, allowed_paths))
    if not mentioned and _READ_CODE.search(text):
        # A generic code-review request needs observed source context. When
        # replay is available, listing-only names are excluded here.
        candidates = _source_allowed(allowed_paths)
        if file_binding_paths is not None:
            candidates = [path for path in candidates if path_is_allowed(path, file_binding_paths)]
        mentioned = candidates[:12]
        mentioned.extend(
            path for path in allowed_paths
            if path.endswith("/") and (file_binding_paths is None or path_is_allowed(path, file_binding_paths))
            and path not in mentioned
        )
    explicit_outputs = _explicit_output_paths(text, allowed_paths)
    initial = [path for path in mentioned if path not in explicit_outputs]
    outputs = [path for path in mentioned if path in explicit_outputs]
    return {
        "obligation_id": oid,
        "required_paths": [*initial, *outputs],
        "initial_required_paths": initial,
        "output_paths": outputs,
        "observable": str(obligation.get("text") or ""),
        "verifier_kind": FILE if mentioned else NON_FILE,
    }


def _normalize_one_binding(
    item: dict[str, Any],
    *,
    known_ids: set[str],
    allowed_paths: list[str],
    file_binding_paths: list[str] | None = None,
    context_text: str = "",
    workspace_root: str | None = None,
    path_aliases: dict[str, str] | None = None,
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
    raw_paths = item.get("required_paths") or []
    raw_outputs = item.get("output_paths") or []
    if (
        not isinstance(raw_paths, list)
        or any(not isinstance(path, str) for path in raw_paths)
        or not isinstance(raw_outputs, list)
        or any(not isinstance(path, str) for path in raw_outputs)
    ):
        return None, [f"BINDING_PATHS_INVALID:{oid}"]
    declared_outputs: set[str] = set()
    all_paths: list[str] = []
    explicit_paths = _filename_tokens(context_text)
    hinted_outputs = _explicit_output_paths(context_text, allowed_paths) & explicit_paths
    bindable = set(file_binding_paths) if file_binding_paths is not None else None
    for raw in dict.fromkeys([*raw_paths, *raw_outputs]):
        path = normalize_binding_path(raw, workspace_root)
        path = (path_aliases or {}).get(path, path)
        if path is None:
            errors.append(f"BINDING_PATH_UNSAFE:{oid}:{raw}")
            continue
        if not path_is_allowed(path, allowed_paths):
            errors.append(f"BINDING_PATH_NOT_ALLOWED:{oid}:{path}")
            continue
        # 用户明确点名的缺失输入仍是缺口；仅在 listing 出现的路径不能升级为输入。
        if raw in raw_outputs and path not in hinted_outputs:
            errors.append(f"BINDING_OUTPUT_PATH_NOT_EXPLICIT:{oid}:{path}")
        if (
            kind == FILE and bindable is not None
            and path not in bindable and path not in explicit_paths
        ):
            continue
        if path not in all_paths:
            all_paths.append(path)
        if raw in raw_outputs and path in hinted_outputs:
            declared_outputs.add(path)
    outputs = [path for path in all_paths if path in declared_outputs or path in hinted_outputs]
    initial = [path for path in all_paths if path not in set(outputs)]
    observable = item.get("observable")
    if observable is None:
        observable = ""
    if not isinstance(observable, str):
        errors.append(f"BINDING_OBSERVABLE_INVALID:{oid}")
        observable = ""
    if kind == FILE and (not observable.strip() or observable.strip().lower() == "replayed excerpts still present"):
        errors.append(f"BINDING_TASK_OUTCOME_REQUIRED:{oid}")
    if kind == FILE and not all_paths and file_binding_paths is None:
        errors.append(f"BINDING_FILE_PATHS_REQUIRED:{oid}")
    if kind == NON_FILE and all_paths:
        # NON_FILE obligations are intentionally pathless.
        all_paths, initial, outputs = [], [], []
    return (
        {
            "obligation_id": oid,
            "required_paths": all_paths,
            "initial_required_paths": initial,
            "output_paths": outputs,
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
    user_text_by_id: dict[str, str] | None = None,
    workspace_root: str | None = None,
    require_complete: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    known_ids = {str(item.get("id")) for item in obligations if isinstance(item, dict) and item.get("id")}
    contexts = {
        str(item["id"]): (
            "\n".join(
                user_text_by_id[ref] for ref in item.get("evidence_ref_ids", [])
                if ref in user_text_by_id
            )
            if user_text_by_id is not None else user_blob
        )
        for item in obligations if isinstance(item, dict) and item.get("id")
    }
    aliases_by_id = {}
    for oid, text in contexts.items():
        contexts[oid], aliases_by_id[oid], context_errors = _binding_context(
            text, file_binding_paths or [], workspace_root
        )
        errors.extend(f"{error}:{oid}" for error in context_errors)
    allowed_paths = sorted({
        normalize_binding_path(path, workspace_root) or path for path in allowed_paths
    } | {path for text in contexts.values() for path in _filename_tokens(text)})
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
            context_text=contexts.get(str(item.get("obligation_id") or item.get("id")), ""),
            workspace_root=workspace_root,
            path_aliases=aliases_by_id.get(str(item.get("obligation_id") or item.get("id"))),
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
        if oid in by_id:
            current = by_id[oid]
            if current.get("verifier_kind") == FILE and not current.get("required_paths"):
                # The model has already declared this obligation as FILE. It may
                # omit a path when the deliverable is a new output file; derive
                # only the path from explicit user/evidence text. This does not
                # infer a missing obligation (the require_complete gate below
                # still rejects an absent binding entry).
                derived = derive_binding(
                    obligation, allowed_paths, contexts[oid], file_binding_paths=file_binding_paths
                )
                if derived.get("required_paths"):
                    for key in ("required_paths", "initial_required_paths", "output_paths"):
                        current[key] = derived[key]
                    result.append(current)
                    continue
                errors.append(f"BINDING_FILE_PATHS_REQUIRED:{oid}")
            if current.get("verifier_kind") == FILE:
                derived = derive_binding(
                    obligation, allowed_paths, contexts[oid], file_binding_paths=file_binding_paths
                )
                for path in derived["output_paths"]:
                    if path not in current["output_paths"]:
                        current["output_paths"].append(path)
                    if path not in current["required_paths"]:
                        current["required_paths"].append(path)
            result.append(current)
        elif require_complete:
            # 模型显式返回 binding 列表时，它就是完整协议声明；
            # 不得从共享上下文推导遗漏的 FILE/NON_FILE 类型。
            errors.append(f"BINDING_REQUIRED:{oid}")
        else:
            result.append(
                derive_binding(
                    obligation, allowed_paths, contexts[oid], file_binding_paths=file_binding_paths
                )
            )
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
            row["initial_required_paths"] = list(
                binding.get("initial_required_paths", binding["required_paths"])
            )
            row["output_paths"] = list(binding.get("output_paths") or [])
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
                "initial_required_paths": list(
                    obligation.get("initial_required_paths", obligation.get("required_paths") or [])
                ),
                "output_paths": list(obligation.get("output_paths") or []),
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
        initial = binding.get("initial_required_paths")
        candidates = initial if isinstance(initial, list) else binding.get("required_paths") or []
        for path in candidates:
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    return paths


def file_output_paths(task: dict[str, Any] | None) -> list[str]:
    """Return FILE paths expected after execution, including new outputs."""
    paths: list[str] = []
    for binding in environment_bindings(task):
        if binding.get("verifier_kind") != FILE:
            continue
        candidates = binding.get("output_paths")
        if not isinstance(candidates, list):
            candidates = binding.get("required_paths") or []
        for path in candidates:
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
    "file_output_paths",
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
