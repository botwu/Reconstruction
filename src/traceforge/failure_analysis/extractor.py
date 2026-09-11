"""Deterministic invariant extraction for M4."""
from __future__ import annotations
from dataclasses import dataclass
from .contracts import (
    EvidenceKind, EvidenceRefV1, EvidenceRole, FailureAnalysisReportV1,
    FailureCategory, FailureLayer, InvariantCheckV1, InvariantKind,
    InvariantResult, Recoverability, EVIDENCE_REF_SCHEMA,
    INVARIANT_CHECK_SCHEMA, FAILURE_ANALYSIS_REPORT_SCHEMA,
    analysis_run_id, evidence_ref_id, failure_analysis_report_id,
)
from .reader import CaptureFact, EventFact, FailureInputView, PairingFact

@dataclass(frozen=True, slots=True)
class AnalysisBundle:
    reports: tuple[FailureAnalysisReportV1, ...]
    evidence_refs: tuple[EvidenceRefV1, ...]
    invariant_checks: tuple[InvariantCheckV1, ...]

def _eref(run_id: str, kind: EvidenceKind, source_id: str, pointer: str, role: EvidenceRole) -> EvidenceRefV1:
    return EvidenceRefV1(
        schema_version=EVIDENCE_REF_SCHEMA,
        evidence_id=evidence_ref_id(m4_run_id=run_id, evidence_kind=kind, source_id=source_id, source_pointer=pointer),
        evidence_kind=kind.value, source_id=source_id, source_pointer=pointer,
        role=role.value, content_sha256=None,
    )

def _check(run_id: str, code: str, kind: InvariantKind, result: InvariantResult,
           trigger: str | None, refs: tuple[str, ...], targets: tuple[str, ...]) -> InvariantCheckV1:
    return InvariantCheckV1(
        schema_version=INVARIANT_CHECK_SCHEMA,
        invariant_id=evidence_ref_id(m4_run_id=run_id, evidence_kind=EvidenceKind.ATTEMPT, source_id=code, source_pointer=trigger),
        kind=kind.value, check_code=code, result=result.value, trigger_event_id=trigger,
        evidence_ref_ids=refs, taxonomy_targets=targets, error_code=None,
    )

def analyze_capture(*, run_id: str, m1b_run_id: str, capture: CaptureFact,
                    events: tuple[EventFact, ...], pairings: tuple[PairingFact, ...]) -> tuple[FailureAnalysisReportV1, tuple[EvidenceRefV1, ...], tuple[InvariantCheckV1, ...]]:
    refs = [_eref(run_id, EvidenceKind.TERMINAL, capture.capture_id, "/terminal_status", EvidenceRole.OUTCOME)]
    for event in events:
        refs.append(_eref(run_id, EvidenceKind.EVENT, event.event_id, "/event_occurrence", EvidenceRole.CONTEXT))
    checks: list[InvariantCheckV1] = []
    checks.append(_check(run_id, "event_stream.monotonic_unique", InvariantKind.DYNAMIC,
                         InvariantResult.PASS if [e.sequence_number for e in events] == sorted(e.sequence_number for e in events) and len({e.event_id for e in events}) == len(events) else InvariantResult.FAIL,
                         events[-1].event_id if events else None,
                         tuple(ref.evidence_id for ref in refs), (FailureCategory.INCONCLUSIVE.value,)))
    for pairing in pairings:
        statuses = set(pairing.statuses)
        pids = tuple(ref.evidence_id for ref in refs if ref.source_id in set(pairing.call_event_ids + pairing.result_event_ids))
        if "MATCHED_ONE_TO_ONE" in statuses and len(statuses) == 1:
            result, target = InvariantResult.PASS, ()
        elif "INVALID_CALL_ARGUMENTS" in statuses:
            result, target = InvariantResult.FAIL, (FailureCategory.INVALID_TOOL_INVOCATION.value,)
        elif statuses:
            result, target = InvariantResult.FAIL, (FailureCategory.TOOL_OUTPUT_MISINTERPRETATION.value,)
        else:
            result, target = InvariantResult.UNCLEAR, (FailureCategory.INCONCLUSIVE.value,)
        checks.append(_check(run_id, "tool_pairing.strict_one_to_one", InvariantKind.STATIC, result,
                             pairing.call_event_ids[0] if pairing.call_event_ids else None, pids, target))
    if capture.terminal_status == "TOOL_CALL_PENDING":
        terminal_result, terminal_target = InvariantResult.FAIL, (FailureCategory.VERIFIER_OR_TERMINATION_FAILURE.value,)
    elif capture.terminal_status == "EMPTY_OUTCOME":
        terminal_result, terminal_target = InvariantResult.UNCLEAR, (FailureCategory.INCONCLUSIVE.value,)
    elif capture.terminal_status == "INVALID":
        terminal_result, terminal_target = InvariantResult.FAIL, (FailureCategory.SYSTEM_FAILURE.value,)
    else:
        terminal_result, terminal_target = InvariantResult.PASS, ()
    terminal_ref = refs[0].evidence_id
    checks.append(_check(run_id, "capture.terminal_observed", InvariantKind.DYNAMIC, terminal_result,
                         None, (terminal_ref,), terminal_target))
    failed = [c for c in checks if c.result == InvariantResult.FAIL.value]
    unclear = [c for c in checks if c.result == InvariantResult.UNCLEAR.value]
    if failed:
        primary = failed[0].taxonomy_targets[0] if failed[0].taxonomy_targets else FailureCategory.INCONCLUSIVE.value
        recoverability = Recoverability.UNKNOWN
        confidence = 0.5
    elif unclear:
        primary, recoverability, confidence = FailureCategory.INCONCLUSIVE.value, Recoverability.UNKNOWN, 0.0
    else:
        primary, recoverability, confidence = FailureCategory.INCONCLUSIVE.value, Recoverability.NOT_RECOVERABLE, 1.0
    critical = tuple(sorted({e for c in failed for r in c.evidence_ref_ids for e in [next((x.source_id for x in refs if x.evidence_id == r and x.evidence_kind == EvidenceKind.EVENT.value), "")] if e}))
    report = FailureAnalysisReportV1(
        schema_version=FAILURE_ANALYSIS_REPORT_SCHEMA,
        report_id=failure_analysis_report_id(m4_run_id=run_id, task_episode_id=capture.capture_id, target_attempt_id=capture.capture_id),
        session_ref=f"capture:{capture.capture_id}", episode_ref=f"capture:{capture.capture_id}", attempt_ref=f"capture:{capture.capture_id}",
        capture_occurrence_id=capture.capture_id, primary_failure=primary, failure_layer=FailureLayer.UNCLEAR.value,
        critical_event_ids=critical, critical_step=None,
        evidence_ref_ids=tuple(r.evidence_id for r in refs),
        violated_invariant_ids=tuple(c.invariant_id for c in failed),
        causal_hypotheses=(), recoverability=recoverability.value,
        attribution={"UNKNOWN": 1.0}, reconstruction_targets=(),
        reconstruction_relevance={"task_recovery": "NOT_RUN", "environment_recovery": "NOT_RUN"},
        open_questions=tuple(c.check_code for c in unclear), confidence=confidence,
        uncertainty_codes=tuple(c.check_code for c in unclear),
    )
    return report, tuple(refs), tuple(checks)

def extract_failure_bundle(view: FailureInputView, run_id: str) -> AnalysisBundle:
    reports, refs, checks = [], [], []
    for capture in view.captures:
        report, capture_refs, capture_checks = analyze_capture(
            run_id=run_id, m1b_run_id=view.m1b_run_id, capture=capture,
            events=view.events_by_capture.get(capture.capture_id, ()),
            pairings=view.pairings_by_capture.get(capture.capture_id, ()),
        )
        reports.append(report); refs.extend(capture_refs); checks.extend(capture_checks)
    return AnalysisBundle(tuple(reports), tuple(sorted(refs, key=lambda x: x.evidence_id)), tuple(sorted(checks, key=lambda x: x.invariant_id)))
