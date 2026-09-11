"""读取 Harbor Job 结果并计算不含臆测的 rollout 指标。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROLLOUT_RESULTS_SCHEMA = "traceforge.harbor-ags-rollout-results.v1"


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
        elapsed = details.get("metadata", {}).get("_harbor_ags_evidence", {}).get("elapsed_s")
        if isinstance(elapsed, int | float):
            return float(elapsed)
    return None


def _trial_status(result: dict[str, Any], verdict: dict[str, Any] | None) -> str:
    exception = result.get("exception_info")
    if isinstance(exception, dict):
        kind = str(exception.get("exception_type", ""))
        return "TIMEOUT" if "Timeout" in kind else "INFRA_ERROR"
    reward = result.get("verifier_result", {}).get("rewards", {}).get("task")
    verdict_status = verdict.get("status") if verdict else None
    if reward == 1.0 and verdict_status in {"TASK_PASS", "PASS"}:
        return "PASS"
    if reward == 0.0 and verdict_status in {"TASK_FAIL", "FAIL"}:
        return "FAIL"
    return "INCONCLUSIVE"


def _cleanup_ok(ledger: Path) -> bool | None:
    if not ledger.is_file():
        return None
    created: set[str] = set()
    terminal: set[str] = set()
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            sandbox = row.get("sandbox_id")
            if not isinstance(sandbox, str):
                continue
            if row.get("event") == "created":
                created.add(sandbox)
            if row.get("event") in {"stopped", "deleted", "delete_confirmed"}:
                terminal.add(sandbox)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(created) and created <= terminal


def read_rollout_results(job_dir: Path | str, *, agent_mode: str = "hermes") -> dict[str, Any]:
    """读取 Job 下每个 trial 的 reward/verdict/trajectory 并聚合指标。"""

    root = Path(job_dir).resolve()
    if not root.is_dir():
        raise HarborResultError(f"Job 目录不存在：{root}")
    trials: list[dict[str, Any]] = []
    trial_dirs = sorted(
        path for path in root.iterdir() if path.is_dir() and path.name != "_control"
    )
    for trial_dir in trial_dirs:
        result_path = trial_dir / "result.json"
        if not result_path.is_file():
            continue
        result = _read_json(result_path)
        verdict_path = trial_dir / "verifier/verdict.json"
        verdict = _read_json(verdict_path) if verdict_path.is_file() else None
        trajectory_path = trial_dir / "agent/trajectory.full.json"
        agent_result = (
            result.get("agent_result") if isinstance(result.get("agent_result"), dict) else {}
        )
        token_info = {
            "input": agent_result.get("n_input_tokens"),
            "cache": agent_result.get("n_cache_tokens"),
            "output": agent_result.get("n_output_tokens"),
        }
        trials.append(
            {
                "trial_name": trial_dir.name,
                "status": _trial_status(result, verdict),
                "reward": result.get("verifier_result", {}).get("rewards", {}).get("task"),
                "verdict_status": verdict.get("status") if verdict else None,
                "trajectory_present": trajectory_path.is_file(),
                "tokens": token_info,
                "duration_seconds": _duration_seconds(result),
                "result_path": str(result_path),
                "verdict_path": str(verdict_path) if verdict_path.is_file() else None,
            }
        )
    total = len(trials)
    completed = sum(item["status"] in {"PASS", "FAIL"} for item in trials)
    passed = sum(item["status"] == "PASS" for item in trials)
    trajectory_count = sum(item["trajectory_present"] for item in trials)
    durations = [
        item["duration_seconds"] for item in trials if item["duration_seconds"] is not None
    ]
    token_totals = {
        key: sum(item["tokens"][key] for item in trials if isinstance(item["tokens"][key], int))
        for key in ("input", "cache", "output")
    }
    cleanup = _cleanup_ok(root / "_control/ags-sandbox-ledger.jsonl")
    return {
        "schema_version": ROLLOUT_RESULTS_SCHEMA,
        "job_dir": str(root),
        "agent_mode": agent_mode,
        "trial_count": total,
        "trials": trials,
        "metrics": {
            "completion_rate": completed / total if total else 0.0,
            "pass_rate": passed / total if total else 0.0,
            "trajectory_capture_rate": trajectory_count / total if total else 0.0,
            "cleanup_rate": 1.0 if cleanup is True else 0.0,
            "total_tokens": token_totals,
            "mean_duration_seconds": sum(durations) / len(durations) if durations else None,
        },
        "cleanup": {"ok": cleanup},
        "quality_gate": {
            "ok": bool(total and completed == total and cleanup is True),
            "reasons": [
                reason
                for reason, condition in (
                    ("NO_TRIAL_RESULTS", total == 0),
                    ("TRIAL_INCOMPLETE_OR_INFRA_ERROR", completed != total),
                    ("SANDBOX_CLEANUP_UNCONFIRMED", cleanup is not True),
                )
                if condition
            ],
        },
    }


__all__ = ["ROLLOUT_RESULTS_SCHEMA", "HarborResultError", "read_rollout_results"]
