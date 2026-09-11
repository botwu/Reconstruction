"""构造第一批人工复核样本。

选择只消费结构化 Failure Analysis 结果，不复制原始轨迹正文。排序规则固定、
可解释并写入 artifact，便于人工标注后作为 golden set。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.trajectory.json_codec import canonical_json_bytes, stable_id

REVIEW_BATCH_SCHEMA = "traceforge.manual-review-batch.v1"


class ReviewBatchInputError(ValueError):
    """人工复核批次输入不满足契约。"""


def _read_rows(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReviewBatchInputError(f"无法读取输入：{path}") from exc
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewBatchInputError(f"第 {line_no} 行不是 JSON") from exc
        if not isinstance(value, dict):
            raise ReviewBatchInputError(f"第 {line_no} 行根节点必须是对象")
        rows.append(value)
    return rows


def _score(row: dict[str, Any]) -> tuple[int, int, int, float, str]:
    raw_decision = row.get("decision")
    raw_outcome = row.get("outcome")
    decision = (
        str(raw_decision)
        if isinstance(raw_decision, str)
        else (
            "REVIEW"
            if str(row.get("primary_failure", "INCONCLUSIVE")) != "INCONCLUSIVE"
            else "DEFER"
        )
    )
    outcome = (
        str(raw_outcome)
        if isinstance(raw_outcome, str)
        else (
            "FAILURE"
            if str(row.get("primary_failure", "INCONCLUSIVE")) != "INCONCLUSIVE"
            else "UNCERTAIN"
        )
    )
    recoverability = str(row.get("recoverability", ""))
    decision_rank = {"ELIGIBLE": 4, "REVIEW": 3, "DEFER": 2, "REJECT": 0}.get(decision, 1)
    outcome_rank = {"FAILURE": 3, "INCOMPLETE": 3, "UNCERTAIN": 1, "SUCCESS": 0}.get(outcome, 0)
    recoverability_rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(recoverability, 0)
    rubric = row.get("rubric", {})
    rubric_total = (
        sum(
            value
            for value in rubric.values()
            if isinstance(value, int) and not isinstance(value, bool)
        )
        if isinstance(rubric, dict)
        else 0
    )
    if rubric_total == 0:
        rubric_total = len(row.get("evidence_ref_ids", [])) + len(
            row.get("reconstruction_targets", [])
        )
    confidence = row.get("confidence", 0.0)
    confidence_value = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    stable = str(row.get("analysis_id") or row.get("report_id") or row.get("attempt_ref") or "")
    return decision_rank, outcome_rank, recoverability_rank, rubric_total + confidence_value, stable


def build_review_batch(
    *,
    input_jsonl: str | Path,
    output_root: str | Path,
    limit: int = 50,
    batch_name: str = "initial-manual-review",
) -> Path:
    """按固定排序生成待人工复核批次，默认最多 50 条。"""
    if limit < 1:
        raise ReviewBatchInputError("limit 必须大于 0")
    rows = _read_rows(Path(input_jsonl))
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("analysis_id") or row.get("report_id") or row.get("attempt_ref") or "")
        if not key:
            continue
        unique.setdefault(key, row)
    ranked = sorted(unique.values(), key=_score, reverse=True)
    selected = ranked[:limit]
    batch_id = stable_id(
        "traceforge.manual-review-batch.v1",
        {
            "name": batch_name,
            "limit": limit,
            "input_sha256": hashlib.sha256(canonical_json_bytes(rows)).hexdigest(),
            "selected": [str(row.get("analysis_id") or row.get("report_id")) for row in selected],
        },
    )
    workspace = ArtifactWorkspace(Path(output_root), batch_id)
    entries = []
    try:
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "review_batch.json",
                {
                    "schema_version": REVIEW_BATCH_SCHEMA,
                    "batch_id": batch_id,
                    "batch_name": batch_name,
                    "status": "PENDING_HUMAN_REVIEW",
                    "selection_policy": {
                        "order": "decision>outcome>recoverability>rubric_plus_confidence>stable_id",
                        "limit": limit,
                        "deduplication": "analysis_id/report_id/attempt_ref",
                    },
                    "items": [
                        {
                            "review_index": index,
                            "review_status": "PENDING",
                            "source": row,
                            "selection_score": list(_score(row)),
                        }
                        for index, row in enumerate(selected)
                    ],
                },
            )
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "metrics.json",
                {
                    "schema_version": "traceforge.manual-review-batch-metrics.v1",
                    "batch_id": batch_id,
                    "input_count": len(rows),
                    "deduplicated_count": len(unique),
                    "selected_count": len(selected),
                    "requested_limit": limit,
                    "decision_counts": {
                        decision: sum(
                            (
                                str(row.get("decision"))
                                if isinstance(row.get("decision"), str)
                                else "UNLABELED"
                            )
                            == decision
                            for row in selected
                        )
                        for decision in ("ELIGIBLE", "REVIEW", "DEFER", "REJECT", "UNLABELED")
                    },
                },
            )
        )
        manifest = write_json_artifact(
            workspace.staging_path,
            "artifact_manifest.json",
            {
                "schema_version": "traceforge.manual-review-batch-artifacts.v1",
                "batch_id": batch_id,
                "files": artifact_entry_dicts(entries),
            },
        )
        write_json_artifact(
            workspace.staging_path,
            "run_receipt.json",
            {
                "schema_version": "traceforge.manual-review-batch-receipt.v1",
                "batch_id": batch_id,
                "status": "PENDING_HUMAN_REVIEW",
                "artifact_manifest_sha256": manifest.sha256,
            },
        )
        return workspace.publish()
    except BaseException:
        workspace.abort()
        raise


__all__ = ["REVIEW_BATCH_SCHEMA", "ReviewBatchInputError", "build_review_batch"]

