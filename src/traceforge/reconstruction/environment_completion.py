"""Terminal-Universe Environment Completion 策略的可审计 artifact runner。"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import asdict
from pathlib import Path, PurePosixPath
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

ENVIRONMENT_COMPLETION_RUN_SCHEMA = "traceforge.environment-completion-run.v1"
ENVIRONMENT_COMPLETION_PROMPT_VERSION = "terminal-universe-environment-completion-b1-v1"


class EnvironmentCompletionError(RuntimeError):
    """重放 workspace 或模型补全不满足安全边界。"""


def _safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise EnvironmentCompletionError(f"不安全的 workspace 路径：{value!r}")
    if path.parts[0] in {"solution", "tests", "environment"}:
        raise EnvironmentCompletionError(f"补全不得写入隐藏或运行时目录：{value}")
    return path.as_posix()


def _prompt(
    task: dict[str, Any], replay_files: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> str:
    return (
        "Complete a Docker workspace so that the given task becomes solvable, but NOT solved. "
        "只补缺失文件或补齐明确标为 PARTIAL 的文件。不得修改 COMPLETE 文件，不得实现任务、"
        "写答案、测试、solution、答案提示或预期输出。只输出 JSON object："
        "{candidates:[{files:[{path,content,provenance,evidence_ref_ids}],dependencies:[],"
        "runtime_constraints:[],uncertainties:[],decision:READY|REVIEW|DEFER|REJECT}],"
        "open_questions:[]}，最多 3 个候选。"
        f"\n任务：{task!r}\n确定性重放文件（含实际内容）：{replay_files!r}"
        f"\n证据索引：{evidence!r}"
    )


def _validate_replay(root: Path, replay_files: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not root.is_dir():
        raise EnvironmentCompletionError(f"重放 workspace 不存在：{root}")
    indexed: dict[str, dict[str, Any]] = {}
    for item in replay_files:
        if not isinstance(item, dict):
            raise EnvironmentCompletionError("replay_files 元素必须是对象")
        path = _safe_path(str(item.get("path", "")))
        if path in indexed:
            raise EnvironmentCompletionError(f"重放文件重复：{path}")
        completeness = str(item.get("completeness", "UNKNOWN"))
        if completeness not in {"COMPLETE", "PARTIAL", "UNKNOWN"}:
            raise EnvironmentCompletionError(f"非法 completeness：{completeness}")
        source = root / path
        if not source.is_file() or source.is_symlink():
            raise EnvironmentCompletionError(f"重放文件不存在或为符号链接：{path}")
        expected = item.get("content_sha256")
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if expected and expected != actual:
            raise EnvironmentCompletionError(f"重放文件 hash 不匹配：{path}")
        try:
            content = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise EnvironmentCompletionError(f"重放文件不是 UTF-8 文本：{path}") from exc
        indexed[path] = {
            **item,
            "content": content,
            "content_sha256": actual,
            "completeness": completeness,
        }
    for path in root.rglob("*"):
        if path.is_symlink():
            raise EnvironmentCompletionError(f"重放 workspace 不允许符号链接：{path}")
    return indexed


def _materialize(
    root: Path,
    destination: Path,
    candidate: dict[str, Any],
    replay: dict[str, dict[str, Any]],
    allowed_refs: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    shutil.copytree(root, destination)
    errors: list[str] = []
    proposed = candidate.get("files")
    if not isinstance(proposed, list):
        return [], ["FILES_MUST_BE_ARRAY"]
    provenance: dict[str, dict[str, Any]] = {
        path: {"kind": "REPLAYED", "evidence_ref_ids": list(item.get("evidence_ref_ids", []))}
        for path, item in replay.items()
    }
    seen: set[str] = set()
    for item in proposed:
        if not isinstance(item, dict):
            errors.append("FILE_NOT_OBJECT")
            continue
        try:
            path = _safe_path(str(item.get("path", "")))
        except EnvironmentCompletionError as exc:
            errors.append(str(exc))
            continue
        if path in seen:
            errors.append(f"DUPLICATE_FILE_PATH:{path}")
            continue
        seen.add(path)
        if replay.get(path, {}).get("completeness") in {"COMPLETE", "UNKNOWN"}:
            errors.append(f"PROTECTED_FILE_OVERWRITE:{path}")
            continue
        content = item.get("content")
        if not isinstance(content, str):
            errors.append(f"FILE_CONTENT_REQUIRED:{path}")
            continue
        refs = item.get("evidence_ref_ids", [])
        if (
            not isinstance(refs, list)
            or not refs
            or any(str(ref) not in allowed_refs for ref in refs)
        ):
            errors.append(f"EVIDENCE_REF_UNKNOWN:{path}")
            continue
        if item.get("provenance", "MODEL_COMPLETED") != "MODEL_COMPLETED":
            errors.append(f"PROVENANCE_FORGERY:{path}")
            continue
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        provenance[path] = {
            "kind": "MODEL_COMPLETED",
            "evidence_ref_ids": [str(ref) for ref in refs],
        }
    if errors:
        shutil.rmtree(destination)
        return [], errors
    files: list[dict[str, Any]] = []
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            relative = path.relative_to(destination).as_posix()
            content = path.read_text(encoding="utf-8")
            files.append(
                {
                    "path": relative,
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "provenance": provenance.get(
                        relative, {"kind": "REPLAYED_UNINDEXED", "evidence_ref_ids": []}
                    ),
                }
            )
    return files, []


def run_environment_completion(
    *,
    task: dict[str, Any],
    attempt_ref: str,
    replay_workspace: str | Path,
    replay_files: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    model: ChatModel,
    output_root: str | Path,
    model_name: str = "claude-opus-4-8",
) -> Path:
    """发布多个候选 workspace；任何 COMPLETE 文件覆盖都会使该候选进入 REVIEW。"""
    root = Path(replay_workspace).resolve()
    replay = _validate_replay(root, replay_files)
    enriched_replay_files = [replay[path] for path in sorted(replay)]
    prompt = _prompt(task, enriched_replay_files, evidence)
    request_id = stable_id(
        "traceforge.environment-completion-request-v1",
        {
            "attempt_ref": attempt_ref,
            "task_recovery_id": task.get("recovery_id"),
            "prompt_version": ENVIRONMENT_COMPLETION_PROMPT_VERSION,
            "task_sha256": hashlib.sha256(canonical_json_bytes(task)).hexdigest(),
            "replay_sha256": hashlib.sha256(
                canonical_json_bytes(enriched_replay_files)
            ).hexdigest(),
            "evidence_sha256": hashlib.sha256(canonical_json_bytes(evidence)).hexdigest(),
            "model": model_name,
        },
    )
    request = ModelRequest(
        request_id,
        model_name,
        "保持 workspace 可解但未解；证据不足时 REVIEW。只返回 JSON。",
        prompt,
        "traceforge.environment-completion-candidates.v1",
    )
    response = model.complete(request)
    receipt = receipt_for_response(response)
    try:
        payload = parse_json_object(response.text)
    except ModelGatewayError as exc:
        payload = {"candidates": [], "open_questions": [], "parse_error": exc.code}
    raw_candidates = payload.get("candidates", [])
    candidate_limit_error = not isinstance(raw_candidates, list) or len(raw_candidates) > 3
    raw_candidates = (
        raw_candidates if isinstance(raw_candidates, list) and len(raw_candidates) <= 3 else []
    )
    run_id = stable_id(
        "traceforge.environment-completion-run.v1",
        {
            "request_id": request_id,
            "response_sha256": response.content_sha256,
        },
    )
    workspace = ArtifactWorkspace(Path(output_root), run_id)
    allowed_refs = {str(x.get("evidence_ref_id")) for x in evidence if x.get("evidence_ref_id")}
    outputs: list[dict[str, Any]] = []
    violations = 0
    try:
        for index, candidate in enumerate(raw_candidates):
            if not isinstance(candidate, dict):
                outputs.append(
                    {"index": index, "status": "REVIEW", "errors": ["CANDIDATE_NOT_OBJECT"]}
                )
                continue
            destination = (
                workspace.staging_path / "candidates" / f"candidate-{index:03d}" / "workspace"
            )
            files, errors = _materialize(root, destination, candidate, replay, allowed_refs)
            violations += sum(error.startswith("PROTECTED_FILE_OVERWRITE") for error in errors)
            decision = str(candidate.get("decision", "REVIEW"))
            status = "READY" if not errors and decision == "READY" else "REVIEW"
            outputs.append(
                {
                    "index": index,
                    "status": status,
                    "decision": decision,
                    "workspace": str(destination.relative_to(workspace.staging_path))
                    if not errors
                    else None,
                    "files": files,
                    "dependencies": candidate.get("dependencies", []),
                    "runtime_constraints": candidate.get("runtime_constraints", []),
                    "uncertainties": candidate.get("uncertainties", []),
                    "confidence": candidate.get("confidence", 0.0),
                    "errors": errors,
                }
            )
        if candidate_limit_error:
            outputs.append({"status": "REVIEW", "errors": ["CANDIDATE_LIMIT_OR_TYPE_INVALID"]})
        entries = [
            write_json_artifact(
                workspace.staging_path,
                "environment_completion.json",
                {
                    "schema_version": ENVIRONMENT_COMPLETION_RUN_SCHEMA,
                    "run_id": run_id,
                    "attempt_ref": attempt_ref,
                    "candidates": outputs,
                    "open_questions": payload.get("open_questions", []),
                },
            )
        ]
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "metrics.json",
                {
                    "schema_version": "traceforge.environment-completion-metrics.v1",
                    "run_id": run_id,
                    "candidate_count": len(raw_candidates),
                    "ready_count": sum(item.get("status") == "READY" for item in outputs),
                    "complete_overwrite_violation_count": violations,
                    "review_count": sum(item.get("status") == "REVIEW" for item in outputs),
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
                        "prompt_version": ENVIRONMENT_COMPLETION_PROMPT_VERSION,
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
                "schema_version": ENVIRONMENT_COMPLETION_RUN_SCHEMA,
                "run_id": run_id,
                "status": "READY" if any(x.get("status") == "READY" for x in outputs) else "REVIEW",
                "files": artifact_entry_dicts(entries),
            },
        )
        write_json_artifact(
            workspace.staging_path,
            "run_receipt.json",
            {
                "schema_version": "traceforge.environment-completion-receipt.v1",
                "run_id": run_id,
                "artifact_manifest_sha256": manifest.sha256,
                "model_calls": 1,
            },
        )
        return workspace.publish()
    except BaseException:
        workspace.abort()
        raise


__all__ = [
    "ENVIRONMENT_COMPLETION_RUN_SCHEMA",
    "EnvironmentCompletionError",
    "run_environment_completion",
]
