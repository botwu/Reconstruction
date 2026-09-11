"""Failure Analysis Agent 的可审计 artifact runner。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from traceforge.reconstruction.model_gateway import ChatModel, ModelRequest, ModelResponse
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.trajectory.json_codec import canonical_json_bytes, stable_id

from .model_analyzer import (
    MODEL_ANALYSIS_PROMPT_VERSION,
    FailureAnalysisModelResult,
    analyze_failure,
)

MODEL_ANALYSIS_RUN_SCHEMA = "traceforge.failure-analysis-model-run.v1"


class _RecordingModel:
    def __init__(self, delegate: ChatModel) -> None:
        self.delegate = delegate
        self.request: ModelRequest | None = None
        self.response: ModelResponse | None = None

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.request = request
        self.response = self.delegate.complete(request)
        return self.response


def run_failure_analysis_model(
    *,
    report: dict[str, Any],
    evidence: list[dict[str, Any]],
    model: ChatModel,
    output_root: str | Path,
    model_name: str = "claude-opus-4-8",
) -> Path:
    """运行 Failure Analysis Agent 并发布公开结果、私有交换和指标。"""
    if not isinstance(report, dict) or not isinstance(evidence, list):
        raise TypeError("report 必须是 object，evidence 必须是 array")
    recording = _RecordingModel(model)
    result: FailureAnalysisModelResult = analyze_failure(
        report=report,
        evidence=evidence,
        model=recording,
        model_name=model_name,
    )
    run_id = stable_id(
        "traceforge.failure-analysis-model-run-artifact.v1",
        {
            "analysis_id": result.analysis_id,
            "report_sha256": hashlib.sha256(canonical_json_bytes(report)).hexdigest(),
            "evidence_sha256": hashlib.sha256(canonical_json_bytes(evidence)).hexdigest(),
        },
    )
    workspace = ArtifactWorkspace(Path(output_root), run_id)
    entries = []
    try:
        entries.append(
            write_json_artifact(workspace.staging_path, "model_analysis.json", result.to_dict())
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "metrics.json",
                {
                    "schema_version": "traceforge.failure-analysis-model-metrics.v1",
                    "run_id": run_id,
                    "status": result.status,
                    "outcome": result.outcome,
                    "decision": result.decision,
                    "needs_reconstruction": result.needs_reconstruction,
                    "evidence_ref_count": len(result.evidence_ref_ids),
                    "critical_event_count": len(result.critical_event_ids),
                    "rubric_total": sum(result.rubric.values()),
                    "review_reason_count": len(result.review_reasons),
                },
            )
        )
        request = recording.request
        response = recording.response
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "private/model_exchange.json",
                {
                    "schema_version": "traceforge.private-model-exchange.v1",
                    "request": {
                        "request_id": request.request_id if request else None,
                        "model": request.model if request else model_name,
                        "response_schema": (
                            request.response_schema
                            if request
                            else MODEL_ANALYSIS_RUN_SCHEMA
                        ),
                        "prompt_version": MODEL_ANALYSIS_PROMPT_VERSION,
                        "prompt": request.prompt if request else None,
                        "prompt_sha256": (
                            hashlib.sha256(request.prompt.encode()).hexdigest()
                            if request else None
                        ),
                    },
                    "response": {
                        "text": response.text if response else None,
                        "sha256": response.content_sha256 if response else None,
                        "receipt": asdict(result.model_receipt) if result.model_receipt else None,
                    },
                    "credentials_embedded": False,
                },
            )
        )
        manifest = write_json_artifact(
            workspace.staging_path,
            "artifact_manifest.json",
            {
                "schema_version": MODEL_ANALYSIS_RUN_SCHEMA,
                "run_id": run_id,
                "status": result.status,
                "files": artifact_entry_dicts(entries),
            },
        )
        write_json_artifact(
            workspace.staging_path,
            "run_receipt.json",
            {
                "schema_version": "traceforge.failure-analysis-model-receipt.v1",
                "run_id": run_id,
                "status": result.status,
                "artifact_manifest_sha256": manifest.sha256,
                "model_calls": 1 if request else 0,
            },
        )
        return workspace.publish()
    except BaseException:
        workspace.abort()
        raise


__all__ = ["MODEL_ANALYSIS_RUN_SCHEMA", "run_failure_analysis_model"]

