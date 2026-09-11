"""只读判断补全 workspace 是否足以完成任务，而非判断 build 是否成功。"""

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

from .model_gateway import (
    ChatModel,
    ModelGatewayError,
    ModelRequest,
    parse_json_object,
    receipt_for_response,
)

SUFFICIENCY_JUDGE_SCHEMA = "traceforge.workspace-sufficiency.v1"
SUFFICIENCY_PROMPT_VERSION = "terminal-universe-workspace-sufficiency-b3-v1"


def _snapshot(root: Path) -> list[dict[str, str]]:
    if not root.is_dir():
        raise ValueError(f"workspace 不存在：{root}")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"workspace 不允许符号链接：{path}")
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                }
            )
    return files


def run_sufficiency_judge(
    *,
    task: dict[str, Any],
    workspace_root: str | Path,
    evidence: list[dict[str, Any]],
    model: ChatModel,
    output_root: str | Path,
    model_name: str = "claude-opus-4-8",
) -> Path:
    """发布 B.3 判定；UNKNOWN/REVIEW 不得进入 Harbor 编译。"""
    files = _snapshot(Path(workspace_root).resolve())
    prompt = (
        "以只读方式判断当前 workspace 是否为给定任务提供足够源码、配置、数据和结构。"
        "判断上下文是否充分，不判断 build 是否成功；缺依赖缓存、生成物或可选文档本身不算不充分。"
        "只输出 JSON object：{label:SUFFICIENT|INSUFFICIENT|UNKNOWN, reason:string, "
        "missing_context:[string], evidence_ref_ids:[string], confidence:0..1, "
        "decision:READY|REVIEW}。证据不足必须 UNKNOWN/REVIEW。"
        f"\n任务：{task!r}\nworkspace 只读快照：{files!r}\n证据索引：{evidence!r}"
    )
    request_id = stable_id(
        "traceforge.workspace-sufficiency-request-v1",
        {
            "prompt_version": SUFFICIENCY_PROMPT_VERSION,
            "model": model_name,
            "task_sha256": hashlib.sha256(canonical_json_bytes(task)).hexdigest(),
            "workspace_sha256": hashlib.sha256(canonical_json_bytes(files)).hexdigest(),
            "evidence_sha256": hashlib.sha256(canonical_json_bytes(evidence)).hexdigest(),
        },
    )
    request = ModelRequest(
        request_id,
        model_name,
        "只读审查，不修改 workspace。只返回 JSON。",
        prompt,
        SUFFICIENCY_JUDGE_SCHEMA,
    )
    response = model.complete(request)
    receipt = receipt_for_response(response)
    errors: list[str] = []
    try:
        payload = parse_json_object(response.text)
    except ModelGatewayError as exc:
        payload = {
            "label": "UNKNOWN",
            "reason": "",
            "missing_context": [],
            "evidence_ref_ids": [],
            "confidence": 0.0,
            "decision": "REVIEW",
        }
        errors.append(exc.code)
    label = str(payload.get("label", "UNKNOWN"))
    decision = str(payload.get("decision", "REVIEW"))
    refs = payload.get("evidence_ref_ids", [])
    allowed = {str(x.get("evidence_ref_id")) for x in evidence if x.get("evidence_ref_id")}
    if label not in {"SUFFICIENT", "INSUFFICIENT", "UNKNOWN"}:
        errors.append("INVALID_LABEL")
        label = "UNKNOWN"
    if decision not in {"READY", "REVIEW"}:
        errors.append("INVALID_DECISION")
        decision = "REVIEW"
    if not isinstance(refs, list) or any(str(ref) not in allowed for ref in refs):
        errors.append("EVIDENCE_REF_UNKNOWN")
        refs = []
    if not refs:
        errors.append("EVIDENCE_REQUIRED")
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
        errors.append("INVALID_CONFIDENCE")
    if not 0 <= confidence <= 1:
        confidence = 0.0
        errors.append("INVALID_CONFIDENCE")
    if errors or label == "UNKNOWN":
        decision = "REVIEW"
    result = {
        "schema_version": SUFFICIENCY_JUDGE_SCHEMA,
        "request_id": request_id,
        "label": label,
        "decision": decision,
        "reason": str(payload.get("reason", "")),
        "missing_context": [str(x) for x in payload.get("missing_context", [])]
        if isinstance(payload.get("missing_context", []), list)
        else [],
        "evidence_ref_ids": [str(x) for x in refs],
        "confidence": confidence,
        "errors": errors,
    }
    run_id = stable_id(
        "traceforge.workspace-sufficiency-run-v1",
        {"request_id": request_id, "response_sha256": response.content_sha256},
    )
    workspace = ArtifactWorkspace(Path(output_root), run_id)
    try:
        entries = [
            write_json_artifact(workspace.staging_path, "sufficiency_judgement.json", result)
        ]
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "metrics.json",
                {
                    "schema_version": "traceforge.workspace-sufficiency-metrics.v1",
                    "run_id": run_id,
                    "sample_count": 1,
                    "sufficient_count": int(label == "SUFFICIENT" and decision == "READY"),
                    "review_count": int(decision == "REVIEW"),
                    "unknown_count": int(label == "UNKNOWN"),
                },
            )
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "private/model_exchange.json",
                {
                    "schema_version": "traceforge.private-model-exchange.v1",
                    "request": {
                        "model": model_name,
                        "prompt_version": SUFFICIENCY_PROMPT_VERSION,
                        "prompt": prompt,
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    },
                    "response": {
                        "text": response.text,
                        "sha256": response.content_sha256,
                        "receipt": asdict(receipt),
                    },
                    "credentials_embedded": False,
                },
            )
        )
        manifest = write_json_artifact(
            workspace.staging_path,
            "artifact_manifest.json",
            {
                "schema_version": "traceforge.workspace-sufficiency-run.v1",
                "run_id": run_id,
                "status": decision,
                "files": artifact_entry_dicts(entries),
            },
        )
        write_json_artifact(
            workspace.staging_path,
            "run_receipt.json",
            {
                "schema_version": "traceforge.workspace-sufficiency-receipt.v1",
                "run_id": run_id,
                "artifact_manifest_sha256": manifest.sha256,
                "model_calls": 1,
            },
        )
        return workspace.publish()
    except BaseException:
        workspace.abort()
        raise


__all__ = ["SUFFICIENCY_JUDGE_SCHEMA", "SUFFICIENCY_PROMPT_VERSION", "run_sufficiency_judge"]
