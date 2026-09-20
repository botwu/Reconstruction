"""Workspace Completion：两条互斥路径补全文件树，可解但未解。

- ``complete_from_replayed``：在回放树上按任务补邻域和正文
- ``complete_from_default_empty``：无回放时按任务和工具处理逻辑生成文件
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents import (
    COMPLETION_DEFAULT_EMPTY_ROLE,
    COMPLETION_REPLAYED_ROLE,
    AgentRuntime,
    AgentSession,
)
from traceforge.reconstruction.agents.runtime import AgentResult, merge_completion_files
from traceforge.reconstruction.agents.session import _debug_agent_log
from traceforge.reconstruction.completion_holes import CompletionIndex, index_completion_holes
from traceforge.reconstruction.environment_bindings import (
    environment_bindings,
    file_required_paths,
    missing_binding_paths,
)
from traceforge.reconstruction.terminal_universe_environment import (
    ReplayResult,
    materialize_environment,
    validate_completion_candidate,
)
from traceforge.reconstruction.tool_process_sketch import build_tool_process_sketch

COMPLETION_SCHEMA = "traceforge.workspace-completion.v1"
COMPLETION_PROMPT_VERSION = "workspace-completion-agent-v5-pre-task-context"
TASK_Q_EVIDENCE_ID = "task:q"
ENV_REPLAYED = "REPLAYED"
ENV_DEFAULT_EMPTY = "DEFAULT_EMPTY"
STRATEGY_REPLAYED = "from_replayed"
STRATEGY_DEFAULT_EMPTY = "from_default_empty"
MAX_CANDIDATES = 5


def timeline_evidence(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Losslessly expose the complete timeline to evidence tools.

    This is an index over original events, not a selected-span projection.
    Anonymous/duplicate calls receive deterministic local ids so every record
    remains addressable without dropping arguments or result bodies.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(timeline):
        if not isinstance(item, dict):
            continue
        ref = str(item.get("call_id") or f"timeline:{index}")
        if ref in seen:
            ref = f"{ref}@{index}"
        seen.add(ref)
        rows.append(
            _redact_value(dict(item))
            | {"evidence_ref_id": ref, "text": _redact_text(str(item.get("result_text") or ""))}
        )
    return rows


def _redact_text(value: str) -> str:
    return re.sub(
        r"(?i)(sk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|token|password)\s*[=:]\s*)[^\s,;]+",
        lambda match: (
            "<redacted>"
            if match.group(0).lower().startswith("sk-")
            else match.group(1) + "<redacted>"
        ),
        value,
    )


def _redact_value(value: Any, key: str = "") -> Any:
    markers = ("api_key", "apikey", "token", "password", "secret", "credential")
    if any(marker in key.lower() for marker in markers):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, key) for item in value]
    return _redact_text(value) if isinstance(value, str) else value


def _excerpt_ids(path: str, replay: ReplayResult, timeline: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    by_path = {item.path: item for item in replay.files}
    item = by_path.get(path)
    if item and item.first_observation_event_id:
        ids.append(item.first_observation_event_id)
    name = path.rsplit("/", 1)[-1]
    for event in timeline:
        if not isinstance(event, dict):
            continue
        cid = str(event.get("call_id") or "")
        if not cid or cid in ids:
            continue
        arguments = event.get("arguments")
        blob = json.dumps(arguments, ensure_ascii=False) if arguments else ""
        result = str(event.get("result_text") or "")
        if path in blob or path in result or (name and name in blob):
            ids.append(cid)
        if len(ids) >= 8:
            break
    return ids


def _hole_cards(
    replay: ReplayResult,
    holes: list[dict[str, Any]],
    timeline: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """给模型可引用的洞卡片：event_id 可直接当 evidence_ref，不必再翻完整 timeline。"""

    by_path = {item.path: item for item in replay.files}
    cards: list[dict[str, Any]] = []
    for hole in holes:
        card: dict[str, Any] = dict(hole)
        path = str(hole.get("path") or "")
        item = by_path.get(path)
        if item is not None:
            card["event_id"] = item.first_observation_event_id
            card["observed_chars"] = len(item.content)
            card["observed_prefix"] = item.content[:160]
        card["excerpt_event_ids"] = _excerpt_ids(path, replay, timeline or [])
        if not card.get("event_id") and card["excerpt_event_ids"]:
            card["event_id"] = card["excerpt_event_ids"][0]
        cards.append(card)
    return cards


def _shared_footer(
    *,
    task: dict[str, Any],
    evidence: list[dict[str, Any]],
    max_candidates: int,
    source: dict[str, Any] | None,
) -> list[str]:
    index = [
        {"evidence_ref_id": item.get("evidence_ref_id"), "name": item.get("name")}
        for item in evidence
    ]
    return [
        "A short stub such as 'body unobserved' is not a body; those paths must not READY.",
        "FILE environment_bindings required_paths must exist as real bodies, not placeholders.",
        "NON_FILE bindings are context; do not invent verifier files for them.",
        "Optional web_search is for typical layout names only. Do not write web source",
        "into user paths. Do not implement the task, write target tests, or overwrite COMPLETE.",
        "补全的是任务开始前的环境，不是用户要求新增的实现或测试。",
        "已有测试文件属于上下文：保留观察到的原始测试；不得生成针对目标修复的新测试。",
        "不得用全量 skip/pass/assert True 的测试骨架满足文件存在要求；无法恢复真实上下文时返回 REVIEW。",
        "Listing-only 路径只是线索，不要求批量生成。优先补齐任务必要源码及依赖。",
        "Tools: list_dir, read_file, list_evidence, read_evidence, write_file, web_search.",
        "Paths must be workspace-relative (foo.py) or /home/user/workspace/foo.py.",
        "Never use a host absolute path.",
        "Cite event_id as evidence_ref_ids. Do not page every evidence record.",
        "Do not write runtime logs.",
        "decision=READY only when required FILE/listing/binding bodies are real",
        "and you did not solve the task.",
        "After grounded writes, finish JSON. Do not keep exploring.",
        f"ENVIRONMENT_BINDINGS: {json.dumps(environment_bindings(task), ensure_ascii=False)}",
        f"Generate at most {max_candidates} candidates.",
        "Finish with JSON:",
        '{"candidates":[{"files":[{"path":"...","content":"...",'
        '"provenance":"MODEL_COMPLETED","evidence_ref_ids":["..."]}],'
        '"dependencies":[],"runtime_constraints":[],"uncertainties":[],'
        '"decision":"READY|REVIEW|DEFER|REJECT"}],"open_questions":[]}',
        "If you already wrote files with tools, files may be empty.",
        "TASK:",
        json.dumps(task, ensure_ascii=False, sort_keys=True),
        "EVIDENCE INDEX:",
        json.dumps(index, ensure_ascii=False),
        "TASK ANCHORS:",
        json.dumps((source or {}).get("selected_span_ids") or [], ensure_ascii=False),
    ]


def _replayed_instruction(
    task: dict[str, Any],
    replay: ReplayResult,
    evidence: list[dict[str, Any]],
    holes: list[dict[str, Any]],
    *,
    max_candidates: int,
    source: dict[str, Any] | None = None,
    topic_card: dict[str, Any] | None = None,
    timeline: list[dict[str, Any]] | None = None,
) -> str:
    public = [
        {
            "path": item.path,
            "completeness": item.completeness,
            "event_id": item.first_observation_event_id,
        }
        for item in replay.files
    ]
    protected = [item["path"] for item in public if item["completeness"] in {"COMPLETE", "UNKNOWN"}]
    return "\n".join(
        [
            "Complete a Docker workspace so the given task is solvable, but NOT solved.",
            "Strategy: from_replayed. The replayed tree is the initial environment.",
            "Leftover files and noise stay. REPLAYED bodies stay. COMPLETE is read-only.",
            "PARTIAL files must keep every observed excerpt; you may enrich and complete them.",
            "Add neighborhood files required by the task, grounded in the planted tree.",
            "If the replayed tree is empty, decision=REVIEW. Do not create a project.",
            "Listing-only and FILE binding holes are generation targets: write real,",
            "scene-consistent file bodies grounded in q, the planted tree, and evidence.",
            "SUPPORT files may be inferred from the observed project and TOPIC_CARDS.",
            "If a path has no neighborhood symbols and no evidence, omit it and return REVIEW.",
            "HOLES already lists path, kind, event_id, observed_prefix, excerpt_event_ids.",
            f"env_origin: {ENV_REPLAYED}",
            f"Empty replay tree: {json.dumps(not replay.files)}",
            f"Protected COMPLETE/UNKNOWN paths: {json.dumps(protected, ensure_ascii=False)}",
            f"TOPIC_CARDS: {json.dumps(topic_card or {}, ensure_ascii=False)}",
            f"HOLES: {json.dumps(_hole_cards(replay, holes, timeline), ensure_ascii=False)}",
            "REPLAYED FILE INDEX:",
            json.dumps(public, ensure_ascii=False),
            *_shared_footer(
                task=task,
                evidence=evidence,
                max_candidates=max_candidates,
                source=source,
            ),
        ]
    )


def _default_empty_instruction(
    task: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    max_candidates: int,
    source: dict[str, Any] | None = None,
    sketch: dict[str, Any] | None = None,
) -> str:
    return "\n".join(
        [
            "Complete a Docker workspace so the given task is solvable, but NOT solved.",
            "Strategy: from_default_empty. The initial environment is an empty seed,",
            "not a replayed tree. Generated files are MODEL_COMPLETED, never REPLAYED.",
            "Generate real, scene-consistent file bodies from q and TOOL_PROCESS_SKETCH.",
            "Use tool names, argument paths, and result shapes as generation constraints.",
            (
                f"Cite {TASK_Q_EVIDENCE_ID} or a timeline event_id. "
                "Do not write 'body unobserved' stubs."
            ),
            "decision=REVIEW if the sketch has no process logic and q names no files.",
            f"env_origin: {ENV_DEFAULT_EMPTY}",
            f"TOOL_PROCESS_SKETCH: {json.dumps(sketch or {}, ensure_ascii=False)}",
            *_shared_footer(
                task=task,
                evidence=evidence,
                max_candidates=max_candidates,
                source=source,
            ),
        ]
    )


def _instruction(
    task: dict[str, Any],
    replay: ReplayResult,
    evidence: list[dict[str, Any]],
    holes: list[dict[str, Any]],
    *,
    max_candidates: int,
    source: dict[str, Any] | None = None,
    topic_card: dict[str, Any] | None = None,
    timeline: list[dict[str, Any]] | None = None,
    env_origin: str = ENV_REPLAYED,
    sketch: dict[str, Any] | None = None,
) -> str:
    if env_origin == ENV_DEFAULT_EMPTY:
        return _default_empty_instruction(
            task,
            evidence,
            max_candidates=max_candidates,
            source=source,
            sketch=sketch,
        )
    return _replayed_instruction(
        task,
        replay,
        evidence,
        holes,
        max_candidates=max_candidates,
        source=source,
        topic_card=topic_card,
        timeline=timeline,
    )


def _empty_candidate() -> dict[str, Any]:
    return {
        "files": [],
        "dependencies": [],
        "runtime_constraints": [],
        "uncertainties": [],
        "decision": "READY",
    }


def _validate_kwargs(
    index: CompletionIndex,
    task: dict[str, Any] | None = None,
    env_origin: str = ENV_REPLAYED,
) -> dict[str, Any]:
    return {
        "listing_names": index.listing_names,
        "body_paths": index.body_paths,
        "required_paths": file_required_paths(task),
        "env_origin": env_origin,
    }


def complete_from_replayed(
    *,
    task: dict[str, Any],
    replay: ReplayResult,
    timeline: list[dict[str, Any]],
    agent: AgentRuntime,
    output_root: str | Path,
    max_candidates: int = 3,
    workspace_root: str | Path | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """路径 A：在回放树上按任务补邻域文件和正文。"""

    return _run_completion(
        task=task,
        replay=replay,
        timeline=timeline,
        agent=agent,
        output_root=output_root,
        max_candidates=max_candidates,
        workspace_root=workspace_root,
        source=source,
        env_origin=ENV_REPLAYED,
    )


def complete_from_default_empty(
    *,
    task: dict[str, Any],
    timeline: list[dict[str, Any]],
    agent: AgentRuntime,
    output_root: str | Path,
    replay: ReplayResult | None = None,
    max_candidates: int = 3,
    workspace_root: str | Path | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """路径 B：按任务和工具处理逻辑从空树生成文件。不得冒充 Replay。"""

    return _run_completion(
        task=task,
        replay=replay or ReplayResult((), (), (), ()),
        timeline=timeline,
        agent=agent,
        output_root=output_root,
        max_candidates=max_candidates,
        workspace_root=workspace_root,
        source=source,
        env_origin=ENV_DEFAULT_EMPTY,
    )


def run_workspace_completion(
    *,
    task: dict[str, Any],
    replay: ReplayResult,
    timeline: list[dict[str, Any]],
    agent: AgentRuntime,
    output_root: str | Path,
    max_candidates: int = 3,
    workspace_root: str | Path | None = None,
    source: dict[str, Any] | None = None,
    env_origin: str = ENV_REPLAYED,
) -> dict[str, Any]:
    """按 env_origin 分发到互斥的 Completion 策略。"""

    if env_origin == ENV_DEFAULT_EMPTY:
        return complete_from_default_empty(
            task=task,
            timeline=timeline,
            agent=agent,
            output_root=output_root,
            replay=replay,
            max_candidates=max_candidates,
            workspace_root=workspace_root,
            source=source,
        )
    return complete_from_replayed(
        task=task,
        replay=replay,
        timeline=timeline,
        agent=agent,
        output_root=output_root,
        max_candidates=max_candidates,
        workspace_root=workspace_root,
        source=source,
    )


def _run_completion(
    *,
    task: dict[str, Any],
    replay: ReplayResult,
    timeline: list[dict[str, Any]],
    agent: AgentRuntime,
    output_root: str | Path,
    max_candidates: int,
    workspace_root: str | Path | None,
    source: dict[str, Any] | None,
    env_origin: str,
) -> dict[str, Any]:
    """Completion 共用物化与门禁；prompt 和跳过策略按 origin 分开。"""

    if not 1 <= max_candidates <= MAX_CANDIDATES:
        raise ValueError(f"max_candidates must be between 1 and {MAX_CANDIDATES}")
    origin = ENV_DEFAULT_EMPTY if env_origin == ENV_DEFAULT_EMPTY else ENV_REPLAYED
    evidence = timeline_evidence(timeline)
    public_timeline = list(timeline)
    if origin == ENV_REPLAYED:
        # Replay barriers are private audit facts. A read after an unknown or
        # known mutation must not re-enter Completion as trusted evidence,
        # including through hole cards and listing extraction.
        blocked_reasons = {
            "unparsed_mutation_scope",
            "unparsed_mutation_unscoped",
            "read_after_unparsed_mutation",
            "read_after_first_mutation",
            "modified_after_observation",
        }
        blocked_refs = {
            str(item.get("source_event_id"))
            for item in (getattr(replay, "partial_evidence", ()) or ())
            if isinstance(item, dict)
            and item.get("reason") in blocked_reasons
            and item.get("source_event_id")
        }
        evidence = [
            item for item in evidence
            if str(item.get("evidence_ref_id") or "") not in blocked_refs
        ]
        public_timeline = [
            item for item in timeline
            if not isinstance(item, dict)
            or str(item.get("call_id") or "") not in blocked_refs
        ]
    strategy = STRATEGY_DEFAULT_EMPTY if origin == ENV_DEFAULT_EMPTY else STRATEGY_REPLAYED
    role = (
        COMPLETION_DEFAULT_EMPTY_ROLE
        if origin == ENV_DEFAULT_EMPTY
        else COMPLETION_REPLAYED_ROLE
    )
    sketch = (
        build_tool_process_sketch(timeline, task=task)
        if origin == ENV_DEFAULT_EMPTY
        else None
    )
    if origin == ENV_DEFAULT_EMPTY and not any(
        str(item.get("evidence_ref_id")) == TASK_Q_EVIDENCE_ID for item in evidence
    ):
        evidence.append(
            {
                "evidence_ref_id": TASK_Q_EVIDENCE_ID,
                "name": "task",
                "text": str(task.get("task_instruction") or task.get("core_objective") or ""),
            }
        )
    refs = {str(item["evidence_ref_id"]) for item in evidence}
    hole_index = index_completion_holes(replay, public_timeline, task)
    holes = hole_index.as_list()
    instruction = _instruction(
        task,
        replay,
        evidence,
        holes,
        max_candidates=max_candidates,
        source=source,
        topic_card=hole_index.topic_card,
        timeline=public_timeline,
        env_origin=origin,
        sketch=sketch,
    )
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    session = AgentSession(
        workspace=Path(workspace_root) if workspace_root else None,
        evidence=evidence,
        replay_files={item.path: item.content for item in replay.files},
        protected_paths={
            item.path
            for item in replay.files
            if item.completeness in {"COMPLETE", "UNKNOWN"}
        },
        partial_files={
            item.path: item.content
            for item in replay.files
            if item.completeness == "PARTIAL"
        },
        listing_names=set(hole_index.listing_names),
        body_paths=set(hole_index.body_paths),
        required_paths=set(file_required_paths(task)),
        allow_write=True,
    )
    skip_reason: str | None = None
    missing_bindings = missing_binding_paths({item.path for item in replay.files}, task)
    replay_only_ok = (
        origin == ENV_REPLAYED
        and bool(replay.files)
        and not hole_index.holes
        and not missing_bindings
    )
    if not replay.files and origin != ENV_DEFAULT_EMPTY:
        skip_reason = "EMPTY_REPLAY_TREE"
    elif origin == ENV_DEFAULT_EMPTY and sketch is not None and not sketch.get("sufficient"):
        skip_reason = "TOOL_PROCESS_INSUFFICIENT"
    if skip_reason:
        ran = AgentResult(
            role=role.name,
            backend=str(getattr(agent, "backend", "") or ""),
            payload={"candidates": [_empty_candidate()], "open_questions": []},
            errors=[],
            final_text="",
            completed=True,
        )
    else:
        ran = agent.run(
            role=role,
            instruction=instruction,
            session=session,
            output_root=root,
        )
    errors = list(ran.errors)
    sandbox_init = [item for item in errors if str(item).startswith("SANDBOX_INIT")]
    if sandbox_init:
        result = {
            "schema_version": COMPLETION_SCHEMA,
            "prompt_version": COMPLETION_PROMPT_VERSION,
            "env_origin": origin,
            "completion_strategy": strategy,
            "status": "REVIEW",
            "errors": sandbox_init,
            "holes": holes,
            "agent": {
                "role": role.name,
                "backend": ran.backend,
                "turns": len(ran.turns),
                "completed": False,
                "skipped": False,
                "skip_reason": None,
            },
            "evidence_ref_ids": sorted(refs),
            "open_questions": [],
            "diagnostics": {"payload_keys": [], "tool_write_count": 0, "tool_event_count": 0},
            "candidates": [],
        }
        (root / "completion.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return result
    objective = task.get("core_objective") or task.get("task_instruction")
    if not isinstance(objective, str) or not objective.strip():
        errors.append("TASK_NOT_EXECUTABLE")
    if skip_reason == "EMPTY_REPLAY_TREE":
        errors.append("EMPTY_REPLAY_TREE")
    if skip_reason == "TOOL_PROCESS_INSUFFICIENT":
        errors.append("TOOL_PROCESS_INSUFFICIENT")
    if skip_reason is None and getattr(agent, "backend", "") != "hermes-sandbox":
        errors.append("CONTAINER_REQUIRED")
    if skip_reason is None and not ran.completed:
        errors.append("AGENT_INCOMPLETE")
    if skip_reason is None and not isinstance(ran.payload, dict):
        errors.append("COMPLETION_PAYLOAD_NOT_OBJECT")
    # Keep the model payload even when the run is incomplete or policy-failed.
    # It is diagnostic only in that case; global errors still make the stage
    # fail closed, but we must not erase candidate/open-question evidence.
    base_payload = dict(ran.payload) if isinstance(ran.payload, dict) else {}
    payload = merge_completion_files(base_payload, session)
    if isinstance(payload, dict):
        errors.extend(
            str(item)
            for item in payload.pop("_merge_errors", [])
            if isinstance(item, str)
        )
    raw_candidates: list[dict[str, Any]] = []
    maybe = payload.get("candidates")
    if isinstance(maybe, list):
        if len(maybe) > max_candidates:
            errors.append("CANDIDATE_LIMIT_EXCEEDED")
        if any(not isinstance(item, dict) for item in maybe):
            errors.append("CANDIDATE_NOT_OBJECT")
        raw_candidates = [item for item in maybe if isinstance(item, dict)]
    elif payload:
        errors.append("CANDIDATES_NOT_ARRAY")
    # A non-file conversation is never silently promoted to a reconstructable
    # environment. Empty replay trees skip the model and stay REVIEW.

    records: list[dict[str, Any]] = []
    candidate_gate_errors: list[str] = []
    for index, candidate in enumerate(raw_candidates):
        ok, candidate_errors = validate_completion_candidate(
            candidate, replay, refs, **_validate_kwargs(hole_index, task, origin)
        )
        schema_errors = [
            f"INVALID_{key.upper()}"
            for key in ("dependencies", "runtime_constraints", "uncertainties")
            if not isinstance(candidate.get(key), list)
            or any(not isinstance(value, str) for value in candidate.get(key, []))
        ]
        candidate_errors = (*candidate_errors, *schema_errors)
        ok = ok and not schema_errors
        # A protected overwrite is a hard candidate failure.  Never silently
        # remove the offending file: doing so hides an agent policy violation.
        record = {
            "index": index,
            "decision": candidate.get("decision", "REVIEW"),
            "valid": ok,
            "errors": list(candidate_errors),
            "workspace": None,
            "dependencies": list(candidate.get("dependencies", []))
            if isinstance(candidate.get("dependencies", []), list)
            else [],
            "runtime_constraints": list(candidate.get("runtime_constraints", []))
            if isinstance(candidate.get("runtime_constraints", []), list)
            else [],
            "uncertainties": list(candidate.get("uncertainties", []))
            if isinstance(candidate.get("uncertainties", []), list)
            else [],
            "file_provenance": [
                {
                    "path": item.get("path"),
                    "evidence_ref_ids": item.get("evidence_ref_ids", []),
                    "provenance": item.get("provenance", "MODEL_COMPLETED"),
                }
                for item in candidate.get("files", [])
                if isinstance(item, dict)
            ],
        }
        # #region agent log
        _debug_agent_log(
            "H8",
            "workspace_completion.py:materialize_gate",
            "materialize_decision",
            {
                "index": index,
                "ok": ok,
                "ran_completed": ran.completed,
                "global_errors": list(errors),
                "candidate_decision": candidate.get("decision"),
                "will_materialize": bool(
                    ok
                    and not errors
                    and ran.completed
                    and candidate.get("decision") == "READY"
                ),
            },
        )
        # #endregion
        if (
            ok
            and not errors
            and ran.completed
            and candidate.get("decision") == "READY"
        ):
            dest = root / "candidates" / f"{index:03d}"
            manifest = materialize_environment(
                replay,
                candidate,
                dest,
                evidence_refs=refs,
                **_validate_kwargs(hole_index, task, origin),
            )
            record["workspace"] = str((dest / "workspace").resolve())
            record["env_root"] = str(dest.resolve())
            record["manifest"] = manifest
        else:
            record["decision"] = "REVIEW"
            if candidate_errors:
                candidate_gate_errors.extend(
                    f"CANDIDATE_{index:03d}:{item}" for item in candidate_errors
                )
            elif candidate.get("decision") != "READY":
                candidate_gate_errors.append(
                    f"CANDIDATE_{index:03d}:MODEL_DECISION_{candidate.get('decision', 'REVIEW')}"
                )
        records.append(record)

    ready = [item for item in records if item["decision"] == "READY" and item["workspace"]]
    if (
        skip_reason is None
        and origin == ENV_REPLAYED
        and replay_only_ok
        and not ready
        and not sandbox_init
        and ran.completed
        and not errors
    ):
        dest = root / "candidates" / "replay-tree"
        manifest = materialize_environment(
            replay,
            _empty_candidate(),
            dest,
            evidence_refs=refs,
            **_validate_kwargs(hole_index, task, origin),
        )
        records.append(
            {
                "index": len(records),
                "decision": "READY",
                "valid": True,
                "errors": [],
                "workspace": str((dest / "workspace").resolve()),
                "env_root": str(dest.resolve()),
                "manifest": manifest,
                "dependencies": [],
                "runtime_constraints": [],
                "uncertainties": [],
                "file_provenance": [],
            }
        )
        ready = [item for item in records if item["decision"] == "READY" and item["workspace"]]
    status = "READY" if ready else "REVIEW"
    if skip_reason is None and not raw_candidates and not errors and not ready:
        errors.append("NO_CANDIDATES")
        status = "REVIEW"
    if not ready:
        errors.extend(candidate_gate_errors)
    errors = list(dict.fromkeys(str(item) for item in errors if str(item)))
    # #region agent log
    _debug_agent_log(
        "H4",
        "workspace_completion.py:run_workspace_completion",
        "completion_status_assembled",
        {
            "agent_backend": getattr(agent, "backend", ""),
            "ran_backend": ran.backend,
            "ran_completed": ran.completed,
            "skip_reason": skip_reason,
            "status": status,
            "errors": list(errors),
            "holes": holes,
            "payload_keys": (
                sorted(str(key) for key in payload)
                if isinstance(payload, dict)
                else []
            ),
            "candidate_count": len(records),
            "ready_count": len(ready),
            "container_required": skip_reason is None
            and getattr(agent, "backend", "") != "hermes-sandbox",
        },
    )
    # #endregion
    result = {
        "schema_version": COMPLETION_SCHEMA,
        "prompt_version": COMPLETION_PROMPT_VERSION,
        "env_origin": origin,
        "completion_strategy": strategy,
        "tool_process_sketch": sketch,
        "status": status,
        "errors": errors,
        "holes": holes,
        "agent": {
            "role": role.name,
            "backend": ran.backend,
            "turns": len(ran.turns),
            "completed": ran.completed,
            "skipped": skip_reason is not None,
            "skip_reason": skip_reason,
        },
        "evidence_ref_ids": sorted(refs),
        "open_questions": payload.get("open_questions", [])
        if isinstance(payload.get("open_questions", []), list)
        else [],
        "diagnostics": {
            "payload_keys": sorted(str(key) for key in payload),
            "tool_write_count": len(session.writes),
            "tool_event_count": len(session.tool_events),
        },
        "candidates": records,
    }
    (root / "completion.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    private = root / "private"
    private.mkdir(exist_ok=True)
    (private / "model_exchange.json").write_text(
        json.dumps(
            {
                "schema_version": "traceforge.private-model-exchange.v1",
                "request": {
                    "model": agent.model_name,
                    "prompt_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
                    "prompt": instruction,
                    "system": role.identity,
                },
                "response": {
                    "text": _redact_text(ran.final_text or ""),
                    "receipt": None,
                    "skipped": skip_reason is not None,
                    "skip_reason": skip_reason,
                },
                "credentials_embedded": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return result
