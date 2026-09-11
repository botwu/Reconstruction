"""AgentRx multi-stage trajectory diagnosis adapted to TraceForge.

The stages follow AgentRx (MIT, Microsoft): static invariants, dynamic invariants
for every prefix, invariant checking, and taxonomy based root-cause judgement.
Generation is model-backed through :class:`ChatModel`; no generated Python is
executed on the host. Python checks are explicitly marked NEEDS_SANDBOX or
NOT_RUN and natural-language checks remain UNCLEAR when evidence is insufficient.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

from traceforge.reconstruction.model_gateway import (
    ChatModel,
    ModelGatewayError,
    ModelRequest,
    parse_json_object,
    receipt_for_response,
)
from traceforge.trajectory.json_codec import stable_id

try:
    from .reference_prompts import prompt_context as _reference_prompt_context
except Exception:  # assets may be unavailable in a minimal source checkout
    def _reference_prompt_context() -> str:
        return PROMPT_SOURCE

from .agentrx_contracts import (
    AgentRxReport,
    CheckResult,
    CheckStatus,
    CheckType,
    Invariant,
    StageReceipt,
    TrajectoryEvent,
    TrajectoryIR,
)
from .reference_prompts import root_cause_guidance, taxonomy_data

SCHEMA_VERSION = "traceforge.agentrx-diagnosis.v1"
PROMPT_SOURCE = "AgentRx@f228165b (MIT; Microsoft Corporation)"
STATIC_PROMPT_VERSION = "agentrx-static-v1"
DYNAMIC_PROMPT_VERSION = "agentrx-dynamic-prefix-v1"
JUDGE_PROMPT_VERSION = "agentrx-failure-judge-v1"

# Verbatim concepts from AgentRx static/dynamic prompt templates are retained:
# rubric checks only CLEAR evidence, previous assertions are reused, and outputs
# contain executable-check metadata. The surrounding text is adapted to our IR.
STATIC_PROMPT = """You are AgentRx's static invariant generator. Analyze the task instruction and
trajectory schema below and propose policy, precondition, ordering, business-rule
and single-use invariants that can be checked from events. Generate only objective,
non-trivial assertions. Return JSON with invariant criterion, check_type, code,
trigger_step, evidence_ref_ids, and confidence. Never invent evidence IDs."""
DYNAMIC_PROMPT = """You are AgentRx's dynamic invariant generator.
Generate checks for THIS PREFIX only.
Reuse prior assertions and add only computation, cross-tool consistency, and
step-specific checks not covered by static assertions. Return the same JSON schema
with an `invariants` array. Evidence must cite supplied event/evidence IDs."""
JUDGE_PROMPT = """You are AgentRx's failure judge. Given a complete trajectory and results,
locate the first unresolved violation and classify it using taxonomy values:
INSTRUCTION_OR_PLAN_ADHERENCE_FAILURE, INVENTION_OF_NEW_INFORMATION, INVALID_INVOCATION,
MISINTERPRETATION_OF_TOOL_OUTPUT, INTENT_PLAN_MISALIGNMENT, UNDERSPECIFIED_USER_INTENT,
INTENT_NOT_SUPPORTED, GUARDRAILS_TRIGGERED, SYSTEM_FAILURE, INCONCLUSIVE.
Return JSON with reason_for_failure, failure_case, failure_step, confidence,
and evidence_ref_ids fields."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_trajectory(raw: dict[str, Any]) -> TrajectoryIR:
    """Normalize strict AgentRx IR (step/event IDs and evidence mapping)."""
    if not isinstance(raw, dict):
        raise ValueError("TRAJECTORY_MUST_BE_OBJECT")
    tid, instruction = (
        str(raw.get("trajectory_id", "")).strip(),
        str(raw.get("instruction", "")).strip(),
    )
    if not tid or not instruction:
        raise ValueError("TRAJECTORY_ID_AND_INSTRUCTION_REQUIRED")
    events: list[TrajectoryEvent] = []
    seen: set[str] = set()
    for pos, step in enumerate(raw.get("steps") or []):
        if not isinstance(step, dict):
            raise ValueError(f"STEP_{pos}_MUST_BE_OBJECT")
        idx = step.get("index", pos)
        if not isinstance(idx, int) or idx < 0:
            raise ValueError(f"STEP_{pos}_INDEX_INVALID")
        subs = step.get("substeps")
        if not isinstance(subs, list) or not subs:
            # Accept compact step form with event_id/content.
            subs = [step]
        for subpos, sub in enumerate(subs, 1):
            if not isinstance(sub, dict):
                raise ValueError(f"STEP_{idx}_SUBSTEP_{subpos}_INVALID")
            raw_event_id = sub.get("event_id")
            if not raw_event_id and len(subs) == 1:
                raw_event_id = step.get("event_id")
            event_id = str(raw_event_id).strip() if raw_event_id is not None else ""
            if not event_id:
                raise ValueError(f"STEP_{idx}_EVENT_ID_REQUIRED")
            if event_id in seen:
                raise ValueError(f"DUPLICATE_EVENT_ID:{event_id}")
            seen.add(event_id)
            refs = sub.get("evidence_ref_ids", step.get("evidence_ref_ids", ()))
            if refs is None:
                refs = ()
            if not isinstance(refs, list | tuple) or any(
                not isinstance(x, str) or not x.strip() for x in refs
            ):
                raise ValueError(f"EVENT_{event_id}_EVIDENCE_REFS_INVALID")
            content = str(sub.get("content", sub.get("message", "")))
            events.append(
                TrajectoryEvent(
                    event_id, idx, str(sub.get("role", "unknown")), content, tuple(refs), dict(sub)
                )
            )
    if not events:
        raise ValueError("TRAJECTORY_STEPS_REQUIRED")
    return TrajectoryIR(tid, instruction, tuple(events))


def _event_payload(ir: TrajectoryIR, upto: int | None = None) -> list[dict[str, Any]]:
    xs = ir.events if upto is None else tuple(e for e in ir.events if e.step_index <= upto)
    return [
        {
            "event_id": e.event_id,
            "step_index": e.step_index,
            "role": e.role,
            "content": e.content,
            "evidence_ref_ids": list(e.evidence_ref_ids),
        }
        for e in xs
    ]


def _known_refs(ir: TrajectoryIR) -> set[str]:
    return {r for e in ir.events for r in e.evidence_ref_ids}


def _request(model_name: str, prompt: str, stage: str, trajectory_id: str) -> ModelRequest:
    rid = stable_id(
        "traceforge.agentrx.request.v1",
        {"trajectory_id": trajectory_id, "stage": stage, "prompt": prompt},
    )
    return ModelRequest(
        rid,
        model_name,
        "You are a strict JSON-only AgentRx verifier.",
        prompt,
        "json_object",
        temperature=0.0,
        max_tokens=4096,
    )


def _call(
    model: ChatModel, model_name: str, prompt: str, stage: str, tid: str
) -> tuple[dict[str, Any] | None, StageReceipt]:
    req = _request(model_name, prompt, stage, tid)
    try:
        response = model.complete(req)
        payload = parse_json_object(response.text)
        return payload, StageReceipt(
            stage, "COMPLETED", req.request_id, asdict(receipt_for_response(response))
        )
    except ModelGatewayError as exc:
        return None, StageReceipt(stage, "FAILED", req.request_id, None, exc.code)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None, StageReceipt(stage, "FAILED", req.request_id, None, "INVALID_JSON")


def _parse_invariants(
    payload: dict[str, Any] | None, kind: str, tid: str, prefix: int | None, refs: set[str]
) -> tuple[tuple[Invariant, ...], list[str]]:
    errors: list[str] = []
    if not payload:
        return (), [f"{kind}_EMPTY_RESPONSE"]
    values = payload.get("invariants", payload.get("invariant", []))
    if not isinstance(values, list):
        return (), [f"{kind}_INVARIANTS_MUST_BE_ARRAY"]
    out: list[Invariant] = []
    for n, item in enumerate(values):
        if not isinstance(item, dict):
            errors.append(f"{kind}_{n}_NOT_OBJECT")
            continue
        criterion = str(item.get("criterion", "")).strip()
        check_type = str(item.get("check_type", "nl_check")).strip()
        if not criterion or check_type not in {CheckType.PYTHON.value, CheckType.NL.value}:
            errors.append(f"{kind}_{n}_SCHEMA_INVALID")
            continue
        evrefs = item.get("evidence_ref_ids", [])
        if not isinstance(evrefs, list) or any(not isinstance(x, str) for x in evrefs):
            errors.append(f"{kind}_{n}_EVIDENCE_REFS_INVALID")
            continue
        unknown = [x for x in evrefs if x not in refs]
        if unknown:
            errors.append(f"{kind}_{n}_UNKNOWN_EVIDENCE:{','.join(unknown)}")
            continue
        confidence = item.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.0
        inv_id = stable_id(
            "traceforge.agentrx.invariant.v1",
            {
                "trajectory_id": tid,
                "kind": kind,
                "prefix": prefix,
                "criterion": criterion,
                "refs": evrefs,
            },
        )
        out.append(
            Invariant(
                inv_id,
                kind,
                check_type,
                criterion,
                item.get("trigger_step", prefix),
                item.get("code"),
                tuple(evrefs),
                PROMPT_SOURCE,
                confidence,
            )
        )
    return tuple(out), errors


def _check(invariants: Iterable[Invariant]) -> tuple[CheckResult, ...]:
    out: list[CheckResult] = []
    for inv in invariants:
        if inv.check_type == CheckType.NL.value:
            status, reason = CheckStatus.UNCLEAR.value, "NL_CHECK_REQUIRES_EVIDENCE_JUDGE"
        elif inv.code and isinstance(inv.code, str) and inv.code.strip():
            status, reason = CheckStatus.NEEDS_SANDBOX.value, "PYTHON_CHECK_NOT_EXECUTED_ON_HOST"
        else:
            status, reason = CheckStatus.NOT_RUN.value, "PYTHON_CHECK_CODE_MISSING"
        out.append(
            CheckResult(
                inv.invariant_id,
                inv.check_type,
                status,
                inv.trigger_step,
                inv.evidence_ref_ids,
                reason,
            )
        )
    return tuple(out)


def run_agentrx_diagnosis(
    trajectory: dict[str, Any] | TrajectoryIR,
    model: ChatModel,
    *,
    model_name: str = "claude-opus-4-8",
) -> AgentRxReport:
    """Execute all AgentRx stages and return an auditable structured report."""
    ir = trajectory if isinstance(trajectory, TrajectoryIR) else normalize_trajectory(trajectory)
    refs = _known_refs(ir)
    receipts: list[StageReceipt] = []
    errors: list[str] = []
    static_prompt = (
        STATIC_PROMPT
        + "\n\nREFERENCE TAXONOMY:\n"
        + _json(taxonomy_data())
        + "\n\nTASK:\n"
        + ir.instruction
        + "\n\nTRAJECTORY:\n"
        + _json(_event_payload(ir))
        + "\n\nSTAGE:static_invariants"
        + f"\n\nPROMPT_SOURCE:{PROMPT_SOURCE};VERSION:{STATIC_PROMPT_VERSION}"
    )
    static_payload, receipt = _call(
        model, model_name, static_prompt, "static_invariants", ir.trajectory_id
    )
    receipts.append(receipt)
    static, parse_errors = _parse_invariants(static_payload, "STATIC", ir.trajectory_id, None, refs)
    errors.extend(parse_errors)
    dynamic_groups: list[tuple[Invariant, ...]] = []
    previous = [asdict(i) for i in static]
    for step_idx in sorted({e.step_index for e in ir.events}):
        prompt = (
            DYNAMIC_PROMPT
            + "\n\n"
            + root_cause_guidance()
            + "\n\nTASK:\n"
            + ir.instruction
            + "\n\nPREFIX:\n"
            + _json(_event_payload(ir, step_idx))
            + "\n\nPREVIOUS:\n"
            + _json(previous)
            + f"\n\nPROMPT_SOURCE:{PROMPT_SOURCE};VERSION:{DYNAMIC_PROMPT_VERSION}"
        )
        payload, rec = _call(
            model, model_name, prompt, f"dynamic_prefix_{step_idx}", ir.trajectory_id
        )
        receipts.append(rec)
        group, pe = _parse_invariants(payload, "DYNAMIC", ir.trajectory_id, step_idx, refs)
        errors.extend(pe)
        dynamic_groups.append(group)
        previous.extend(asdict(i) for i in group)
    all_inv = static + tuple(i for group in dynamic_groups for i in group)
    checks = _check(all_inv)
    judge_context = {
        "trajectory": _event_payload(ir),
        "invariants": [asdict(i) for i in all_inv],
        "checks": [asdict(c) for c in checks],
    }
    payload, rec = _call(
        model,
        model_name,
        (
            JUDGE_PROMPT
            + "\n\n"
            + root_cause_guidance()
            + "\n\nREFERENCE TAXONOMY:\n"
            + _json(taxonomy_data())
            + "\n\nINPUT:\n"
            + _json(judge_context)
            + f"\n\nPROMPT_SOURCE:{PROMPT_SOURCE};VERSION:{JUDGE_PROMPT_VERSION}"
        ),
        "root_cause_judge",
        ir.trajectory_id,
    )
    receipts.append(rec)
    root = payload or {
        reason_for_failure: judge unavailable,
        failure_case: 10,
        failure_step: None,
        confidence: 0.0,
        evidence_ref_ids: [],
    }
    if not isinstance(root, dict):
        errors.append(JUDGE_RESPONSE_MUST_BE_OBJECT)
        root = {reason_for_failure: invalid judge response, failure_case: 10, failure_step: None, confidence: 0.0, evidence_ref_ids: []}
    try:
        case = int(root.get(failure_case, 10))
    except (TypeError, ValueError):
        case = 10
        errors.append(JUDGE_FAILURE_CASE_INVALID)
    if case < 1 or case > 10:
        case = 10
        errors.append(JUDGE_FAILURE_CASE_OUT_OF_RANGE)
    root[failure_case] = case
    if not isinstance(root.get(evidence_ref_ids, []), list) or any(
        x not in refs for x in root.get(evidence_ref_ids, [])
    ):
        errors.append(JUDGE_UNKNOWN_EVIDENCE)
        root[evidence_ref_ids] = []
    statuses = {s.value: sum(1 for c in checks if c.status == s.value) for s in CheckStatus}
    coverage = {
        "invariant_count": len(all_inv),
        "checked_count": len(checks),
        "status_counts": statuses,
        "event_count": len(ir.events),
        "event_coverage": len({e.event_id for e in ir.events if e.evidence_ref_ids})
        / len(ir.events),
    }
    return AgentRxReport(
        SCHEMA_VERSION,
        ir.trajectory_id,
        static,
        tuple(dynamic_groups),
        checks,
        root,
        coverage,
        tuple(receipts),
        tuple(errors),
    )


__all__ = [
    "DYNAMIC_PROMPT",
    "JUDGE_PROMPT",
    "STATIC_PROMPT",
    "AgentRxReport",
    "TrajectoryIR",
    "normalize_trajectory",
    "run_agentrx_diagnosis",
]
