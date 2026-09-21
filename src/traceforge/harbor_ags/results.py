"""读取 Harbor Job 结果并计算不含臆测的 rollout 指标。"""

from __future__ import annotations

import hashlib
import json
import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ROLLOUT_RESULTS_SCHEMA = "traceforge.harbor-ags-rollout-results.v1"
_HARBOR_AGS_SRC = Path(
    "/mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags/src"
)


class HarborResultError(RuntimeError):
    """Harbor 结果目录缺失或内容不满足契约。"""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborResultError(f"无法读取 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise HarborResultError(f"JSON 根节点必须是对象：{path}")
    return value


_SECRET_RE = re.compile(r"(?i)(?:authorization\s*:\s*bearer\s+|(?:api[_-]?key|token|secret|password)\s*[=:]\s*)([^\s,;]+)|\bsk-[A-Za-z0-9_-]{12,}\b")


def _redact_detail(value: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", value[-1200:])


def _trial_diagnostic(trial_dir: Path, result: dict[str, Any]) -> dict[str, str]:
    """保留短的失败原因，避免只暴露 TrajectoryCaptureError 包装层。"""
    diagnostic: dict[str, str] = {}
    exception = result.get("exception_info")
    if isinstance(exception, dict):
        kind = exception.get("exception_type")
        message = exception.get("exception_message")
        if isinstance(kind, str) and kind:
            diagnostic["error_code"] = kind
        if isinstance(message, str) and message:
            diagnostic["error_detail"] = _redact_detail(message)
    try:
        payload = json.loads((trial_dir / "agent/hermes-result.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = None
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict) and meta.get("failed") is True:
        final_response = meta.get("final_response")
        if isinstance(final_response, str) and final_response:
            diagnostic["agent_error"] = _redact_detail(final_response)
    return diagnostic


def _duration_seconds(value: dict[str, Any]) -> float | None:
    started = value.get("started_at")
    finished = value.get("finished_at")
    try:
        if isinstance(started, str) and isinstance(finished, str):
            left = datetime.fromisoformat(started.replace("Z", "+00:00"))
            right = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            return max(0.0, (right - left).total_seconds())
    except ValueError:
        return None
    details = value.get("agent_result")
    if isinstance(details, dict):
        metadata = details.get("metadata")
        evidence = metadata.get("_harbor_ags_evidence") if isinstance(metadata, dict) else None
        elapsed = evidence.get("elapsed_s") if isinstance(evidence, dict) else None
        if isinstance(elapsed, int | float):
            return float(elapsed)
    return None


def _trial_status(result: dict[str, Any], verdict: dict[str, Any] | None) -> str:
    exception = result.get("exception_info")
    if isinstance(exception, dict):
        kind = str(exception.get("exception_type", ""))
        return "TIMEOUT" if "Timeout" in kind else "INFRA_ERROR"
    verifier_result = result.get("verifier_result")
    rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
    reward = rewards.get("task") if isinstance(rewards, dict) else None
    verdict_status = verdict.get("status") if verdict else None
    if reward == 1.0 and verdict_status in {"TASK_PASS", "PASS"}:
        return "PASS"
    if reward == 0.0 and verdict_status in {"TASK_FAIL", "FAIL"}:
        return "FAIL"
    return "INCONCLUSIVE"


def _import_audit_sandbox_ledger() -> Any:
    try:
        from harbor_ags.sandbox_ledger import audit_sandbox_ledger

        return audit_sandbox_ledger
    except ImportError:
        pass
    candidates = (
        _HARBOR_AGS_SRC,
        Path(__file__).resolve().parents[4] / "harbor_ags" / "src",
    )
    for source in candidates:
        if not (source / "harbor_ags" / "sandbox_ledger.py").is_file():
            continue
        inserted = str(source)
        if inserted not in sys.path:
            sys.path.insert(0, inserted)
        try:
            from harbor_ags.sandbox_ledger import audit_sandbox_ledger

            return audit_sandbox_ledger
        except ImportError:
            continue
    raise HarborResultError("无法导入 harbor_ags.sandbox_ledger.audit_sandbox_ledger")


def _cleanup_ok(ledger: Path) -> bool | None:
    if not ledger.is_file():
        return None
    try:
        audit = _import_audit_sandbox_ledger()(ledger)
    except Exception:
        return False
    return bool(getattr(audit, "ok", False))


def _requires_hermes_artifacts(agent_mode: str) -> bool:
    return agent_mode == "hermes"


def _validate_hermes_artifacts(trial_dir: Path) -> tuple[bool, list[str]]:
    """Validate the content of Hermes artifacts, rather than only their paths.

    A present-but-corrupt trajectory must never be eligible for SFT.  The
    artifact manifest is generated by the Harbor control plane and must have
    the locked schema and at least one hashed file.
    """

    errors: list[str] = []
    trajectory = trial_dir / "agent/trajectory.full.json"
    manifest = trial_dir / "artifacts/manifest.json"
    try:
        value = json.loads(trajectory.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != "traceforge-lossless-trajectory-v1" or not value.get("messages"):
            errors.append("TRAJECTORY_SCHEMA_OR_MESSAGES_INVALID")
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append("TRAJECTORY_INVALID_JSON")
    try:
        payload = _read_json(manifest)
        if payload.get("schema_version") != "traceforge-harbor-artifacts/v1":
            errors.append("ARTIFACT_MANIFEST_SCHEMA_INVALID")
        files = payload.get("files")
        if not isinstance(files, list) or not files:
            errors.append("ARTIFACT_MANIFEST_EMPTY")
        else:
            for item in files:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    errors.append("ARTIFACT_MANIFEST_ENTRY_INVALID")
                    break
                rel = Path(item["path"])
                artifact_root = trial_dir / "artifacts"
                artifact = artifact_root / rel
                if rel.is_absolute() or ".." in rel.parts or artifact.is_symlink() or not artifact.resolve().is_relative_to(artifact_root.resolve()):
                    errors.append("ARTIFACT_MANIFEST_PATH_UNSAFE")
                    break
                if not artifact.is_file():
                    errors.append("ARTIFACT_MANIFEST_FILE_MISSING")
                    break
                if item.get("sha256") != hashlib.sha256(artifact.read_bytes()).hexdigest():
                    errors.append("ARTIFACT_MANIFEST_HASH_MISMATCH")
                    break
    except HarborResultError:
        errors.append("ARTIFACT_MANIFEST_INVALID_JSON")
    try:
        receipt = _read_json(trial_dir / "reconstruction-certification.json")
        if receipt.get("schema_version") != "traceforge.rollout-certification.v1" or receipt.get("certified") is not True:
            errors.append("HERMES_CERTIFICATION_FAILED")
        for label, path in (("trajectory_sha256", trajectory), ("manifest_sha256", manifest)):
            if not path.is_file() or receipt.get(label) != hashlib.sha256(path.read_bytes()).hexdigest():
                errors.append("HERMES_CERTIFICATION_STALE")
    except (HarborResultError, OSError):
        errors.append("HERMES_CERTIFICATION_MISSING")
    return not errors, list(dict.fromkeys(errors))


def certify_hermes_job(job_dir: Path | str, *, harbor_root: Path | str | None = None) -> None:
    """保存可信 validator 结果，并将认证绑定到当前轨迹与 manifest 字节。"""
    root = Path(job_dir).resolve()
    sources = ([Path(harbor_root).resolve() / "src"] if harbor_root is not None else []) + [
        _HARBOR_AGS_SRC, Path(__file__).resolve().parents[4] / "harbor_ags" / "src",
    ]
    for source in sources:
        if (source / "harbor_ags/artifacts.py").is_file() and str(source) not in sys.path:
            sys.path.insert(0, str(source))
            break
    for trial_dir in sorted(path for path in root.iterdir() if path.is_dir() and path.name != "_control"):
        receipt: dict[str, Any] = {"schema_version": "traceforge.rollout-certification.v1", "certified": False}
        try:
            from harbor_ags.artifacts import build_artifact_manifest
            from harbor_ags.validator import validate_harbor_trial
            build_artifact_manifest(trial_dir)
            validated = validate_harbor_trial(trial_dir)
            receipt["certified"] = getattr(validated, "certified", False) is True
            receipt["validator_status"] = str(getattr(validated, "status", "UNKNOWN"))
        except Exception as exc:
            receipt["error_code"] = type(exc).__name__
        for label, path in (
            ("trajectory_sha256", trial_dir / "agent/trajectory.full.json"),
            ("manifest_sha256", trial_dir / "artifacts/manifest.json"),
        ):
            if path.is_file():
                receipt[label] = hashlib.sha256(path.read_bytes()).hexdigest()
        (trial_dir / "reconstruction-certification.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_rollout_results(
    job_dir: Path | str,
    *,
    agent_mode: str = "hermes",
    expected_trial_count: int | None = None,
) -> dict[str, Any]:
    """读取 Job 下每个 trial 的 reward/verdict/trajectory 并聚合指标。"""

    root = Path(job_dir).resolve()
    if not root.is_dir():
        raise HarborResultError(f"Job 目录不存在：{root}")
    trials: list[dict[str, Any]] = []
    trial_dirs = sorted(
        path for path in root.iterdir() if path.is_dir() and path.name != "_control"
    )
    hermes_artifacts = _requires_hermes_artifacts(agent_mode)
    for trial_dir in trial_dirs:
        result_path = trial_dir / "result.json"
        if not result_path.is_file():
            trials.append(
                {
                    "trial_name": trial_dir.name,
                    "status": "INFRA_ERROR",
                    "reward": None,
                    "verdict_status": None,
                    "trajectory_present": False,
                    "artifact_manifest_present": False,
                    "tokens": {"input": None, "cache": None, "output": None},
                    "duration_seconds": None,
                    "result_path": str(result_path),
                    "verdict_path": None,
                    "error_code": "TRIAL_RESULT_MISSING",
                }
            )
            continue
        result = _read_json(result_path)
        verdict_path = trial_dir / "verifier/verdict.json"
        verdict = _read_json(verdict_path) if verdict_path.is_file() else None
        trajectory_path = trial_dir / "agent/trajectory.full.json"
        artifact_manifest_path = trial_dir / "artifacts/manifest.json"
        agent_result = (
            result.get("agent_result") if isinstance(result.get("agent_result"), dict) else {}
        )
        verifier_result = result.get("verifier_result")
        rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
        token_info = {
            "input": agent_result.get("n_input_tokens"),
            "cache": agent_result.get("n_cache_tokens"),
            "output": agent_result.get("n_output_tokens"),
        }
        content_valid, content_errors = _validate_hermes_artifacts(trial_dir) if hermes_artifacts else (True, [])
        diagnostic = _trial_diagnostic(trial_dir, result)
        trials.append(
            {
                "trial_name": trial_dir.name,
                "status": _trial_status(result, verdict),
                "reward": rewards.get("task") if isinstance(rewards, dict) else None,
                "verdict_status": verdict.get("status") if verdict else None,
                "trajectory_present": trajectory_path.is_file(),
                "trajectory_path": str(trajectory_path) if trajectory_path.is_file() else None,
                "artifact_manifest_present": artifact_manifest_path.is_file(),
                "content_valid": content_valid,
                "content_errors": content_errors,
                "tokens": token_info,
                "duration_seconds": _duration_seconds(result),
                "result_path": str(result_path),
                "verdict_path": str(verdict_path) if verdict_path.is_file() else None,
                **diagnostic,
            }
        )
    trial_count_mismatch = False
    if expected_trial_count is not None:
        if expected_trial_count < 1:
            raise HarborResultError("expected_trial_count 必须大于 0")
        if len(trial_dirs) != expected_trial_count:
            trial_count_mismatch = True
        missing_count = expected_trial_count - len(trials)
        for index in range(max(0, missing_count)):
            trials.append(
                {
                    "trial_name": f"missing-trial-{index + 1:03d}",
                    "status": "INFRA_ERROR",
                    "reward": None,
                    "verdict_status": None,
                    "trajectory_present": False,
                    "artifact_manifest_present": False,
                    "tokens": {"input": None, "cache": None, "output": None},
                    "duration_seconds": None,
                    "result_path": None,
                    "verdict_path": None,
                    "error_code": "TRIAL_RESULT_MISSING",
                }
            )
    total = len(trials)
    completed = sum(item["status"] in {"PASS", "FAIL"} for item in trials)
    passed = sum(item["status"] == "PASS" for item in trials)
    trajectory_count = sum(item["trajectory_present"] for item in trials)
    artifact_manifest_count = sum(item["artifact_manifest_present"] for item in trials)
    durations = [
        item["duration_seconds"] for item in trials if item["duration_seconds"] is not None
    ]
    token_totals = {
        key: sum(item["tokens"][key] for item in trials if isinstance(item["tokens"][key], int))
        for key in ("input", "cache", "output")
    }
    cleanup = _cleanup_ok(root / "_control/ags-sandbox-ledger.jsonl")
    artifact_missing = hermes_artifacts and artifact_manifest_count != total
    trajectory_missing = hermes_artifacts and trajectory_count != total
    content_invalid = hermes_artifacts and any(not item.get("content_valid") for item in trials)
    return {
        "schema_version": ROLLOUT_RESULTS_SCHEMA,
        "job_dir": str(root),
        "agent_mode": agent_mode,
        "trial_count": total,
        "expected_trial_count": expected_trial_count,
        "trials": trials,
        "metrics": {
            "completion_rate": completed / total if total else 0.0,
            "pass_rate": passed / total if total else 0.0,
            "trajectory_capture_rate": trajectory_count / total if total else 0.0,
            "artifact_manifest_rate": artifact_manifest_count / total if total else 0.0,
            "cleanup_rate": 1.0 if cleanup is True else 0.0,
            "total_tokens": token_totals,
            "mean_duration_seconds": sum(durations) / len(durations) if durations else None,
        },
        "cleanup": {"ok": cleanup},
        "quality_gate": {
            "ok": bool(
                total
                and completed == total
                and cleanup is True
                and not trial_count_mismatch
                and not artifact_missing
                and not trajectory_missing
                and not content_invalid
            ),
            "reasons": [
                reason
                for reason, condition in (
                    ("NO_TRIAL_RESULTS", total == 0),
                    ("TRIAL_COUNT_MISMATCH", trial_count_mismatch),
                    ("TRIAL_INCOMPLETE_OR_INFRA_ERROR", completed != total),
                    ("SANDBOX_CLEANUP_UNCONFIRMED", cleanup is not True),
                    ("ARTIFACT_MANIFEST_MISSING", artifact_missing),
                    ("TRAJECTORY_MISSING", trajectory_missing),
                    ("TRAJECTORY_OR_ARTIFACT_CONTENT_INVALID", content_invalid),
                )
                if condition
            ],
        },
    }


__all__ = [
    "ROLLOUT_RESULTS_SCHEMA",
    "HarborResultError",
    "certify_hermes_job",
    "read_rollout_results",
]
