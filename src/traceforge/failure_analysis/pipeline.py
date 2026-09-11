"""Deterministic M4 failure-analysis artifact pipeline."""
from __future__ import annotations
import platform
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from traceforge import __version__
from traceforge.trajectory.artifacts import ArtifactWorkspace, JsonlArtifactWriter, artifact_entry_dicts, write_json_artifact
from traceforge.trajectory.provenance import collect_git_provenance
from .contracts import FAILURE_ANALYSIS_CONTRACT_VERSION, FAILURE_ANALYSIS_SUMMARY_SCHEMA, analysis_run_id, failure_analysis_report_id
from .extractor import extract_failure_bundle
from .reader import load_failure_input
from .mapping_reader import load_mapping_index, MappingInputError

_JSONL_ARTIFACTS = {"failure_analysis": "private/failure_analysis.jsonl", "evidence_refs": "private/evidence_refs.jsonl", "invariant_checks": "private/invariant_checks.jsonl"}

def _close_writers(writers: dict[str, JsonlArtifactWriter]) -> list[Any]: return [writers[name].close() for name in sorted(writers)]
def _abort_writers(writers: dict[str, JsonlArtifactWriter]) -> list[BaseException]:
    errors=[]
    for writer in writers.values():
        try: writer.abort()
        except BaseException as exc: errors.append(exc)
    return errors

def _bind_reports(bundle: Any, *, run_id: str, mapping: Any | None) -> Any:
    """按 capture 绑定真实 M1D episode/attempt；无映射只留下显式 pending 标记。"""
    if mapping is None:
        reports = []
        for report in bundle.reports:
            # 未提供 M1D 时不构造伪 episode/attempt；下游必须显式处理空引用。
            reports.append(
                replace(
                    report,
                    report_id=failure_analysis_report_id(
                        m4_run_id=run_id,
                        task_episode_id=None,
                        target_attempt_id=None,
                    ),
                    episode_ref=None,
                    attempt_ref=None,
                    reconstruction_relevance={
                        **report.reconstruction_relevance,
                        "mapping_status": "MAPPING_PENDING",
                    },
                )
            )
        return replace(bundle, reports=tuple(reports))
    by_capture = {}
    for episode in mapping.episodes:
        by_capture.setdefault(episode.capture_occurrence_id, []).append(episode)
    attempts_by_episode = {}
    for attempt in mapping.attempts:
        attempts_by_episode.setdefault(attempt.episode_ref, []).append(attempt)
    reports=[]
    for report in bundle.reports:
        candidates=by_capture.get(report.capture_occurrence_id, ())
        if not candidates:
            # capture 未出现在 M1D 中时保持未绑定，不能猜测 episode。
            reports.append(report)
            continue
        episode=sorted(candidates, key=lambda e: e.turn_ordinal)[0]
        attempts=sorted(attempts_by_episode.get(episode.episode_ref, ()), key=lambda a: a.step_ordinal)
        attempt=attempts[0] if attempts else None
        episode_ref=episode.episode_ref
        attempt_ref=attempt.attempt_ref if attempt else None
        reports.append(replace(report, report_id=failure_analysis_report_id(m4_run_id=run_id, task_episode_id=episode_ref, target_attempt_id=attempt_ref), session_ref=episode.session_ref, episode_ref=episode_ref, attempt_ref=attempt_ref, reconstruction_relevance={**report.reconstruction_relevance, "mapping_status": "BOUND"}))
    return replace(bundle, reports=tuple(reports))

def build_failure_analysis(*, m1b_run_dir: str | Path, output_root: str | Path, m1d_run_dir: str | Path | None = None) -> Path:
    started_at=datetime.now(UTC); provenance=collect_git_provenance(); view=load_failure_input(m1b_run_dir)
    mapping=None; mapping_status="MAPPING_PENDING"
    if m1d_run_dir is not None:
        mapping=load_mapping_index(m1d_run_dir=m1d_run_dir, m1b_run_dir=m1b_run_dir); mapping_status="BOUND"
    run_id=analysis_run_id(m1b_run_id=view.m1b_run_id,m1b_manifest_sha256=view.m1b_manifest_sha256); bundle=_bind_reports(extract_failure_bundle(view,run_id),run_id=run_id,mapping=mapping)
    workspace=ArtifactWorkspace(Path(output_root),run_id); writers={}; entries=[]
    try:
        entries.append(write_json_artifact(workspace.staging_path,"failure_analysis_manifest.json",{"schema_version":"traceforge.failure-analysis-manifest.v1","failure_analysis_run_id":run_id,"failure_analysis_contract_version":FAILURE_ANALYSIS_CONTRACT_VERSION,"m1b_run_id":view.m1b_run_id,"m1b_artifact_manifest_sha256":view.m1b_manifest_sha256,"m1d_run_id":mapping.m1d_run_id if mapping else None,"m1d_artifact_manifest_sha256":mapping.m1d_artifact_manifest_sha256 if mapping else None,"mapping_status":mapping_status,"model_status":"NOT_RUN"}))
        writers=workspace.open_jsonl_writers(_JSONL_ARTIFACTS)
        for report in bundle.reports: writers["failure_analysis"].write(report.to_dict())
        for ref in bundle.evidence_refs: writers["evidence_refs"].write(ref.to_dict())
        for check in bundle.invariant_checks: writers["invariant_checks"].write(check.to_dict())
        entries.extend(_close_writers(writers)); counts={"report_count":len(bundle.reports),"failed_invariant_count":sum(c.result=="FAIL" for c in bundle.invariant_checks),"unclear_invariant_count":sum(c.result=="UNCLEAR" for c in bundle.invariant_checks)}
        entries.append(write_json_artifact(workspace.staging_path,"reports/failure_analysis_report.json",{"schema_version":FAILURE_ANALYSIS_SUMMARY_SCHEMA,"failure_analysis_run_id":run_id,"m1b_run_id":view.m1b_run_id,"m1d_run_id":mapping.m1d_run_id if mapping else None,"mapping_status":mapping_status,"counts":counts}))
        entries.append(write_json_artifact(workspace.staging_path,"artifact_manifest.json",{"schema_version":"traceforge.failure-analysis-artifact-manifest.v1","failure_analysis_run_id":run_id,"failure_analysis_contract_version":FAILURE_ANALYSIS_CONTRACT_VERSION,"m1b_run_id":view.m1b_run_id,"m1d_run_id":mapping.m1d_run_id if mapping else None,"mapping_status":mapping_status,"files":artifact_entry_dicts(entries)}))
        completed=datetime.now(UTC); write_json_artifact(workspace.staging_path,"run_receipt.json",{"schema_version":"traceforge.failure-analysis-run-receipt.v1","run_id":run_id,"artifact_manifest_sha256":entries[-1].sha256,"completed_at":completed.isoformat(),"duration_seconds":(completed-started_at).total_seconds(),"git_provenance":provenance.to_dict(),"git_provenance_verified_at_completion":False,"python":platform.python_version(),"traceforge_version":__version__})
        return workspace.publish()
    except BaseException as primary_error:
        cleanup_errors=_abort_writers(writers)
        try: workspace.abort()
        except BaseException as cleanup_error: cleanup_errors.append(cleanup_error)
        for cleanup_error in cleanup_errors: primary_error.add_note(f"cleanup failed: {cleanup_error!r}")
        raise
