"""对原始 JSONL 运行规则粗筛与可选模型细筛，并发布 SelectionManifest。"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
from .observable import DEFAULT_MAX_INPUT_CHARS, build_observable_evidence
from .rules import decide_rule
from .scan import scan_source_record
from .task_labels import build_session_tags


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
    concurrency: int = 8,
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    max_messages_for_triage: int = 200,
    max_source_requests_for_triage: int = 20,
) -> Path:
    """扫描原始 session JSONL。未注入模型时只做规则分流，不编译轨迹。

    消息数 / 来源请求数只记入清单，不再把超长对话挡在模型细筛之外。
    """

    source = Path(input_path)
    if not source.is_file():
        raise ScreeningInputError(f"筛选输入不存在：{source}")
    if offset < 0:
        raise ScreeningInputError("offset 不能为负")
    if limit is not None and limit < 1:
        raise ScreeningInputError("limit 必须大于 0")
    if concurrency < 1:
        raise ScreeningInputError("concurrency 必须大于 0")
    if max_input_chars < 1:
        raise ScreeningInputError("max_input_chars 必须大于 0")
    if max_messages_for_triage < 1 or max_source_requests_for_triage < 1:
        raise ScreeningInputError("screening triage limits must be positive")

    jobs: list[dict[str, Any]] = []
    kept = 0
    seen = 0
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
            jobs.append({"raw_line": raw_line, "features": features, "rule": rule})
            kept += 1

    needs_model = [
        index
        for index, job in enumerate(jobs)
        if (
            model is not None
            and job["rule"]["decision"] == ScreeningDecision.REVIEW.value
            and job["rule"]["rule_pass"]
        )
    ]
    if model is not None and needs_model:
        workers = min(concurrency, len(needs_model))
        if workers == 1:
            for index in needs_model:
                jobs[index]["triage"] = _triage_job(
                    jobs[index], model, model_name, max_input_chars
                )
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        _triage_job, jobs[index], model, model_name, max_input_chars
                    ): index
                    for index in needs_model
                }
                for future, index in futures.items():
                    jobs[index]["triage"] = future.result()

    records: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for job in jobs:
        records.append(_record_from_job(job))
        counts[str(records[-1]["decision"])] += 1

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
            "concurrency": concurrency if model is not None else 1,
            "max_input_chars": max_input_chars,
            "max_messages_for_triage": max_messages_for_triage,
            "max_source_requests_for_triage": max_source_requests_for_triage,
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
                    "concurrency": concurrency if model is not None else 1,
                    "max_input_chars": max_input_chars,
                    "max_messages_for_triage": max_messages_for_triage,
                    "max_source_requests_for_triage": max_source_requests_for_triage,
                    "downstream": "ELIGIBLE_ONLY",
                    "cost_defer_long_sessions": False,
                    "eligible_requires": {
                        "any_task_passes": True,
                        "task_labels": "v10 stable task_id/is_actionable/evidence_refs/relations",
                        "primary": "valid_task_and_not_done_well",
                        "intent": "task_identifiability>=2",
                        "unfinished": "FAILURE|INCOMPLETE and failure_evidence>=2",
                        "tools": "auxiliary_not_required",
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
                "model_call_count": len(needs_model),
                "concurrency": concurrency if model is not None else 1,
                "max_input_chars": max_input_chars,
                "max_messages_for_triage": max_messages_for_triage,
                "max_source_requests_for_triage": max_source_requests_for_triage,
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


def _triage_job(
    job: dict[str, Any], model: ChatModel, model_name: str, max_input_chars: int
) -> dict[str, Any]:
    return judge_reconstructability(
        evidence=build_observable_evidence(
            job["raw_line"], job["features"], max_input_chars=max_input_chars
        ),
        rule=job["rule"],
        model=model,
        model_name=model_name,
        max_input_chars=max_input_chars,
    )


def _record_from_job(job: dict[str, Any]) -> dict[str, Any]:
    features = job["features"]
    rule = job["rule"]
    triage = job.get("triage")
    if triage is None:
        decision = dict(rule)
    else:
        decision = {
            "decision": triage["decision"],
            "route": triage["route"],
            "rule_pass": triage["rule_pass"],
            "blocking_reason_codes": triage["blocking_reason_codes"],
        }
    return {
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
                "relations": list(triage.get("relations") or ()),
                "label_status": triage.get("label_status"),
                "selected_span_ids": list(triage.get("selected_span_ids") or ()),
                "selected_task_ids": list(triage.get("selected_task_ids") or ()),
                "session_tags": build_session_tags(
                    tasks=list(triage.get("tasks") or ()),
                    relations=list(triage.get("relations") or ()),
                    decision=decision["decision"],
                ),
                "prompt_version": triage.get("prompt_version"),
                "model": triage.get("model"),
                "model_receipt": triage.get("model_receipt"),
                "errors": list(triage.get("errors") or ()),
            }
        ),
    }


def _write_records(root: Path, records: list[dict[str, Any]]) -> ArtifactEntryV1:
    writer = JsonlArtifactWriter(root, "private/records.jsonl")
    try:
        for record in records:
            writer.write(record)
        return writer.close()
    except Exception:
        writer.abort()
        raise
