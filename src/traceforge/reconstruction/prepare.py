"""从已发布 M1B run 确定性地准备重建闭环输入（report.json + 带正文 evidence.json）。

本模块不调用模型：它把 M4 确定性失败分析（build_failure_analysis）与轨迹证据投影
（evidence_join.build_session_input）拼装成 `reconstruct workflow` 所需的两个输入文件，
并把「事实完整性」质量信号（未观测工具调用、缺失证据、输入截断）如实透传给下游门禁。

设计要点：
- report.status/quality/selection/provenance 只反映证据是否完整可用，不把确定性失败
  归因当作重建门禁——已配对且被观测的完整工具轨迹应能自动 COMPLETE。
- 按 candidate_group_id 聚合完整 session；capture 与 request boundary 仅作为 provenance 元数据，
  不改变事件集合。工具配对继续透传，以便保留 pending 标记。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceforge.failure_analysis.pipeline import build_failure_analysis
from traceforge.trajectory.evidence_join import build_session_input

PREPARE_REPORT_SCHEMA = "traceforge.reconstruction-source-report.v1"
PREPARE_MANIFEST_SCHEMA = "traceforge.reconstruction-prepare-manifest.v1"


class ReconstructionPrepareError(RuntimeError):
    """无法从 M1B run 准备重建输入。"""


@dataclass(frozen=True, slots=True)
class PreparedInputs:
    capture_id: str
    attempt_ref: str
    source_report_id: str
    report_path: Path
    evidence_path: Path
    manifest_path: Path
    requires_review: bool


def _load_reports(fa_run_dir: Path) -> list[dict[str, Any]]:
    path = fa_run_dir / "private" / "failure_analysis.jsonl"
    if not path.is_file():
        raise ReconstructionPrepareError(f"失败分析产物缺少 {path}")
    reports: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                reports.append(value)
    return reports


def _select_report(reports: list[dict[str, Any]], capture_id: str | None) -> dict[str, Any]:
    available = ", ".join(sorted(str(r.get("capture_occurrence_id")) for r in reports))
    if capture_id is not None:
        for report in reports:
            if report.get("capture_occurrence_id") == capture_id:
                return report
        raise ReconstructionPrepareError(
            f"M1B run 中找不到 capture_id={capture_id}；可选：{available}"
        )
    if len(reports) != 1:
        raise ReconstructionPrepareError(
            f"M1B run 含多个 capture，必须显式指定 --capture-id；可选：{available}"
        )
    return reports[0]


def build_reconstruction_inputs(
    *,
    m1b_run_dir: str | Path,
    output_dir: str | Path,
    capture_id: str | None = None,
    m1d_run_dir: str | Path | None = None,
) -> PreparedInputs:
    """在一个已发布 M1B run 上产出 report.json + evidence.json，供 workflow 直接消费。"""
    m1b = Path(m1b_run_dir)
    if not m1b.is_dir():
        raise ReconstructionPrepareError(f"M1B run 不是目录：{m1b}")
    event_occurrences = m1b / "private" / "event_occurrences.jsonl"
    tool_pairings = m1b / "private" / "tool_pairings.jsonl"
    if not event_occurrences.is_file():
        raise ReconstructionPrepareError(f"M1B run 缺少 {event_occurrences}")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    fa_run_dir = Path(
        build_failure_analysis(
            m1b_run_dir=m1b,
            output_root=output / "failure_analysis",
            m1d_run_dir=m1d_run_dir,
        )
    )
    reports = _load_reports(fa_run_dir)
    if not reports:
        raise ReconstructionPrepareError("失败分析未产出任何报告")
    fa_report = _select_report(reports, capture_id)
    resolved_capture_id = str(fa_report.get("capture_occurrence_id"))

    captures = m1b / "private" / "captures.jsonl"
    boundaries = m1b / "private" / "request_boundaries.jsonl"
    task_input = build_session_input(
        event_occurrences_path=event_occurrences,
        capture_id=resolved_capture_id,
        output_path=output / "task_input.json",
        captures_path=captures if captures.is_file() else None,
        request_boundaries_path=boundaries if boundaries.is_file() else None,
        tool_pairings_path=tool_pairings if tool_pairings.is_file() else None,
    )
    evidence = task_input.get("evidence", [])
    selection = task_input.get("selection", {})
    quality = task_input.get("quality", {})
    requires_review = bool(quality.get("requires_review"))
    pending = int(selection.get("pending_tool_call_count", 0) or 0)
    truncated = bool(selection.get("truncated"))

    evidence_path = output / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    attempt_ref = str(fa_report.get("attempt_ref") or resolved_capture_id)
    source_report_id = str(fa_report.get("report_id") or resolved_capture_id)

    report = {
        "schema_version": PREPARE_REPORT_SCHEMA,
        "capture_id": resolved_capture_id,
        "attempt_ref": attempt_ref,
        "source_report_id": source_report_id,
        # status 仅反映「事实完整性」是否需人审；确定性失败归因是模型上下文而非门禁。
        "status": "REVIEW" if requires_review else "READY",
        "failure_analysis": fa_report,
        "quality": {
            "requires_review": requires_review,
            "reason_codes": list(quality.get("reason_codes", [])),
        },
        "selection": {
            "pending_tool_call_count": pending,
            "pending_tool_call_ids": list(selection.get("pending_tool_call_ids", [])),
            "attempt_action_count": selection.get("attempt_action_count", 0),
            "attempt_observation_count": selection.get("attempt_observation_count", 0),
            "user_query_count": selection.get("user_query_count", 0),
            "session_capture_count": selection.get("session_capture_count", 1),
            "raw_session_event_count": selection.get(
                "raw_session_event_count", selection.get("source_row_count", 0)
            ),
            "deduplicated_session_event_count": selection.get(
                "deduplicated_session_event_count", len(evidence)
            ),
            "query_ordinal": None,
        },
        "provenance": {
            # snapshot 表示「输入被截断、仅部分快照」；完整轨迹为 False，不额外触发人审。
            "snapshot": truncated,
            "m1b_run_dir": str(m1b),
            "failure_analysis_run_dir": str(fa_run_dir),
            "unobserved_result_is_not_failure": True,
            "session_identity": task_input.get("provenance", {}).get("session_identity", {}),
            "request_boundaries": task_input.get("provenance", {}).get("request_boundaries", []),
        },
    }
    report_path = output / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    manifest = {
        "schema_version": PREPARE_MANIFEST_SCHEMA,
        "m1b_run_dir": str(m1b),
        "capture_id": resolved_capture_id,
        "attempt_ref": attempt_ref,
        "source_report_id": source_report_id,
        "report_path": str(report_path),
        "evidence_path": str(evidence_path),
        "requires_review": requires_review,
        "evidence_count": len(evidence),
        "pending_tool_call_count": pending,
        "truncated": truncated,
        "session_capture_count": selection.get("session_capture_count", 1),
        "raw_session_event_count": selection.get(
            "raw_session_event_count", selection.get("source_row_count", 0)
        ),
        "deduplicated_session_event_count": selection.get(
            "deduplicated_session_event_count", len(evidence)
        ),
    }
    manifest_path = output / "prepare_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    return PreparedInputs(
        capture_id=resolved_capture_id,
        attempt_ref=attempt_ref,
        source_report_id=source_report_id,
        report_path=report_path,
        evidence_path=evidence_path,
        manifest_path=manifest_path,
        requires_review=requires_review,
    )


__all__ = [
    "PREPARE_MANIFEST_SCHEMA",
    "PREPARE_REPORT_SCHEMA",
    "PreparedInputs",
    "ReconstructionPrepareError",
    "build_reconstruction_inputs",
]
