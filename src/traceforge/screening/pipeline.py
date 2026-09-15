"""对原始 JSONL 运行规则粗筛与可选模型细筛，并发布 SelectionManifest。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from traceforge.reconstruction.model_gateway import ChatModel
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    JsonlArtifactWriter,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.trajectory.contracts import ArtifactEntryV1
from traceforge.trajectory.json_codec import stable_id

from .contracts import (
    SCREENING_CONTRACT_VERSION,
    SCREENING_RECORD_SCHEMA,
    SELECTION_MANIFEST_SCHEMA,
    TRIAGE_PROMPT_VERSION,
    ScreeningDecision,
)
from .model_triage import judge_reconstructability
from .observable import build_observable_evidence
from .rules import decide_rule
from .scan import scan_source_record


class ScreeningInputError(ValueError):
    """筛选输入不满足契约。"""


def run_reconstruction_screening(
    *,
    input_path: str | Path,
    output_root: str | Path,
    limit: int | None = None,
    offset: int = 0,
    model: ChatModel | None = None,
    model_name: str = "claude-opus-4-8",
) -> Path:
    """扫描原始 session JSONL。未注入模型时只做规则分流，不编译轨迹。"""

    source = Path(input_path)
    if not source.is_file():
        raise ScreeningInputError(f"筛选输入不存在：{source}")
    if offset < 0:
        raise ScreeningInputError("offset 不能为负")
    if limit is not None and limit < 1:
        raise ScreeningInputError("limit 必须大于 0")

    records: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    kept = 0
    seen = 0
    model_calls = 0
    with source.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            seen += 1
            if seen <= offset:
                continue
            if limit is not None and kept >= limit:
                break
            features = scan_source_record(raw_line, line_number=line_number)
            rule = decide_rule(features)
            decision = dict(rule)
            triage: dict[str, Any] | None = None
            if (
                model is not None
                and rule["decision"] == ScreeningDecision.REVIEW.value
                and rule["rule_pass"]
            ):
                triage = judge_reconstructability(
                    evidence=build_observable_evidence(raw_line, features),
                    rule=rule,
                    model=model,
                    model_name=model_name,
                )
                decision = {
                    "decision": triage["decision"],
                    "route": triage["route"],
                    "rule_pass": triage["rule_pass"],
                    "blocking_reason_codes": triage["blocking_reason_codes"],
                }
                model_calls += 1
            record = {
                "schema_version": SCREENING_RECORD_SCHEMA,
                "source_ref": features["source_ref"],
                "line_number": features["line_number"],
                "line_sha256": features["line_sha256"],
                "record_id": features.get("record_id"),
                "capture_id": features.get("capture_id"),
                "thread_id": features.get("thread_id"),
                "decision": decision["decision"],
                "route": decision["route"],
                "rule_pass": decision["rule_pass"],
                "rule_decision": rule["decision"],
                "blocking_reason_codes": list(decision["blocking_reason_codes"]),
                "features": {
                    "parse_ok": features["parse_ok"],
                    "has_user_task_like_turn": features["has_user_task_like_turn"],
                    "has_agent_attempt": features["has_agent_attempt"],
                    "has_tool_activity": features["has_tool_activity"],
                    "has_failure_or_unfinished_signal": features[
                        "has_failure_or_unfinished_signal"
                    ],
                    "message_count": features["message_count"],
                    "user_count": features["user_count"],
                    "assistant_count": features["assistant_count"],
                    "tool_message_count": features["tool_message_count"],
                    "source_request_count": features["source_request_count"],
                    "last_assistant_empty": features["last_assistant_empty"],
                    "leaf_response_status": features["leaf_response_status"],
                },
                "triage": (
                    None
                    if triage is None
                    else {
                        "outcome": triage.get("outcome"),
                        "needs_reconstruction": triage.get("needs_reconstruction"),
                        "domain_route": triage.get("domain_route"),
                        "rubric": triage.get("rubric"),
                        "reason": triage.get("reason"),
                        "tasks": list(triage.get("tasks") or ()),
                        "selected_span_ids": list(triage.get("selected_span_ids") or ()),
                        "prompt_version": triage.get("prompt_version"),
                        "model": triage.get("model"),
                        "model_receipt": triage.get("model_receipt"),
                        "errors": list(triage.get("errors") or ()),
                    }
                ),
            }
            records.append(record)
            counts[str(decision["decision"])] += 1
            kept += 1

    eligible_refs = [
        item["source_ref"]
        for item in records
        if item["decision"] == ScreeningDecision.ELIGIBLE.value
    ]
    run_id = stable_id(
        SCREENING_CONTRACT_VERSION,
        {
            "contract": SCREENING_CONTRACT_VERSION,
            "input": str(source.resolve()),
            "offset": offset,
            "limit": limit,
            "record_count": len(records),
            "model": model_name if model is not None else None,
            "prompt_version": TRIAGE_PROMPT_VERSION if model is not None else None,
        },
    )
    workspace = ArtifactWorkspace(Path(output_root), run_id)
    workspace.staging_path.mkdir(parents=True, exist_ok=True)
    entries = [
        write_json_artifact(
            workspace.staging_path,
            "selection_manifest.json",
            {
                "schema_version": SELECTION_MANIFEST_SCHEMA,
                "screening_run_id": run_id,
                "contract_version": SCREENING_CONTRACT_VERSION,
                "source_path": str(source.resolve()),
                "offset": offset,
                "limit": limit,
                "record_count": len(records),
                "decision_counts": {
                    "ELIGIBLE": counts["ELIGIBLE"],
                    "REVIEW": counts["REVIEW"],
                    "DEFER": counts["DEFER"],
                    "REJECT": counts["REJECT"],
                },
                "eligible_source_refs": eligible_refs,
                "policy": {
                    "unit": "capture_jsonl_line",
                    "rule_can_admit_eligible": False,
                    "model_triage": "RUN" if model is not None else "NOT_RUN",
                    "model_name": model_name if model is not None else None,
                    "prompt_version": TRIAGE_PROMPT_VERSION if model is not None else None,
                    "downstream": "ELIGIBLE_ONLY",
                    "eligible_requires": {
                        "any_task_passes": True,
                        "intent": "task_identifiability>=2",
                        "unfinished": "FAILURE|INCOMPLETE and failure_evidence>=2",
                        "environment_handle": "code_file",
                        "needs_reconstruction": True,
                    },
                },
            },
        ),
        _write_records(workspace.staging_path, records),
        write_json_artifact(
            workspace.staging_path,
            "metrics.json",
            {
                "schema_version": "traceforge.reconstruction-screening-metrics.v1",
                "record_count": len(records),
                "eligible_count": counts["ELIGIBLE"],
                "review_count": counts["REVIEW"],
                "defer_count": counts["DEFER"],
                "reject_count": counts["REJECT"],
                "rule_pass_count": sum(1 for item in records if item["rule_pass"]),
                "failure_signal_count": sum(
                    1
                    for item in records
                    if item["features"]["has_failure_or_unfinished_signal"]
                ),
                "model_call_count": model_calls,
            },
        ),
    ]
    write_json_artifact(
        workspace.staging_path,
        "artifact_manifest.json",
        {
            "schema_version": "traceforge.artifact-manifest.v1",
            "files": artifact_entry_dicts(entries),
        },
    )
    return workspace.publish()


def _write_records(root: Path, records: list[dict[str, Any]]) -> ArtifactEntryV1:
    writer = JsonlArtifactWriter(root, "private/records.jsonl")
    try:
        for record in records:
            writer.write(record)
        return writer.close()
    except Exception:
        writer.abort()
        raise
