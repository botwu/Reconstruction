"""Pure orchestration for the reconstruction DAG.

This layer intentionally does not call an LLM, Harbor or AGS.  It materializes
an auditable execution plan and a conservative candidate selection manifest,
while leaving model-backed stages explicitly pending.
"""

from __future__ import annotations

import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from traceforge import __version__
from traceforge.failure_analysis.pipeline import build_failure_analysis
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.trajectory.json_codec import stable_id
from traceforge.trajectory.provenance import collect_git_provenance

from .contracts import (
    EXECUTION_PLAN_SCHEMA,
    PIPELINE_MANIFEST_SCHEMA,
    PIPELINE_RUN_RECEIPT_SCHEMA,
    RECONSTRUCTION_CONTRACT_VERSION,
    SELECTION_MANIFEST_SCHEMA,
    CandidateDecision,
    NodeStatus,
    PipelineNodeV1,
    ReconstructionCandidateV1,
    pipeline_run_id,
)


class ReconstructionPipelineInputError(RuntimeError):
    """输入 run 或派生 M4 产物不满足编排要求。"""


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconstructionPipelineInputError(f"无法读取 JSON artifact：{path}") from exc
    if not isinstance(value, dict):
        raise ReconstructionPipelineInputError(f"JSON artifact 必须是对象：{path}")
    return value


def _reports(m4_dir: Path) -> list[dict[str, Any]]:
    path = m4_dir / "private/failure_analysis.jsonl"
    if not path.is_file():
        raise ReconstructionPipelineInputError(f"M4 缺少 failure_analysis：{path}")
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("record is not object")
                rows.append(row)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReconstructionPipelineInputError(f"M4 failure_analysis 非法：{path}") from exc
    return rows


def _candidate_decision(report: dict[str, Any]) -> tuple[CandidateDecision, str, tuple[str, ...]]:
    """Conservative, model-free gate used before semantic agents are enabled."""
    primary = str(report.get("primary_failure", "INCONCLUSIVE"))
    layer = str(report.get("failure_layer", "UNCLEAR"))
    confidence = float(report.get("confidence", 0.0) or 0.0)
    recoverability = str(report.get("recoverability", "UNKNOWN"))
    if primary == "INCONCLUSIVE":
        return CandidateDecision.DEFER, "DEFERRED", ("NO_DETERMINISTIC_FAILURE_SIGNAL",)
    if layer in {"SYSTEM", "USER"}:
        return CandidateDecision.REJECT, "REJECTED", (f"NON_RECONSTRUCTABLE_LAYER:{layer}",)
    if confidence >= 0.75 and recoverability in {"HIGH", "MEDIUM"}:
        return CandidateDecision.ELIGIBLE, "ELIGIBLE_CODE_FILE", ()
    return CandidateDecision.REVIEW, "NEEDS_MANUAL_REVIEW", ("SEMANTIC_ANALYSIS_PENDING",)


def _candidate(report: dict[str, Any], *, run_id: str) -> ReconstructionCandidateV1:
    decision, route, reasons = _candidate_decision(report)
    report_id = str(report.get("report_id", ""))
    candidate_id = stable_id(
        "reconstruction-candidate-v1", {"pipeline_run_id": run_id, "report_id": report_id}
    )

    def _tuple(name: str) -> tuple[str, ...]:
        value = report.get(name, ())
        return tuple(x for x in value if isinstance(x, str)) if isinstance(value, list) else ()

    return ReconstructionCandidateV1(
        schema_version="traceforge.reconstruction-candidate.v1",
        candidate_id=candidate_id,
        report_id=report_id,
        session_ref=str(report.get("session_ref", "")),
        episode_ref=str(report.get("episode_ref", "")),
        attempt_ref=str(report.get("attempt_ref", "")),
        decision=decision.value,
        route=route,
        primary_failure=str(report.get("primary_failure", "INCONCLUSIVE")),
        failure_layer=str(report.get("failure_layer", "UNCLEAR")),
        recoverability=str(report.get("recoverability", "UNKNOWN")),
        confidence=float(report.get("confidence", 0.0) or 0.0),
        evidence_ref_ids=_tuple("evidence_ref_ids"),
        reconstruction_targets=_tuple("reconstruction_targets"),
        blocking_reason_codes=tuple(reasons),
    )


def build_reconstruction_pipeline(
    *, m1b_run_dir: str | Path, output_root: str | Path, m1d_run_dir: str | Path | None = None
) -> Path:
    """Run M4 and publish a deterministic reconstruction execution plan.

    ``output_root`` receives ``m4/<m4-run>`` and ``pipeline/<pipeline-run>``;
    existing runs are never overwritten.  Downstream model/Harbor/AGS nodes are
    emitted as ``PENDING_MODEL`` with stable input references.
    """
    started_at = datetime.now(UTC)
    root = Path(output_root)
    m4_root = root / "m4"
    m4_dir = build_failure_analysis(
        m1b_run_dir=m1b_run_dir, m1d_run_dir=m1d_run_dir, output_root=m4_root
    )
    m4_manifest = _json_object(m4_dir / "artifact_manifest.json")
    m4_run_id = str(m4_manifest.get("failure_analysis_run_id", m4_dir.name))
    m1b_run_id = str(m4_manifest.get("m1b_run_id", ""))
    m1d_run_id = m4_manifest.get("m1d_run_id")
    m1b_manifest_sha256 = str(m4_manifest.get("m1b_artifact_manifest_sha256", ""))
    run_id = pipeline_run_id(
        m1b_run_id=m1b_run_id,
        m1b_manifest_sha256=m1b_manifest_sha256,
        m1d_run_id=m1d_run_id if isinstance(m1d_run_id, str) else None,
        m4_run_id=m4_run_id,
    )
    candidates = tuple(_candidate(row, run_id=run_id) for row in _reports(m4_dir))
    m4_rel = str(m4_dir.relative_to(root))
    nodes = (
        PipelineNodeV1(
            "traceforge.reconstruction-node.v1",
            "m1b",
            "M1B_TRAJECTORY",
            NodeStatus.COMPLETED.value,
            (),
            ("m1b_run",),
            (),
        ),
        PipelineNodeV1(
            "traceforge.reconstruction-node.v1",
            "m1d",
            "M1D_TASK_EPISODE",
            NodeStatus.COMPLETED.value if m1d_run_id else NodeStatus.BLOCKED.value,
            ("m1b_run",),
            ("m1d_run",) if m1d_run_id else (),
            () if m1d_run_id else ("M1D_NOT_PROVIDED",),
        ),
        PipelineNodeV1(
            "traceforge.reconstruction-node.v1",
            "m4",
            "FAILURE_ANALYSIS",
            NodeStatus.COMPLETED.value,
            ("m1b_run", "m1d_run") if m1d_run_id else ("m1b_run",),
            (f"{m4_rel}/artifact_manifest.json", f"{m4_rel}/private/failure_analysis.jsonl"),
            (),
        ),
        PipelineNodeV1(
            "traceforge.reconstruction-node.v1",
            "task-recovery",
            "TASK_RECOVERY",
            NodeStatus.PENDING_MODEL.value,
            (f"{m4_rel}/private/failure_analysis.jsonl",),
            ("pending/task_recovery.jsonl",),
            ("MODEL_AGENT_NOT_RUN",),
        ),
        PipelineNodeV1(
            "traceforge.reconstruction-node.v1",
            "environment-recovery",
            "ENVIRONMENT_RECOVERY",
            NodeStatus.PENDING_MODEL.value,
            (f"{m4_rel}/private/failure_analysis.jsonl",),
            ("pending/environment_recovery.jsonl",),
            ("MODEL_AGENT_NOT_RUN",),
        ),
        PipelineNodeV1(
            "traceforge.reconstruction-node.v1",
            "harbor-compile",
            "HARBOR_BUNDLE",
            NodeStatus.PENDING_MODEL.value,
            ("pending/task_recovery.jsonl", "pending/environment_recovery.jsonl"),
            ("pending/harbor_bundles/",),
            ("UPSTREAM_RECONSTRUCTION_PENDING",),
        ),
        PipelineNodeV1(
            "traceforge.reconstruction-node.v1",
            "hermes-rollout",
            "HERMES_ROLLOUT",
            NodeStatus.PENDING_MODEL.value,
            ("pending/harbor_bundles/",),
            ("pending/rollouts/",),
            ("HARBOR_BUNDLE_PENDING",),
        ),
        PipelineNodeV1(
            "traceforge.reconstruction-node.v1",
            "sft-curation",
            "VERIFIER_AND_SFT",
            NodeStatus.PENDING_MODEL.value,
            ("pending/rollouts/",),
            ("pending/sft_manifest.jsonl",),
            ("ROLLOUT_PENDING",),
        ),
    )
    workspace = ArtifactWorkspace(root / "pipeline", run_id)
    entries = []
    try:
        selection = {
            "schema_version": SELECTION_MANIFEST_SCHEMA,
            "pipeline_run_id": run_id,
            "m4_run_id": m4_run_id,
            "candidate_count": len(candidates),
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
        entries.append(
            write_json_artifact(workspace.staging_path, "selection_manifest.json", selection)
        )
        plan = {
            "schema_version": EXECUTION_PLAN_SCHEMA,
            "pipeline_run_id": run_id,
            "contract_version": RECONSTRUCTION_CONTRACT_VERSION,
            "nodes": [node.to_dict() for node in nodes],
            "model_calls": 0,
            "external_execution": False,
        }
        entries.append(write_json_artifact(workspace.staging_path, "execution_plan.json", plan))
        manifest = {
            "schema_version": PIPELINE_MANIFEST_SCHEMA,
            "pipeline_run_id": run_id,
            "contract_version": RECONSTRUCTION_CONTRACT_VERSION,
            "m1b_run_id": m1b_run_id,
            "m1d_run_id": m1d_run_id,
            "m4_run_id": m4_run_id,
            "m4_artifact_root": m4_rel,
            "files": artifact_entry_dicts(entries),
        }
        manifest_entry = write_json_artifact(
            workspace.staging_path, "artifact_manifest.json", manifest
        )
        entries.append(manifest_entry)
        completed_at = datetime.now(UTC)
        write_json_artifact(
            workspace.staging_path,
            "run_receipt.json",
            {
                "schema_version": PIPELINE_RUN_RECEIPT_SCHEMA,
                "run_id": run_id,
                "artifact_manifest_sha256": manifest_entry.sha256,
                "completed_at": completed_at.isoformat(),
                "duration_seconds": (completed_at - started_at).total_seconds(),
                "git_provenance": collect_git_provenance().to_dict(),
                "python": platform.python_version(),
                "traceforge_version": __version__,
            },
        )
        return workspace.publish()
    except BaseException:
        workspace.abort()
        raise
