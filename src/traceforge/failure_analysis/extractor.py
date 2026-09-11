"""从 M1B 事实抽取 AgentRx 风格的确定性证据。"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    EVIDENCE_REF_SCHEMA,
    FAILURE_ANALYSIS_REPORT_SCHEMA,
    INVARIANT_CHECK_SCHEMA,
    EvidenceKind,
    EvidenceRefV1,
    EvidenceRole,
    FailureAnalysisReportV1,
    FailureCategory,
    FailureLayer,
    InvariantCheckV1,
    InvariantKind,
    InvariantResult,
    Recoverability,
    evidence_ref_id,
    failure_analysis_report_id,
)
from .reader import CaptureFact, EventFact, FailureInputView, PairingFact


@dataclass(frozen=True, slots=True)
class AnalysisBundle:
    """一次 M4 确定性分析产生的全部 artifact 记录。"""

    reports: tuple[FailureAnalysisReportV1, ...]
    evidence_refs: tuple[EvidenceRefV1, ...]
    invariant_checks: tuple[InvariantCheckV1, ...]


def _evidence_ref(
    run_id: str,
    kind: EvidenceKind,
    source_id: str,
    pointer: str,
    role: EvidenceRole,
) -> EvidenceRefV1:
    return EvidenceRefV1(
        schema_version=EVIDENCE_REF_SCHEMA,
        evidence_id=evidence_ref_id(
            m4_run_id=run_id,
            evidence_kind=kind,
            source_id=source_id,
            source_pointer=pointer,
        ),
        evidence_kind=kind.value,
        source_id=source_id,
        source_pointer=pointer,
        role=role.value,
        content_sha256=None,
    )


def _invariant_check(
    run_id: str,
    code: str,
    kind: InvariantKind,
    result: InvariantResult,
    trigger_event_id: str | None,
    evidence_ref_ids: tuple[str, ...],
    taxonomy_targets: tuple[str, ...],
) -> InvariantCheckV1:
    invariant_id = evidence_ref_id(
        m4_run_id=run_id,
        evidence_kind=EvidenceKind.ATTEMPT,
        source_id=code,
        source_pointer=trigger_event_id,
    )
    return InvariantCheckV1(
        schema_version=INVARIANT_CHECK_SCHEMA,
        invariant_id=invariant_id,
        kind=kind.value,
        check_code=code,
        result=result.value,
        trigger_event_id=trigger_event_id,
        evidence_ref_ids=evidence_ref_ids,
        taxonomy_targets=taxonomy_targets,
        error_code=None,
    )


def _event_ids_for_refs(
    refs: tuple[EvidenceRefV1, ...], evidence_ref_ids: tuple[str, ...]
) -> tuple[str, ...]:
    ref_ids = set(evidence_ref_ids)
    return tuple(
        ref.source_id
        for ref in refs
        if ref.evidence_id in ref_ids and ref.evidence_kind == EvidenceKind.EVENT.value
    )


def analyze_capture(
    *,
    run_id: str,
    m1b_run_id: str,
    capture: CaptureFact,
    events: tuple[EventFact, ...],
    pairings: tuple[PairingFact, ...],
) -> tuple[
    FailureAnalysisReportV1,
    tuple[EvidenceRefV1, ...],
    tuple[InvariantCheckV1, ...],
]:
    """对单个 capture 运行不依赖模型的结构检查。"""

    del m1b_run_id  # 保留参数以兼容调用方；身份已绑定在上游 run 中。

    refs: list[EvidenceRefV1] = [
        _evidence_ref(
            run_id,
            EvidenceKind.TERMINAL,
            capture.capture_id,
            "/terminal_status",
            EvidenceRole.OUTCOME,
        )
    ]
    refs.extend(
        _evidence_ref(
            run_id,
            EvidenceKind.EVENT,
            event.event_id,
            "/event_occurrence",
            EvidenceRole.CONTEXT,
        )
        for event in events
    )
    ref_ids = tuple(ref.evidence_id for ref in refs)

    checks: list[InvariantCheckV1] = []
    sequence_numbers = [event.sequence_number for event in events]
    stream_is_valid = sequence_numbers == sorted(sequence_numbers) and len(
        {event.event_id for event in events}
    ) == len(events)
    checks.append(
        _invariant_check(
            run_id,
            "event_stream.monotonic_unique",
            InvariantKind.DYNAMIC,
            InvariantResult.PASS if stream_is_valid else InvariantResult.FAIL,
            events[-1].event_id if events else None,
            ref_ids,
            (FailureCategory.INCONCLUSIVE.value,),
        )
    )

    for pairing in pairings:
        statuses = set(pairing.statuses)
        pairing_ref_ids = tuple(
            ref.evidence_id
            for ref in refs
            if ref.source_id in set(pairing.call_event_ids + pairing.result_event_ids)
        )
        if statuses == {"MATCHED_ONE_TO_ONE"}:
            result = InvariantResult.PASS
            targets: tuple[str, ...] = ()
        elif "INVALID_CALL_ARGUMENTS" in statuses:
            result = InvariantResult.FAIL
            targets = (FailureCategory.INVALID_TOOL_INVOCATION.value,)
        else:
            # 观察缺口不能证明 Agent 错误地解释了工具结果。
            result = InvariantResult.UNCLEAR
            targets = (FailureCategory.INCONCLUSIVE.value,)
        checks.append(
            _invariant_check(
                run_id,
                "tool_pairing.strict_one_to_one",
                InvariantKind.STATIC,
                result,
                pairing.call_event_ids[0] if pairing.call_event_ids else None,
                pairing_ref_ids,
                targets,
            )
        )

    if capture.terminal_status in {"TOOL_CALL_PENDING", "EMPTY_OUTCOME"}:
        terminal_result = InvariantResult.UNCLEAR
        terminal_targets = (FailureCategory.INCONCLUSIVE.value,)
    elif capture.terminal_status == "INVALID":
        terminal_result = InvariantResult.FAIL
        terminal_targets = (FailureCategory.SYSTEM_FAILURE.value,)
    else:
        terminal_result = InvariantResult.PASS
        terminal_targets = ()
    checks.append(
        _invariant_check(
            run_id,
            "capture.terminal_observed",
            InvariantKind.DYNAMIC,
            terminal_result,
            None,
            (refs[0].evidence_id,),
            terminal_targets,
        )
    )

    failed_checks = [
        check for check in checks if check.result == InvariantResult.FAIL.value
    ]
    unclear_checks = [
        check for check in checks if check.result == InvariantResult.UNCLEAR.value
    ]
    uncertainty_codes = tuple(check.check_code for check in unclear_checks)
    if failed_checks:
        primary_failure = next(
            (
                target
                for check in failed_checks
                for target in check.taxonomy_targets
            ),
            FailureCategory.INCONCLUSIVE.value,
        )
        recoverability = Recoverability.UNKNOWN
        confidence = 0.5
    else:
        primary_failure = FailureCategory.INCONCLUSIVE.value
        recoverability = Recoverability.UNKNOWN
        confidence = 0.0
        if not unclear_checks:
            uncertainty_codes = ("no_deterministic_failure_signal",)

    critical_event_ids = tuple(
        dict.fromkeys(
            event_id
            for check in failed_checks
            for event_id in _event_ids_for_refs(refs, check.evidence_ref_ids)
        )
    )
    report = FailureAnalysisReportV1(
        schema_version=FAILURE_ANALYSIS_REPORT_SCHEMA,
        report_id=failure_analysis_report_id(
            m4_run_id=run_id,
            task_episode_id=capture.capture_id,
            target_attempt_id=capture.capture_id,
        ),
        session_ref=f"capture:{capture.capture_id}",
        episode_ref=capture.capture_id,
        attempt_ref=capture.capture_id,
        capture_occurrence_id=capture.capture_id,
        primary_failure=primary_failure,
        failure_layer=FailureLayer.UNCLEAR.value,
        critical_event_ids=critical_event_ids,
        critical_step=None,
        evidence_ref_ids=ref_ids,
        violated_invariant_ids=tuple(check.invariant_id for check in failed_checks),
        causal_hypotheses=(),
        recoverability=recoverability.value,
        attribution={"UNKNOWN": 1.0},
        reconstruction_targets=(),
        reconstruction_relevance={
            "task_recovery": "NOT_RUN",
            "environment_recovery": "NOT_RUN",
        },
        open_questions=uncertainty_codes,
        confidence=confidence,
        uncertainty_codes=uncertainty_codes,
    )
    return report, tuple(refs), tuple(checks)


def extract_failure_bundle(view: FailureInputView, run_id: str) -> AnalysisBundle:
    """对所有 capture 进行确定性分析并按稳定 ID 排序。"""

    reports: list[FailureAnalysisReportV1] = []
    evidence_refs: list[EvidenceRefV1] = []
    invariant_checks: list[InvariantCheckV1] = []
    for capture in view.captures:
        report, refs, checks = analyze_capture(
            run_id=run_id,
            m1b_run_id=view.m1b_run_id,
            capture=capture,
            events=view.events_by_capture.get(capture.capture_id, ()),
            pairings=view.pairings_by_capture.get(capture.capture_id, ()),
        )
        reports.append(report)
        evidence_refs.extend(refs)
        invariant_checks.extend(checks)

    return AnalysisBundle(
        reports=tuple(reports),
        evidence_refs=tuple(sorted(evidence_refs, key=lambda item: item.evidence_id)),
        invariant_checks=tuple(
            sorted(invariant_checks, key=lambda item: item.invariant_id)
        ),
    )


__all__ = ["AnalysisBundle", "analyze_capture", "extract_failure_bundle"]
