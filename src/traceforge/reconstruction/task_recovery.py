"""Terminal-Universe Intent Recovery 策略的可审计 artifact runner。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.trajectory.json_codec import canonical_json_bytes, stable_id

from .model_gateway import ChatModel, ModelRequest, ModelResponse
from .semantic_recovery import recover, recovery_metrics

TASK_RECOVERY_RUN_SCHEMA = "traceforge.task-recovery-run.v1"
TASK_RECOVERY_PROMPT_VERSION = "terminal-universe-intent-recovery-c1-v1"


class _RecordingModel:
    def __init__(self, delegate: ChatModel) -> None:
        self.delegate = delegate
        self.request: ModelRequest | None = None
        self.response: ModelResponse | None = None

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.request = request
        self.response = self.delegate.complete(request)
        return self.response


def run_task_recovery(
    *,
    attempt_ref: str,
    source_report_id: str,
    report: dict[str, Any],
    evidence: list[dict[str, Any]],
    model: ChatModel,
    output_root: str | Path,
    model_name: str = "claude-opus-4-8",
) -> Path:
    """重述用户任务并发布公开结果、私有模型边界和指标。

    C.1 约束由 semantic_recovery prompt 执行：锚定用户显式请求，Agent 动作只可
    解释请求，不可注入新路径、值或实现策略。
    """

    run_id = stable_id(
        "traceforge.task-recovery-run.v1",
        {
            "attempt_ref": attempt_ref,
            "source_report_id": source_report_id,
            "model": model_name,
            "prompt_version": TASK_RECOVERY_PROMPT_VERSION,
            "report_sha256": hashlib.sha256(canonical_json_bytes(report)).hexdigest(),
            "evidence_sha256": hashlib.sha256(canonical_json_bytes(evidence)).hexdigest(),
        },
    )
    recording = _RecordingModel(model)
    outcome = recover(
        kind="task",
        attempt_ref=attempt_ref,
        source_report_id=source_report_id,
        report=report,
        evidence=evidence,
        model=recording,
        model_name=model_name,
    )
    workspace = ArtifactWorkspace(Path(output_root), run_id)
    entries = []
    try:
        entries.append(
            write_json_artifact(workspace.staging_path, "task_recovery.json", outcome.to_dict())
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "metrics.json",
                {
                    "schema_version": "traceforge.task-recovery-metrics.v1",
                    "run_id": run_id,
                    **recovery_metrics([outcome]),
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
                        "response_schema": request.response_schema if request else None,
                        "prompt_version": TASK_RECOVERY_PROMPT_VERSION,
                        "prompt_sha256": (
                            hashlib.sha256(request.prompt.encode()).hexdigest() if request else None
                        ),
                        "prompt": request.prompt if request else None,
                    },
                    "response": {
                        "text": response.text if response else None,
                        "sha256": response.content_sha256 if response else None,
                        "receipt": asdict(outcome.model_receipt) if outcome.model_receipt else None,
                    },
                    "credentials_embedded": False,
                },
            )
        )
        manifest = write_json_artifact(
            workspace.staging_path,
            "artifact_manifest.json",
            {
                "schema_version": TASK_RECOVERY_RUN_SCHEMA,
                "run_id": run_id,
                "status": outcome.status,
                "files": artifact_entry_dicts(entries),
            },
        )
        write_json_artifact(
            workspace.staging_path,
            "run_receipt.json",
            {
                "schema_version": "traceforge.task-recovery-receipt.v1",
                "run_id": run_id,
                "status": outcome.status,
                "artifact_manifest_sha256": manifest.sha256,
                "model_calls": 1 if recording.request else 0,
            },
        )
        return workspace.publish()
    except BaseException:
        workspace.abort()
        raise


__all__ = ["TASK_RECOVERY_RUN_SCHEMA", "run_task_recovery"]
