"""从工具调用编出 DEFAULT_EMPTY 可用的处理逻辑摘要。不调用模型。"""

from __future__ import annotations

import json
from typing import Any

from traceforge.reconstruction.env_replay import (
    _looks_like_filename,
    _safe_relpath,
    normalize_file_ops,
)
from traceforge.reconstruction.environment_bindings import (
    collect_allowed_paths,
    file_required_paths,
)

TOOL_PROCESS_SKETCH_SCHEMA = "traceforge.tool-process-sketch.v1"
_WRITE_NAMES = frozenset(
    {
        "write",
        "write_file",
        "write_text",
        "edit",
        "str_replace",
        "create_file",
        "apply_patch",
    }
)
_LIST_NAMES = frozenset(
    {"ls", "dir", "glob", "grep", "rg", "find", "list_dir", "get-childitem"}
)


def _call_name(item: dict[str, Any]) -> str:
    raw = item.get("name") or item.get("tool")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    function = item.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"].strip()
    return ""


def _result_text(item: dict[str, Any]) -> str:
    for key in ("result_text", "content", "output"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    return ""


def _result_shape(name: str, item: dict[str, Any], result: str) -> str:
    if item.get("pending") is True:
        return "pending"
    lowered = result.lower()
    if any(token in lowered for token in ("error", "traceback", "exception", "failed")):
        return "error"
    key = name.lower()
    if key in _WRITE_NAMES:
        return "wrote"
    if key in _LIST_NAMES or key.endswith("list_dir"):
        return "listed"
    stripped = result.strip()
    if not stripped:
        return "empty"
    if stripped[:1] in {"{", "["}:
        try:
            json.loads(stripped)
            return "json"
        except json.JSONDecodeError:
            pass
    return "text"


def _event_paths(item: dict[str, Any], op_paths: set[str]) -> list[str]:
    found: set[str] = set()
    arguments = item.get("arguments")
    if isinstance(arguments, dict):
        for key in ("path", "file_path", "filename", "file", "target"):
            path = _safe_relpath(str(arguments.get(key) or ""))
            if path:
                found.add(path)
        blob = json.dumps(arguments, ensure_ascii=False)
    else:
        blob = str(arguments or "")
    for token in blob.replace(",", " ").split():
        if _looks_like_filename(token):
            path = _safe_relpath(token.strip("\"'`"))
            if path:
                found.add(path)
    found.update(op_paths)
    return sorted(found)


def build_tool_process_sketch(
    timeline: list[dict[str, Any]] | None,
    *,
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把工具名、路径和结果形态收成可引用的生成约束。"""

    events: list[dict[str, Any]] = []
    candidate_paths: set[str] = set()
    ops = normalize_file_ops(list(timeline or []))
    ops_by_id: dict[str, set[str]] = {}
    for op in ops:
        if not isinstance(op.get("path"), str) or not op["path"]:
            continue
        key = str(op.get("event_id") or op.get("call_id") or "")
        ops_by_id.setdefault(key, set()).add(op["path"])
        candidate_paths.add(op["path"])
    for index, item in enumerate(timeline or []):
        if not isinstance(item, dict):
            continue
        ref = str(item.get("call_id") or f"timeline:{index}")
        name = _call_name(item)
        result = _result_text(item)
        paths = _event_paths(item, ops_by_id.get(ref, set()))
        candidate_paths.update(paths)
        events.append(
            {
                "evidence_ref_id": ref,
                "name": name or "unknown",
                "paths": paths,
                "result_shape": _result_shape(name, item, result),
                "pending": bool(item.get("pending")),
            }
        )
    required = [str(path) for path in file_required_paths(task) if str(path)]
    instruction = " ".join(
        str((task or {}).get(key) or "")
        for key in ("task_instruction", "core_objective")
    )
    for record in (task or {}).get("acceptance_obligations") or []:
        if isinstance(record, dict):
            instruction += " " + str(record.get("text") or "")
    q_paths = collect_allowed_paths({"tool_timeline": []}, [{"text": instruction}])
    named = [path for path in q_paths if path]
    candidate_paths.update(required)
    candidate_paths.update(named)
    sufficient = bool(required or named or any(event["paths"] for event in events))
    reasons: list[str] = []
    if not sufficient:
        reasons.append("TOOL_PROCESS_INSUFFICIENT")
    return {
        "schema_version": TOOL_PROCESS_SKETCH_SCHEMA,
        "events": events,
        "candidate_paths": sorted(candidate_paths),
        "required_paths": required,
        "task_named_paths": named,
        "sufficient": sufficient,
        "reason_codes": reasons,
    }
