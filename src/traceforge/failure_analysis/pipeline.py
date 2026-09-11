"""Deterministic M4 failure-analysis artifact pipeline."""
from __future__ import annotations
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from traceforge import __version__
from traceforge.trajectory.artifacts import ArtifactWorkspace, JsonlArtifactWriter, artifact_entry_dicts, write_json_artifact
from traceforge.trajectory.provenance import collect_git_provenance
from .contracts import (
    FAILURE_ANALYSIS_CONTRACT_VERSION, FAILURE_ANALYSIS_REPORT_SCHEMA, analysis_run_id,
)
from .extractor import extract_failure_bundle
from .reader import load_failure_input

_JSONL_ARTIFACTS = {
    "failure_analysis": "private/failure_analysis.jsonl",
    "evidence_refs": "private/evidence_refs.jsonl",
    "invariant_checks": "private/invariant_checks.jsonl",
}

def _close_writers(writers: dict[str, JsonlArtifactWriter]) -> list[Any]:
    return [writers[name].close() for name in sorted(writers)]

def _abort_writers(writers: dict[str, JsonlArtifactWriter]) -> list[BaseException]:
    errors: list[BaseException] = []
    for writer in writers.values():
        try: writer.abort()
        except BaseException as exc: errors.append(exc)
    return errors

def build_failure_analysis(*, m1b_run_dir: str | Path, output_root: str | Path) -> Path:
    started_at = datetime.now(UTC)
    provenance = collect_git_provenance()
    view = load_failure_input(m1b_run_dir)
    run_id = analysis_run_id(m1b_run_id=view.m1b_run_id, m1b_manifest_sha256=view.m1b_manifest_sha256)
    bundle = extract_failure_bundle(view, run_id)
    workspace = ArtifactWorkspace(Path(output_root), run_id)
    writers: dict[str, JsonlArtifactWriter] = {}
    entries = []
    try:
        entries.append(write_json_artifact(workspace.staging_path, "failure_analysis_manifest.json", {
            "schema_version": "traceforge.failure-analysis-manifest.v1",
            "failure_analysis_run_id": run_id,
            "failure_analysis_contract_version": FAILURE_ANALYSIS_CONTRACT_VERSION,
            "m1b_run_id": view.m1b_run_id,
            "m1b_artifact_manifest_sha256": view.m1b_manifest_sha256,
            "source_schema": view.source_schema,
            "model_status": "NOT_RUN",
        }))
        writers = workspace.open_jsonl_writers(_JSONL_ARTIFACTS)
        for report in bundle.reports: writers["failure_analysis"].write(report.to_dict())
        for ref in bundle.evidence_refs: writers["evidence_refs"].write(ref.to_dict())
        for check in bundle.invariant_checks: writers["invariant_checks"].write(check.to_dict())
        entries.extend(_close_writers(writers))
        route_counts = {
            "report_count": len(bundle.reports),
            "failed_invariant_count": sum(c.result == "FAIL" for c in bundle.invariant_checks),
            "unclear_invariant_count": sum(c.result == "UNCLEAR" for c in bundle.invariant_checks),
        }
        entries.append(write_json_artifact(workspace.staging_path, "reports/failure_analysis_report.json", {
            "schema_version": FAILURE_ANALYSIS_REPORT_SCHEMA,
            "failure_analysis_run_id": run_id,
            "m1b_run_id": view.m1b_run_id,
            "counts": route_counts,
        }))
        entries.append(write_json_artifact(workspace.staging_path, "artifact_manifest.json", {
            "schema_version": "traceforge.failure-analysis-artifact-manifest.v1",
            "failure_analysis_run_id": run_id,
            "failure_analysis_contract_version": FAILURE_ANALYSIS_CONTRACT_VERSION,
            "m1b_run_id": view.m1b_run_id,
            "m1b_artifact_manifest_sha256": view.m1b_manifest_sha256,
            "files": artifact_entry_dicts(entries),
        }))
        completed = datetime.now(UTC)
        write_json_artifact(workspace.staging_path, "run_receipt.json", {
            "schema_version": "traceforge.failure-analysis-run-receipt.v1",
            "run_id": run_id,
            "artifact_manifest_sha256": entries[-1].sha256,
            "completed_at": completed.isoformat(),
            "duration_seconds": (completed - started_at).total_seconds(),
            "git_provenance": provenance.to_dict(),
            "git_provenance_verified_at_completion": False,
            "python": platform.python_version(),
            "traceforge_version": __version__,
        })
        return workspace.publish()
    except BaseException as primary_error:
        cleanup_errors = _abort_writers(writers)
        try: workspace.abort()
        except BaseException as cleanup_error: cleanup_errors.append(cleanup_error)
        for cleanup_error in cleanup_errors: primary_error.add_note(f"cleanup failed: {cleanup_error!r}")
        raise
