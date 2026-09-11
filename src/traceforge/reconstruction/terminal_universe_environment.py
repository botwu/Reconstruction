"""Terminal-Universe aligned environment reconstruction primitives.

This module implements the paper's three environment stages without invoking a
model or a container: deterministic first-observation replay, constrained
agentic-completion validation, and Harbor-facing materialisation.  The model
adapter can use :func:`build_completion_prompt`; all writes are provenance
tracked and hidden changes never enter the public workspace.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

TERMINAL_UNIVERSE_ENVIRONMENT_PROMPT_VERSION = "terminal-universe-b1-environment-completion-v1"
TERMINAL_UNIVERSE_ENVIRONMENT_SCHEMA = "traceforge.terminal-universe-environment.v1"


class EnvironmentReconstructionError(ValueError):
    """Input or candidate violates the paper's reconstruction contract."""


def _safe_path(value: str) -> str:
    p = PurePosixPath(value.replace("\\", "/"))
    if not value or p.is_absolute() or ".." in p.parts or ":" in p.parts[0]:
        raise EnvironmentReconstructionError(f"unsafe path: {value!r}")
    if any(
        part in {"solution", "tests", "environment", "hidden_control", ".git"} for part in p.parts
    ):
        raise EnvironmentReconstructionError(f"hidden path is not public workspace: {value!r}")
    return p.as_posix().lstrip("./")


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _tool_name(event: dict[str, Any]) -> str:
    payload = _payload(event)
    fn = payload.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return fn["name"].lower()
    return str(payload.get("tool_name", "")).lower()


def _arguments(event: dict[str, Any]) -> dict[str, Any]:
    payload = _payload(event)
    fn = payload.get("function")
    args = fn.get("arguments") if isinstance(fn, dict) else payload.get("arguments")
    if isinstance(args, dict) and isinstance(args.get("value"), dict):
        args = args["value"]
    return args if isinstance(args, dict) else {}


def _call_id(event: dict[str, Any]) -> str:
    payload = _payload(event)
    return str(payload.get("tool_call_id") or payload.get("call_id") or "")


def _result_text(event: dict[str, Any]) -> str | None:
    payload = _payload(event)
    content = payload.get("content")
    if isinstance(content, dict) and isinstance(content.get("value"), str):
        return content["value"]
    return content if isinstance(content, str) else None


def _event_path(event: dict[str, Any]) -> str | None:
    args = _arguments(event)
    for key in ("path", "file_path", "filename", "file"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            try:
                return _safe_path(value.strip())
            except EnvironmentReconstructionError:
                return None
    return None


@dataclass(frozen=True, slots=True)
class ReplayedFile:
    path: str
    content: str
    first_observation_event_id: str
    completeness: str = "COMPLETE"
    provenance: str = "TRAJECTORY_FIRST_OBSERVATION"

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class WithheldChange:
    path: str | None
    event_id: str
    operation: str
    classification: str
    old_content_available: bool
    new_content_withheld: bool = True


@dataclass(frozen=True, slots=True)
class ReplayResult:
    files: tuple[ReplayedFile, ...]
    withheld_changes: tuple[WithheldChange, ...]
    partial_evidence: tuple[dict[str, Any], ...]
    unknown_mutation_barriers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TERMINAL_UNIVERSE_ENVIRONMENT_SCHEMA,
            "files": [
                asdict(item) | {"content_sha256": item.content_sha256} for item in self.files
            ],
            "withheld_changes": [asdict(item) for item in self.withheld_changes],
            "partial_evidence": list(self.partial_evidence),
            "unknown_mutation_barriers": list(self.unknown_mutation_barriers),
        }


def replay_initial_workspace(
    events: Iterable[dict[str, Any]], destination: str | Path | None = None
) -> ReplayResult:
    """Implement Terminal-Universe Stage 1.

    Only complete reads observed before the first mutation are materialised.
    Shell/unknown mutations are barriers; writes and agent-created files are
    withheld for the verifier and are never copied from a source directory.
    """
    ordered = sorted(events, key=lambda e: int(e.get("sequence_number", 0)))
    pending: dict[str, tuple[str, str, bool]] = {}
    observed: dict[str, ReplayedFile] = {}
    mutations: list[WithheldChange] = []
    partial: list[dict[str, Any]] = []
    barriers: list[str] = []
    mutation_started = False
    for event in ordered:
        kind = str(event.get("event_kind", ""))
        name = _tool_name(event)
        eid = str(event.get("event_occurrence_id") or event.get("id") or "unknown")
        path = _event_path(event)
        args = _arguments(event)
        call_id = _call_id(event)
        if kind == "TOOL_CALL" and name == "read" and path:
            if mutation_started:
                partial.append(
                    {"path": path, "reason": "read_after_first_mutation", "source_event_id": eid}
                )
                continue
            partial_read = any(key in args for key in ("offset", "limit", "line_start", "line_end"))
            if partial_read:
                partial.append(
                    {"path": path, "reason": "partial_read_range", "source_event_id": eid}
                )
                pending[call_id] = (path, eid, True)
            else:
                pending[call_id] = (path, eid, False)
        elif kind == "TOOL_RESULT" and call_id in pending:
            file_path, source_id, was_partial = pending.pop(call_id)
            text = _result_text(event)
            if text is not None and not was_partial and file_path not in observed:
                observed[file_path] = ReplayedFile(file_path, text, source_id)
        elif kind == "TOOL_CALL" and any(
            token in name
            for token in ("write", "edit", "patch", "replace", "create", "delete", "remove")
        ):
            mutation_started = True
            mutations.append(
                WithheldChange(
                    path,
                    eid,
                    name,
                    "withheld_change" if path in observed else "agent_created_file",
                    path in observed,
                )
            )
            if path in observed:
                prior = observed[path]
                observed[path] = ReplayedFile(
                    prior.path,
                    prior.content,
                    prior.first_observation_event_id,
                    "PARTIAL",
                    prior.provenance,
                )
                partial.append(
                    {"path": path, "reason": "modified_after_observation", "source_event_id": eid}
                )
        elif kind == "TOOL_CALL" and any(
            token in name
            for token in ("shell", "exec", "terminal", "command", "bash", "powershell")
        ):
            mutation_started = True
            barriers.append(eid)
    result = ReplayResult(
        tuple(observed[p] for p in sorted(observed)),
        tuple(mutations),
        tuple(partial),
        tuple(barriers),
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


def build_completion_prompt(
    task: dict[str, Any],
    replay: ReplayResult,
    evidence: list[dict[str, Any]],
    *,
    max_candidates: int = 3,
) -> str:
    """Return the B.1 contract, adapted from Terminal-Universe verbatim in spirit."""
    if max_candidates < 1 or max_candidates > 5:
        raise ValueError("max_candidates must be in [1,5]")
    public = [
        {
            "path": f.path,
            "content": f.content,
            "completeness": f.completeness,
            "event_id": f.first_observation_event_id,
        }
        for f in replay.files
    ]
    template = """You are reconstructing the initial Docker workspace for a coding task.
Complete the workspace so the task is solvable, but NOT solved (Terminal-Universe B.1).
Use only the supplied trajectory evidence. Create or complete only files that are
needed as context: do not implement the requested change, modify a COMPLETE file,
add tests, write a solution, reveal where the answer belongs, or include expected
outputs. The project root is /app. Preserve observed content exactly. Every new
file must cite one or more evidence_ref_ids. If evidence is insufficient, return
REVIEW with open_questions instead of guessing.
Return JSON only: {"candidates":[{"files":[{"path":"...","content":"...",
"provenance":"MODEL_COMPLETED","evidence_ref_ids":["..."]}],"dependencies":[],
"runtime_constraints":[],"uncertainties":[],"decision":"READY|REVIEW|DEFER|REJECT"}],
"open_questions":[]}. Generate at most {max_candidates} candidates.

TASK:\n{task_json}\nREPLAYED INITIAL FILES:\n{public_json}\nEVIDENCE INDEX:\n{evidence_json}"""
    return (
        template.replace("{max_candidates}", str(max_candidates))
        .replace("{task_json}", json.dumps(task, ensure_ascii=False, sort_keys=True))
        .replace("{public_json}", json.dumps(public, ensure_ascii=False))
        .replace("{evidence_json}", json.dumps(evidence, ensure_ascii=False))
    )


def _tokens(text: str) -> set[str]:
    return {x.lower() for x in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)}


def validate_completion_candidate(
    candidate: dict[str, Any],
    replay: ReplayResult,
    evidence_refs: set[str],
    *,
    max_file_bytes: int = 2_000_000,
) -> tuple[bool, tuple[str, ...]]:
    """Validate B.1 safety and answer non-leakage before materialisation."""
    if not isinstance(candidate, dict):
        return False, ("CANDIDATE_NOT_OBJECT",)
    files = candidate.get("files")
    if not isinstance(files, list):
        return False, ("FILES_MUST_BE_ARRAY",)
    errors: list[str] = []
    replay_map = {x.path: x for x in replay.files}
    withheld_paths = {x.path for x in replay.withheld_changes if x.path}
    for item in files:
        if not isinstance(item, dict):
            errors.append("FILE_NOT_OBJECT")
            continue
        try:
            path = _safe_path(str(item.get("path", "")))
        except EnvironmentReconstructionError as exc:
            errors.append(str(exc))
            continue
        if path in replay_map and replay_map[path].completeness in {"COMPLETE", "UNKNOWN"}:
            errors.append(f"PROTECTED_FILE_OVERWRITE:{path}")
        if path in withheld_paths and any(
            x.classification == "agent_created_file" and x.path == path
            for x in replay.withheld_changes
        ):
            errors.append(f"WITHHELD_CHANGE_PATH:{path}")
        content = item.get("content")
        if not isinstance(content, str) or len(content.encode()) > max_file_bytes:
            errors.append(f"INVALID_FILE_CONTENT:{path}")
            continue
        refs = item.get("evidence_ref_ids")
        if (
            not isinstance(refs, list)
            or not refs
            or any(str(ref) not in evidence_refs for ref in refs)
        ):
            errors.append(f"EVIDENCE_REF_UNKNOWN:{path}")
        if item.get("provenance", "MODEL_COMPLETED") != "MODEL_COMPLETED":
            errors.append(f"PROVENANCE_FORGERY:{path}")
        # Optional final content in richer replay records enables deterministic leak checking.
        for change in replay.withheld_changes:
            final = getattr(change, "final_content", None)
            if (
                isinstance(final, str)
                and final
                and (
                    final in content
                    or (
                        len(_tokens(final)) >= 4
                        and len(_tokens(final) & _tokens(content)) / len(_tokens(final)) >= 0.8
                    )
                )
            ):
                errors.append(f"SOLUTION_LEAKAGE:{path}")
    decision = str(candidate.get("decision", "REVIEW"))
    if decision not in {"READY", "REVIEW", "DEFER", "REJECT"}:
        errors.append("INVALID_DECISION")
    return not errors, tuple(errors)


def materialize_environment(
    replay: ReplayResult,
    candidate: dict[str, Any],
    destination: str | Path,
    *,
    evidence_refs: set[str],
) -> dict[str, Any]:
    """Write Harbor-compatible public ``workspace`` and private control metadata."""
    ok, errors = validate_completion_candidate(candidate, replay, evidence_refs)
    if not ok:
        raise EnvironmentReconstructionError(";".join(errors))
    root = Path(destination)
    if root.exists():
        shutil.rmtree(root)
    public = root / "workspace"
    hidden = root / "hidden_control"
    public.mkdir(parents=True)
    hidden.mkdir(parents=True)
    provenance: dict[str, str] = {}
    for item in replay.files:
        target = public / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.content, encoding="utf-8")
        provenance[item.path] = "REPLAYED"
    for item in candidate.get("files", []):
        path = _safe_path(str(item["path"]))
        target = public / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")
        provenance[path] = "MODEL_COMPLETED"
    (hidden / "withheld_changes.json").write_text(
        json.dumps([asdict(x) for x in replay.withheld_changes], ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": TERMINAL_UNIVERSE_ENVIRONMENT_SCHEMA,
        "public_workspace": "workspace",
        "hidden_control": "hidden_control",
        "provenance": provenance,
        "withheld_change_count": len(replay.withheld_changes),
        "candidate_decision": candidate.get("decision", "REVIEW"),
    }
    (root / "env_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


__all__ = [
    "TERMINAL_UNIVERSE_ENVIRONMENT_PROMPT_VERSION",
    "EnvironmentReconstructionError",
    "ReplayResult",
    "ReplayedFile",
    "WithheldChange",
    "build_completion_prompt",
    "materialize_environment",
    "replay_initial_workspace",
    "validate_completion_candidate",
]
