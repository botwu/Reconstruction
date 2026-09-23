#!/usr/bin/env python3
"""Run many terminal reconstruction candidates with isolated Harbor outputs.

The input manifest is JSONL with one object per candidate::

    {"input": "return_data/four_batch/by-rubric/R04.jsonl",
     "records": "/tmp/traceforge-screen-r04-l262/.../records.jsonl",
     "line_number": 262,
     "label": "r04-l262"}

Every row invokes the real ``traceforge reconstruct run`` command.  A failed
or skipped row is recorded from its own reconstruction manifest; this driver
never synthesizes verifier or rollout results.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from traceforge.reconstruction.batch_process import run_batch_process

SCHEMA = "traceforge.terminal-batch.v1"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    out = _SAFE.sub("-", value).strip(".-")
    return out[:80] or "candidate"


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{number}: invalid JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise SystemExit(f"{path}:{number}: candidate must be an object")
        try:
            line = int(item["line_number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"{path}:{number}: line_number must be a positive integer") from exc
        if line <= 0 or not isinstance(item.get("input"), str) or not item["input"].strip():
            raise SystemExit(f"{path}:{number}: input and positive line_number are required")
        if not isinstance(item.get("records"), str) or not item["records"].strip():
            raise SystemExit(f"{path}:{number}: records is required")
        rows.append({**item, "line_number": line})
    if not rows:
        raise SystemExit(f"{path}: no candidates")
    return rows


def _run_one(
    index: int,
    candidate: dict[str, Any],
    args: argparse.Namespace,
    output_root: Path,
) -> dict[str, Any]:
    label = str(candidate.get("label") or f"line-{candidate['line_number']}")
    output = output_root / f"{index:04d}-{_slug(label)}"
    output.mkdir(parents=True, exist_ok=True)
    command = [
        args.python,
        "-m",
        "traceforge",
        "reconstruct",
        "run",
        "--input",
        candidate["input"],
        "--records",
        candidate["records"],
        "--line-number",
        str(candidate["line_number"]),
        "--output",
        str(output),
        "--config",
        args.config,
        "--hermes-home",
        args.hermes_home,
        "--harbor-root",
        args.harbor_root,
        "--rollout-trials",
        str(args.rollout_trials),
        "--rollout-timeout-seconds",
        str(args.rollout_timeout_seconds),
        "--rollout-max-iterations",
        str(args.rollout_max_iterations),
    ]
    if args.sandbox:
        command.append("--sandbox")
    if args.execute_red:
        command.append("--execute-red")
    if args.execute_rollout:
        command.append("--execute-rollout")
    started = _now()
    try:
        return_code = run_batch_process(
            command,
            cwd=args.repo_root,
            stdout_path=output / "run.log",
            timeout_seconds=args.session_timeout_seconds,
        )
    except OSError as exc:
        (output / "run.log").write_text(str(exc) + "\n", encoding="utf-8")
        return_code = 127
    manifest_path = output / "reconstruction_manifest.json"
    manifest: dict[str, Any] | None = None
    if manifest_path.is_file():
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            manifest = None
    return {
        "index": index,
        "label": label,
        "input": candidate["input"],
        "records": candidate["records"],
        "line_number": candidate["line_number"],
        "output": str(output),
        "return_code": return_code,
        "status": (
            "PROCESS_TIMEOUT" if return_code is None
            else (manifest or {}).get("status", "PROCESS_ERROR")
        ),
        "timeout_seconds": args.session_timeout_seconds,
        "stopped_at": (manifest or {}).get("stopped_at"),
        "manifest": str(manifest_path) if manifest is not None else None,
        "started_at": started,
        "finished_at": _now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--config", required=True)
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--harbor-root", required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--rollout-trials", type=int, default=2)
    parser.add_argument("--rollout-timeout-seconds", type=int, default=14400)
    parser.add_argument("--rollout-max-iterations", type=int, default=500)
    parser.add_argument("--session-timeout-seconds", type=int, default=7200,
                        help="整条 session 的总时限（秒），包含重建、RED 和 rollout")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--execute-red", action="store_true")
    parser.add_argument("--execute-rollout", action="store_true")
    args = parser.parse_args()
    if args.session_timeout_seconds <= 0:
        parser.error("--session-timeout-seconds 必须大于 0")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    candidates = _read_manifest(args.manifest)
    args.output_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_run_one, i, candidate, args, args.output_root): i
            for i, candidate in enumerate(candidates)
        }
        for future in as_completed(futures):
            rows.append(future.result())
    rows.sort(key=lambda item: item["index"])
    payload = {
        "schema_version": SCHEMA,
        "status": (
            "COMPLETED"
            if all(item["status"] in {"READY", "READY_VARIANT"} for item in rows)
            else "COMPLETED_WITH_ERRORS"
        ),
        "created_at": _now(),
        "repo_root": str(args.repo_root),
        "candidate_count": len(rows),
        "completed_count": sum(
            item["status"] in {"READY", "READY_VARIANT"} for item in rows
        ),
        "status_counts": {
            status: sum(item["status"] == status for item in rows)
            for status in sorted({item["status"] for item in rows})
        },
        "budget": {
            "rollout_trials": args.rollout_trials,
            "rollout_timeout_seconds": args.rollout_timeout_seconds,
            "rollout_max_iterations": args.rollout_max_iterations,
            "session_timeout_seconds": args.session_timeout_seconds,
        },
        "candidates": rows,
    }
    path = args.output_root / "batch_manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    return 0 if all(item["return_code"] == 0 for item in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
