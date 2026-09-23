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
    if not value or not p.parts or p.is_absolute() or ".." in p.parts or ":" in p.parts[0]:
        raise EnvironmentReconstructionError(f"unsafe path: {value!r}")
    # 项目自身的 tests/ 是 task-start workspace 的可见源码；真正的隐藏区是
    # verifier/control 目录。Harbor 的 task/tests 与 workspace/tests 物理分离，
    # 不能把两个语义混成一个路径黑名单。
    parts = p.parts
    hidden = (
        "solution" in parts
        or "environment" in parts
        or "hidden_control" in parts
        or ".git" in parts
        or any(
            parts[index] == "tests" and index + 1 < len(parts) and parts[index + 1] == "control"
            for index in range(len(parts))
        )
    )
    if hidden:
        raise EnvironmentReconstructionError(f"hidden path is not public workspace: {value!r}")
    normalized = p.as_posix()
    return normalized[2:] if normalized.startswith("./") else normalized


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
    # Captured only when the original tool call exposes the written bytes.
    # Shell writes remain withheld with final_content=None.
    final_content: str | None = None
    provenance: str = "TRAJECTORY_MUTATION"


@dataclass(frozen=True, slots=True)
class ReplayResult:
    files: tuple[ReplayedFile, ...]
    withheld_changes: tuple[WithheldChange, ...]
    partial_evidence: tuple[dict[str, Any], ...]
    unknown_mutation_barriers: tuple[str, ...]
    workspace_root: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Replay artifacts may be published for debugging.  Never put withheld
        # solution bytes in that public artifact; the full value is retained
        # only in hidden_control/withheld_changes.json at materialisation time.
        public_changes = []
        for item in self.withheld_changes:
            row = asdict(item)
            final = row.pop("final_content", None)
            row["final_content_sha256"] = (
                hashlib.sha256(final.encode("utf-8")).hexdigest()
                if isinstance(final, str) else None
            )
            public_changes.append(row)
        return {
            "schema_version": TERMINAL_UNIVERSE_ENVIRONMENT_SCHEMA,
            "files": [
                asdict(item) | {"content_sha256": item.content_sha256} for item in self.files
            ],
            "withheld_changes": public_changes,
            "partial_evidence": list(self.partial_evidence),
            "unknown_mutation_barriers": list(self.unknown_mutation_barriers),
            "workspace_root": self.workspace_root,
        }


def select_max_exposed_trajectory(
    trajectories: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """按论文 B.1 在同一任务重复 rollout 中选 replay 暴露最多者。

    分组键是 repository、base commit 和 problem statement。评分只使用
    deterministic replay 暴露的文件数量、文本行数和字节数，不看最终 reward，
    避免把某一次策略的成功与环境质量混为一谈。
    """

    groups: dict[tuple[str, str, str], list[tuple[tuple[int, int, int, str], dict[str, Any]]]] = {}
    for item in trajectories:
        if not isinstance(item, dict):
            raise EnvironmentReconstructionError("trajectory 必须是对象")
        replay = item.get("replay")
        if not isinstance(replay, ReplayResult):
            raise EnvironmentReconstructionError("trajectory.replay 必须是 ReplayResult")
        identity = (
            str(item.get("repository") or item.get("repo") or ""),
            str(item.get("base_commit") or item.get("commit") or ""),
            str(item.get("problem_statement") or item.get("task") or ""),
        )
        if not all(identity):
            raise EnvironmentReconstructionError(
                "trajectory 缺少 repository/base_commit/problem_statement"
            )
        lines = sum(file.content.count("\n") + (1 if file.content else 0) for file in replay.files)
        bytes_count = sum(len(file.content.encode("utf-8")) for file in replay.files)
        score = (len(replay.files), lines, bytes_count, str(item.get("trajectory_id") or ""))
        groups.setdefault(identity, []).append((score, item))
    selected: list[dict[str, Any]] = []
    for rows in groups.values():
        selected.append(max(rows, key=lambda pair: pair[0])[1])
    return tuple(sorted(selected, key=lambda item: str(item.get("trajectory_id") or "")))


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
            args = _arguments(event)
            final_content = next(
                (args[key] for key in ("content", "contents", "new_content")
                 if isinstance(args.get(key), str)),
                None,
            )
            mutations.append(
                WithheldChange(
                    path,
                    eid,
                    name,
                    "withheld_change" if path in observed else "agent_created_file",
                    path in observed,
                    final_content=final_content,
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
    listing_names: frozenset[str] | set[str] | None = None,
    body_paths: frozenset[str] | set[str] | None = None,
    required_paths: list[str] | tuple[str, ...] | None = None,
    env_origin: str | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Validate B.1 safety and answer non-leakage before materialisation."""
    from traceforge.reconstruction.completion_holes import (
        _needs_real_generated_body,
        is_runtime_log,
        listing_stub_error,
    )
    from traceforge.reconstruction.environment_bindings import (
        expand_tree_paths,
        looks_like_synthetic_stub,
        path_present,
    )

    if not isinstance(candidate, dict):
        return False, ("CANDIDATE_NOT_OBJECT",)
    files = candidate.get("files")
    if not isinstance(files, list):
        return False, ("FILES_MUST_BE_ARRAY",)
    errors: list[str] = []
    for key in ("dependencies", "runtime_constraints", "uncertainties"):
        value = candidate.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(f"INVALID_{key.upper()}")
    if files and not replay.files and env_origin != "DEFAULT_EMPTY":
        errors.append("EMPTY_TREE_INVENTION")
    replay_map = {x.path: x for x in replay.files}
    listing = set(listing_names or ())
    bodies = set(body_paths or ())
    resolved: dict[str, str] = {path: item.content for path, item in replay_map.items()}
    seen_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            errors.append("FILE_NOT_OBJECT")
            continue
        try:
            path = _safe_path(str(item.get("path", "")))
        except EnvironmentReconstructionError as exc:
            errors.append(str(exc))
            continue
        if path in seen_paths:
            errors.append(f"DUPLICATE_FILE_PATH:{path}")
            continue
        seen_paths.add(path)
        if path in replay_map and replay_map[path].completeness in {"COMPLETE", "UNKNOWN"}:
            errors.append(f"PROTECTED_FILE_OVERWRITE:{path}")
        if is_runtime_log(path):
            errors.append(f"RUNTIME_LOG_NOT_WRITABLE:{path}")
        content = item.get("content")
        if not isinstance(content, str) or len(content.encode()) > max_file_bytes:
            errors.append(f"INVALID_FILE_CONTENT:{path}")
            continue
        stub_error = listing_stub_error(
            path,
            content,
            listing_names=listing,
            body_paths=bodies,
            replay_paths=set(replay_map),
            required_paths=required_paths,
        )
        if stub_error:
            errors.append(stub_error)
        resolved[path] = content
        if path in replay_map and replay_map[path].completeness == "PARTIAL":
            if replay_map[path].content not in content:
                errors.append(f"PARTIAL_OBSERVED_CONTENT_LOST:{path}")
        refs = item.get("evidence_ref_ids")
        if (
            not isinstance(refs, list)
            or not refs
            or any(str(ref) not in evidence_refs for ref in refs)
        ):
            errors.append(f"EVIDENCE_REF_UNKNOWN:{path}")
        provenance = item.get("provenance", "MODEL_COMPLETED")
        if provenance not in {"MODEL_COMPLETED", "SYNTHETIC_STUB", "NEIGHBOR"}:
            errors.append(f"PROVENANCE_FORGERY:{path}")
        if provenance == "SYNTHETIC_STUB" and _needs_real_generated_body(
            path,
            listing_names=listing,
            body_paths=bodies,
            replay_paths=set(replay_map),
            required_paths=required_paths,
        ):
            errors.append(f"BINDING_PATH_STUB_ONLY:{path}")
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
    present = expand_tree_paths(set(replay_map) | seen_paths)
    for required in required_paths or ():
        req = str(required)
        if not path_present(req, present):
            errors.append(f"BINDING_PATH_MISSING:{req}")
            continue
        if req.endswith("/"):
            continue
        body = resolved.get(req)
        if isinstance(body, str) and looks_like_synthetic_stub(body):
            errors.append(f"BINDING_PATH_STUB_ONLY:{req}")
    decision = str(candidate.get("decision", "REVIEW"))
    if decision not in {"READY", "REVIEW", "DEFER", "REJECT"}:
        errors.append("INVALID_DECISION")
    return not errors, tuple(errors)


def select_sufficient_candidate(
    candidates: Iterable[dict[str, Any]],
    sufficiency: Iterable[dict[str, Any]] | None = None,
) -> tuple[int | None, dict[str, Any]]:
    """Apply Terminal-Universe Stage 3 and deterministic candidate screening.

    A candidate is eligible only when both completion and the independent
    read-only sufficiency judge say ``READY/SUFFICIENT``.  Selection never
    uses rollout reward (that belongs to RED-check); ties are resolved by
    confidence, uncertainty count, then original order.
    """
    rows = list(candidates)
    judges = list(sufficiency or ())
    eligible: list[tuple[float, int, int]] = []
    rejected: list[dict[str, Any]] = []
    for index, candidate in enumerate(rows):
        if not isinstance(candidate, dict):
            rejected.append({"index": index, "reason": "CANDIDATE_NOT_OBJECT"})
            continue
        decision = str(candidate.get("decision", candidate.get("status", "REVIEW")))
        judge = judges[index] if index < len(judges) and isinstance(judges[index], dict) else {}
        label = str(judge.get("label", "UNKNOWN"))
        judge_decision = str(judge.get("decision", "REVIEW"))
        if decision == "SKIPPED_UNRECONSTRUCTABLE":
            rejected.append(
                {
                    "index": index,
                    "reason": "SKIPPED_UNRECONSTRUCTABLE",
                    "reason_codes": list(candidate.get("reason_codes") or []),
                }
            )
            continue
        if decision == "ENVIRONMENT_NOT_READY":
            # Sufficiency 回答“上下文是否足以尝试”；runtime preflight 是执行
            # 收据，不是第二道充分性闸门。未完成的收据仍交给下游 verifier/rollout；
            # 只有确定性的基础设施或管线故障才排除候选，REVIEW 仅作审计提示。
            environment_status = str(candidate.get("environment_status") or "REVIEW")
            if environment_status in {"INFRA_ERROR", "PIPELINE_ERROR"}:
                rejected.append(
                    {
                        "index": index,
                        "reason": "ENVIRONMENT_CONTRACT_NOT_READY",
                        "environment_status": environment_status,
                        "reason_codes": list(candidate.get("reason_codes") or []),
                    }
                )
                continue
            decision = "READY"
        if decision != "READY":
            rejected.append({"index": index, "reason": "COMPLETION_NOT_READY"})
            continue
        if label != "SUFFICIENT" or judge_decision != "READY":
            rejected.append({"index": index, "reason": "WORKSPACE_NOT_SUFFICIENT", "label": label})
            continue
        try:
            confidence = float(candidate.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        uncertainty_count = (
            len(candidate.get("uncertainties", []))
            if isinstance(candidate.get("uncertainties", []), list)
            else 0
        )
        eligible.append((confidence, -uncertainty_count, index))
    if not eligible:
        return None, {
            "status": "NO_SUFFICIENT_CANDIDATE",
            "eligible_count": 0,
            "rejected": rejected,
        }
    _, _, selected = max(eligible, key=lambda row: (row[0], row[1], -row[2]))
    return selected, {
        "status": "SELECTED",
        "selected_index": selected,
        "eligible_count": len(eligible),
        "rejected": rejected,
    }


def materialize_environment(
    replay: ReplayResult,
    candidate: dict[str, Any],
    destination: str | Path,
    *,
    evidence_refs: set[str],
    listing_names: frozenset[str] | set[str] | None = None,
    body_paths: frozenset[str] | set[str] | None = None,
    required_paths: list[str] | tuple[str, ...] | None = None,
    env_origin: str | None = None,
) -> dict[str, Any]:
    """Write Harbor-compatible public ``workspace`` and private control metadata."""
    ok, errors = validate_completion_candidate(
        candidate,
        replay,
        evidence_refs,
        listing_names=listing_names,
        body_paths=body_paths,
        required_paths=required_paths,
        env_origin=env_origin,
    )
    if not ok:
        raise EnvironmentReconstructionError(";".join(errors))
    root = Path(destination)
    if root.exists():
        shutil.rmtree(root)
    public = root / "workspace"
    hidden = root / "hidden_control"
    public.mkdir(parents=True)
    hidden.mkdir(parents=True)
    provenance: dict[str, Any] = {}
    for item in replay.files:
        target = public / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.content, encoding="utf-8")
        provenance[item.path] = {
            "kind": "REPLAYED",
            "evidence_ref_ids": [item.first_observation_event_id],
            "content_sha256": item.content_sha256,
        }
    for item in candidate.get("files", []):
        path = _safe_path(str(item["path"]))
        target = public / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")
        kind = str(item.get("provenance") or "MODEL_COMPLETED")
        if kind not in {"MODEL_COMPLETED", "SYNTHETIC_STUB", "NEIGHBOR"}:
            kind = "MODEL_COMPLETED"
        provenance[path] = {
            "kind": kind,
            "evidence_ref_ids": list(item.get("evidence_ref_ids", [])),
            "content_sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
        }
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
        "dependencies": list(candidate.get("dependencies", [])) if isinstance(candidate.get("dependencies", []), list) else [],
        "runtime_constraints": list(candidate.get("runtime_constraints", [])) if isinstance(candidate.get("runtime_constraints", []), list) else [],
        "uncertainties": list(candidate.get("uncertainties", [])) if isinstance(candidate.get("uncertainties", []), list) else [],
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
    "select_max_exposed_trajectory",
    "select_sufficient_candidate",
    "validate_completion_candidate",
]
