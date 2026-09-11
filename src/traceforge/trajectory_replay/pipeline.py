"""确定性初始状态回放，不执行历史 shell。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.trajectory.json_codec import stable_id

REPLAY_SCHEMA = "traceforge.trajectory-replay.v1"


class ReplayInputError(ValueError):
    """规范化事件无法用于回放。"""


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("record must be object")
                result.append(value)
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReplayInputError(f"无法读取规范化事件：{path}") from exc


def _args(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return {}
    function = payload.get("function")
    arguments = (
        function.get("arguments") if isinstance(function, dict) else payload.get("arguments")
    )
    if isinstance(arguments, dict) and isinstance(arguments.get("value"), dict):
        return arguments["value"]
    return arguments if isinstance(arguments, dict) else {}


def _tool_name(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ""
    function = payload.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"].lower()
    return str(payload.get("tool_name", "")).lower()


def _path(arguments: dict[str, Any], source_workspace_root: Path | None = None) -> str | None:
    for key in ("path", "file_path", "filename", "file"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            path = value.strip().replace("\\", "/")
            if path.startswith("/"):
                if source_workspace_root is None:
                    return None
                try:
                    return (
                        Path(path).resolve().relative_to(source_workspace_root.resolve()).as_posix()
                    )
                except ValueError:
                    return None
            if ".." in Path(path).parts:
                return None
            return path.lstrip("./")
    return None


def _result_text(event: dict[str, Any]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if isinstance(content, dict) and isinstance(content.get("value"), str):
        return content["value"]
    return content if isinstance(content, str) else None


def _replay_capture(
    events: list[dict[str, Any]],
    source_workspace_root: Path | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    ordered = sorted(events, key=lambda item: int(item.get("sequence_number", 0)))
    pending: dict[str, tuple[str, str]] = {}
    observed: dict[str, tuple[str, str]] = {}
    changes: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    barriers: list[str] = []
    mutation_started = False
    for event in ordered:
        kind = str(event.get("event_kind", ""))
        name = _tool_name(event)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        call_id = str(payload.get("tool_call_id", ""))
        args = _args(event)
        path = _path(args, source_workspace_root)
        if kind == "TOOL_CALL" and path and name == "read":
            if mutation_started:
                partial.append(
                    {
                        "path": path,
                        "reason": "read_after_first_mutation",
                        "source_event_id": event.get("event_occurrence_id"),
                    }
                )
                continue
            if any(key in args for key in ("offset", "limit", "line_start", "line_end")):
                partial.append(
                    {
                        "path": path,
                        "reason": "partial_read_range",
                        "source_event_id": event.get("event_occurrence_id"),
                    }
                )
                continue
            pending[call_id] = (path, str(event.get("event_occurrence_id", "")))
        elif kind == "TOOL_RESULT" and call_id in pending:
            file_path, source_id = pending.pop(call_id)
            text = _result_text(event)
            if text is not None and file_path not in observed:
                observed[file_path] = (text, source_id)
        elif (
            kind == "TOOL_CALL"
            and path
            and any(token in name for token in ("write", "edit", "patch", "replace", "create"))
        ):
            changes.append(
                {
                    "event_id": event.get("event_occurrence_id"),
                    "path": path,
                    "operation": name,
                    "classification": "withheld_change",
                    "old_content_available": path in observed,
                    "new_content_withheld": True,
                }
            )
        elif kind == "TOOL_CALL" and any(
            token in name
            for token in ("shell", "exec", "terminal", "command", "bash", "powershell")
        ):
            barriers.append(str(event.get("event_occurrence_id", "unknown")))
    workspace = {}
    modified = {item["path"] for item in changes}
    for file_path, (text, source_id) in observed.items():
        if file_path in modified:
            partial.append(
                {
                    "path": file_path,
                    "reason": "modified_after_observation",
                    "source_event_id": source_id,
                }
            )
        workspace[file_path] = text
    for item in changes:
        if not item["old_content_available"]:
            item["classification"] = "agent_created_file"
    return workspace, changes, partial, barriers


def build_trajectory_replay(
    *,
    normalized_run_dir: str | Path,
    output_root: str | Path,
    capture_id: str | None = None,
    source_workspace_root: str | Path | None = None,
) -> Path:
    """从 normalized run 生成 replay manifest 与只读初始 workspace。"""
    source = Path(normalized_run_dir)
    workspace_root = (
        Path(source_workspace_root).resolve() if source_workspace_root is not None else None
    )
    events = _jsonl(source / "private/event_occurrences.jsonl")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        key = str(event.get("capture_occurrence_id", ""))
        if key and (capture_id is None or key == capture_id):
            grouped.setdefault(key, []).append(event)
    if capture_id is not None and capture_id not in grouped:
        raise ReplayInputError(f"未找到 capture：{capture_id}")
    run_id = stable_id(
        "trajectory-replay-v1",
        {"source": str(source.resolve()), "capture_id": capture_id, "event_count": len(events)},
    )
    workspace = ArtifactWorkspace(Path(output_root), run_id)
    entries = []
    captures = []
    try:
        for cid in sorted(grouped):
            files, changes, partial, barriers = _replay_capture(grouped[cid], workspace_root)
            base = Path("workspaces") / cid
            for file_path, text in sorted(files.items()):
                target = workspace.staging_path / base / file_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            captures.append(
                {
                    "capture_occurrence_id": cid,
                    "workspace_path": base.as_posix(),
                    "observed_file_count": len(files),
                    "withheld_changes": changes,
                    "partial_evidence": partial,
                    "unknown_mutation_barriers": barriers,
                    "status": "PARTIAL" if partial or barriers else "RECOVERED",
                }
            )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "replay_manifest.json",
                {
                    "schema_version": REPLAY_SCHEMA,
                    "replay_run_id": run_id,
                    "source_run": str(source.resolve()),
                    "capture_filter": capture_id,
                    "capture_count": len(captures),
                    "captures": captures,
                    "policy": {
                        "shell_execution": "FORBIDDEN",
                        "new_files": "EXCLUDED",
                        "writes": "WITHHELD",
                    },
                },
            )
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "metrics.json",
                {
                    "schema_version": "traceforge.trajectory-replay-metrics.v1",
                    "capture_count": len(captures),
                    "recovered_file_count": sum(item["observed_file_count"] for item in captures),
                    "withheld_change_count": sum(
                        len(item["withheld_changes"]) for item in captures
                    ),
                    "partial_evidence_count": sum(
                        len(item["partial_evidence"]) for item in captures
                    ),
                    "unknown_mutation_barrier_count": sum(
                        len(item["unknown_mutation_barriers"]) for item in captures
                    ),
                },
            )
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "artifact_manifest.json",
                {
                    "schema_version": "traceforge.trajectory-replay-artifact-manifest.v1",
                    "replay_run_id": run_id,
                    "files": artifact_entry_dicts(entries),
                },
            )
        )
        return workspace.publish()
    except BaseException:
        workspace.abort()
        raise
