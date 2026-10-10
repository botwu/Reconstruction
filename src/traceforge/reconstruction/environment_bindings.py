"""Shared q↔Ê contract: obligation bindings to workspace paths.

Intent cites user text only. required_paths may come from that text or from
paths already visible in the timeline / replay. Completion, Sufficiency, and
Verifier read the same normalized list.
"""

from __future__ import annotations

import re
import shlex
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
    r"(?:^|[\s'\"`=:,(\[])((?:[A-Za-z0-9._-]+/){1,6})(?![A-Za-z0-9_/-]|\.[A-Za-z0-9_])"
)
_FENCED_BLOCK = re.compile(r"(?ms)^(`{3,}|~{3,})([^\n]*)\n.*?^\1[ \t]*$")
_CLOSING_TAG = re.compile(r"</[A-Za-z_][\w:.-]*\s*>")
_EXAMPLE_CONTEXT = re.compile(r"(?i)(?:\b(?:example|shape|schema)\s*[:：]\s*$|(?:示例|格式如下)\s*[:：]?\s*$)")
_INLINE_EXAMPLE = re.compile(r"(?i)(?:\be\.g\.|\bfor example\b|例如|比如|示例[:：])[^\n;；。]*")
_LISTING_TOOLS = frozenset({"ls", "list_dir", "glob", "find", "fd", "tree", "rg", "grep"})
_ABSOLUTE_PATH = re.compile(r"(?<![\w./])(?:[A-Za-z]:/|/)[^\s`\"'<>,]+")
_STUB_MARKERS = ("body unobserved", "observed name", "unobserved body")


def normalize_binding_path(raw: str) -> str | None:
    text = str(raw or "").replace("\\", "/").strip()
    # Models often copy a path from a prose list with a trailing semicolon or
    # comma. Remove only punctuation outside the path; keep filename dots.
    text = text.rstrip(";,")
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


def _request_path_text(text: str) -> str:
    """仅清理路径提取视图中的格式示例和结束标签，原始记录不变。"""
    def mask_example(match: re.Match[str]) -> str:
        prefix = text[:match.start()].rstrip().rsplit("\n", 1)[-1]
        tag = match.group(2).strip().lower()
        if tag in {"acceptance-report", "json acceptance-report"} or _EXAMPLE_CONTEXT.search(prefix):
            return "\n"
        return match.group(0)

    without_examples = _INLINE_EXAMPLE.sub("", _FENCED_BLOCK.sub(mask_example, text or ""))
    return _CLOSING_TAG.sub(" ", without_examples)


def collect_binding_path_aliases(
    source: dict[str, Any] | None, records: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """只依据 Replay 根和明确的原始路径锚点转换坐标，不按 basename 猜测。"""
    timeline = list((source or {}).get("tool_timeline") or [])
    root = replay_workspace_root(timeline)
    if not root:
        return {}
    candidates: dict[str, set[str]] = {}
    canonical_paths = {op["path"] for op in normalize_file_ops(timeline) if op.get("path")}

    def add(raw: str, canonical: str | None) -> None:
        if not canonical:
            return
        for alias in (raw.replace("\\", "/"), normalize_binding_path(raw)):
            if alias:
                candidates.setdefault(alias, set()).add(canonical)

    for item in timeline:
        arguments = item.get("arguments") if isinstance(item, dict) else None
        if not isinstance(arguments, dict):
            continue
        for key in ("path", "file_path", "filePath", "filename", "file"):
            raw = arguments.get(key)
            if isinstance(raw, str):
                add(raw, _safe_relpath(raw, root))
    for record in records or []:
        text = _request_path_text(str(record.get("text") or "")).replace("\\", "/")
        absolute = {match.group(0).rstrip(".;)]}") for match in _ABSOLUTE_PATH.finditer(text)}
        for raw in absolute:
            add(raw, _safe_relpath(raw, root))
        # 至少两个同目录的绝对用户输入才足以锚定省略根的相对路径。
        # 不把同名文件或跨目录的共同后缀视为用户工作目录证据。
        parents = {str(PurePosixPath(path).parent) for path in absolute}
        if len(absolute) < 2 or len(parents) != 1:
            continue
        parent = next(iter(parents))
        normalized_root = root.replace("\\", "/").rstrip("/")
        if parent != normalized_root and not parent.startswith(normalized_root + "/"):
            continue
        relative_root = _safe_relpath(parent, root)
        if not relative_root:
            continue
        relative_text = _ABSOLUTE_PATH.sub("", text)
        for path in _filename_tokens(relative_text):
            if path in canonical_paths or path.startswith(relative_root + "/"):
                continue
            canonical = normalize_binding_path(relative_root + "/" + path)
            add(path, canonical)
            add(parent + "/" + path, canonical)
    return {raw: next(iter(paths)) for raw, paths in candidates.items()
            if len(paths) == 1 and raw != next(iter(paths))}


def _binding_context_text(text: str, aliases: dict[str, str]) -> str:
    """仅转换派生绑定视图；原始用户记录不修改。"""
    return _replace_binding_paths(_request_path_text(text), aliases)


def _replace_binding_paths(text: str, aliases: dict[str, str]) -> str:
    """按已有映射替换路径；保留 observable 的其余语义。"""
    if not aliases:
        return text
    replacements = dict(aliases)
    replacements.update({path.replace("/", "\\"): target for path, target in aliases.items()})
    pattern = re.compile(r"(?<![A-Za-z0-9_./\\-])(?:" + "|".join(
        re.escape(path) for path in sorted(replacements, key=len, reverse=True)
    ) + r")(?![A-Za-z0-9_/\\-]|\.[A-Za-z0-9_])")
    return pattern.sub(lambda match: replacements[match.group(0)], text)


def _canonical_binding_path(raw: str, aliases: dict[str, str]) -> str | None:
    text = raw.replace("\\", "/").strip().rstrip(";,")
    if re.match(r"(?:[A-Za-z]:/|/)", text):
        # 绝对坐标只能用原轨迹建立的完整别名，不能静默剥掉未知用户目录。
        mapped = aliases.get(text)
    else:
        normalized = normalize_binding_path(text)
        mapped = aliases.get(normalized, normalized) if normalized else None
    if mapped is None:
        return None
    return mapped.rstrip("/") + "/" if text.endswith("/") else mapped


def _listing_paths(item: dict[str, Any]) -> set[str]:
    """只读取 listing 的路径列，不把匹配到的源文件正文当路径证据。"""
    arguments = item.get("arguments")
    if isinstance(arguments, dict):
        command = next((arguments[key] for key in ("command", "cmd", "cmd_string", "input")
                        if isinstance(arguments.get(key), str)), "")
    else:
        command = str(arguments or "")
    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    executable = PurePosixPath(words[0]).name.lower() if words else ""
    if str(item.get("name") or "").lower() not in _LISTING_TOOLS and executable not in _LISTING_TOOLS:
        return set()
    found: set[str] = set()
    for word in words[1:]:
        if word.startswith("-"):
            continue
        found.update(_filename_tokens(word))
    for line in str(item.get("result_text") or "").splitlines():
        path_column = re.split(r":\d+:", line.strip(), maxsplit=1)[0]
        # 一行一个路径或 rg/grep 路径列；其余正文继续留在原始证据中。
        if re.fullmatch(r"[A-Za-z0-9._/-]+", path_column):
            found.update(_filename_tokens(path_column))
    return found


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
    path_aliases: dict[str, str] | None = None,
) -> list[str]:
    """可引用路径来自用户实际要求、工具路径参数、文件操作或 listing 路径列。"""

    aliases = collect_binding_path_aliases(source, records) if path_aliases is None else path_aliases
    found: set[str] = set()
    for record in records or []:
        if isinstance(record, dict):
            found.update(_filename_tokens(_binding_context_text(str(record.get("text") or ""), aliases)))
    timeline = list((source or {}).get("tool_timeline") or [])
    for item in timeline:
        if not isinstance(item, dict):
            continue
        arguments = item.get("arguments")
        if isinstance(arguments, dict):
            for key in ("path", "file_path", "filename", "file"):
                path = _canonical_binding_path(str(arguments.get(key) or ""), aliases)
                if path:
                    found.add(path)
        found.update(aliases.get(path, path) for path in _listing_paths(item))
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
    """可绑定路径来自可信初态正文及父目录，正文可以尚未完整。"""

    del records
    # 已有 Replay 时，以其屏障前初态路径为唯一依据，包含有正文的 PARTIAL。
    # 这里接纳路径不宣称文件已补全；回退原始 timeline 会把修改后的读取混入初态。
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


def _path_has_source(path: str, text: str) -> bool:
    """按完整字面路径核对来源，不受文件名语言或扩展名限制。"""
    literal = re.escape(path.rstrip("/")) + (r"/?" if path.endswith("/") else "")
    pattern = r"(?<![A-Za-z0-9_./\\-])" + literal + r"(?![A-Za-z0-9_/\\-]|\.[A-Za-z0-9_])"
    return re.search(pattern, text.replace("\\", "/")) is not None


def _normalize_one_binding(
    item: dict[str, Any],
    *,
    known_ids: set[str],
    allowed_paths: list[str],
    context_text: str = "",
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

    # 模型决定路径的作用；这里只规范坐标和集合，不从上下文猜测依赖。
    fields: dict[str, list[str]] = {}
    for key in ("required_paths", "initial_required_paths", "output_paths"):
        raw_paths = item.get(key, [])
        if not isinstance(raw_paths, list) or any(not isinstance(path, str) for path in raw_paths):
            return None, [f"BINDING_PATHS_INVALID:{oid}"]
        paths: list[str] = []
        for raw in raw_paths:
            path = _canonical_binding_path(raw, path_aliases or {})
            if path is None:
                errors.append(f"BINDING_PATH_UNSAFE:{oid}:{raw}")
            elif (
                not path_is_allowed(path, allowed_paths)
                and not _path_has_source(path, context_text)
            ):
                errors.append(f"BINDING_PATH_NOT_ALLOWED:{oid}:{path}")
            elif path not in paths:
                paths.append(path)
        fields[key] = paths

    outputs = fields["output_paths"]
    initial = fields["initial_required_paths"]
    if "initial_required_paths" not in item:
        initial = [path for path in fields["required_paths"] if path not in outputs]
    all_paths = fields["required_paths"]
    if "required_paths" not in item:
        all_paths = list(dict.fromkeys([*initial, *outputs]))
    if set(all_paths) != set(initial) | set(outputs):
        errors.append(f"BINDING_PATH_UNION_MISMATCH:{oid}")
    for path in initial:
        if path in outputs:
            errors.append(f"BINDING_PATH_ROLE_CONFLICT:{oid}:{path}")

    # 仅核对产物位置的原始来源；目录也合法，不要求沿用历史助手的文件名。
    for path in outputs:
        if not _path_has_source(path, context_text):
            errors.append(f"BINDING_OUTPUT_PATH_NOT_EXPLICIT:{oid}:{path}")

    observable = item.get("observable", "")
    if not isinstance(observable, str):
        errors.append(f"BINDING_OBSERVABLE_INVALID:{oid}")
        observable = ""
    observable = _replace_binding_paths(observable, path_aliases or {})
    if kind == FILE and (not observable.strip() or observable.strip().lower() == "replayed excerpts still present"):
        errors.append(f"BINDING_TASK_OUTCOME_REQUIRED:{oid}")
    if kind == FILE and not all_paths:
        errors.append(f"BINDING_FILE_PATHS_REQUIRED:{oid}")
    return (
        {
            "obligation_id": oid,
            "required_paths": all_paths,
            "initial_required_paths": initial,
            "output_paths": outputs,
            "observable": observable,
            "verifier_kind": kind,
        },
        list(dict.fromkeys(errors)),
    )


def normalize_environment_bindings(
    payload: dict[str, Any],
    obligations: list[dict[str, Any]],
    allowed_paths: list[str],
    *,
    user_blob: str = "",
    user_records: list[dict[str, Any]] | None = None,
    system_records: list[dict[str, Any]] | None = None,
    path_aliases: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """校验模型显式合同；缺失或冲突交回模型，不补造绑定。"""
    errors: list[str] = []
    known_ids = {str(item.get("id")) for item in obligations if isinstance(item, dict) and item.get("id")}
    obligations_by_id = {str(item.get("id")): item for item in obligations if isinstance(item, dict)}
    records_by_id = {str(item.get("id")): str(item.get("text") or "")
                     for item in user_records or [] if isinstance(item, dict)}

    def binding_context(obligation: dict[str, Any]) -> str:
        # 来源校验只读取该义务引用的用户消息，不能借用其他任务或助手命名。
        if user_records is not None:
            text = "\n".join(records_by_id[ref] for ref in obligation.get("evidence_ref_ids") or []
                             if ref in records_by_id)
        else:
            text = user_blob
        text = "\n".join([text, *[str(row.get("text") or "") for row in system_records or []]])
        return _replace_binding_paths(text, path_aliases or {})

    raw = payload.get("environment_bindings", [])
    by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
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
            context_text=binding_context(obligations_by_id.get(str(item.get("obligation_id") or item.get("id")), {})),
            path_aliases=path_aliases,
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
            result.append(by_id[oid])
        else:
            errors.append(f"BINDING_REQUIRED:{oid}")
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
    """所需初态文件，不依赖义务的验收类型。"""
    paths: list[str] = []
    for binding in environment_bindings(task):
        initial = binding.get("initial_required_paths")
        candidates = initial if isinstance(initial, list) else binding.get("required_paths") or []
        for path in candidates:
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    return paths


def file_output_paths(task: dict[str, Any] | None) -> list[str]:
    """任务明确声明的新增产物；回答验收类型不限制其输出位置。"""
    paths: list[str] = []
    for binding in environment_bindings(task):
        candidates = binding.get("output_paths")
        if not isinstance(candidates, list):
            candidates = (
                binding.get("required_paths") or [] if binding.get("verifier_kind") == FILE else []
            )
        for path in candidates:
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
    return paths


def task_start_message_index(task: dict[str, Any]) -> int | None:
    source = task.get("source_task") or task
    indices = (source.get("evidence_refs") or {}).get("message_indices") or source.get("message_indices") or []
    return min((i for i in indices if type(i) is int and i >= 0), default=None)


def workspace_task_context(task: dict[str, Any]) -> dict[str, Any]:
    """初态 Agent 接收用户语义和输入输出路径，验收后端标签仅供编排器使用。"""
    context = {key: task[key] for key in (
        "task_id", "task_instruction", "core_objective", "source_task", "evidence_refs",
        "success_criteria", "mandatory_constraints", "prohibitions", "specified_output_format",
        "has_examples",
    ) if key in task}
    context["acceptance_obligations"] = [
        {key: item[key] for key in ("id", "text", "evidence_ref_ids") if key in item}
        for item in task.get("acceptance_obligations") or [] if isinstance(item, dict)
    ]
    context["initial_required_paths"] = file_required_paths(task)
    context["output_paths"] = file_output_paths(task)
    context["task_start_message_index"] = task_start_message_index(task)
    return context


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
    "collect_binding_path_aliases",
    "collect_file_binding_paths",
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
