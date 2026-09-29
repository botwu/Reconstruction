#!/usr/bin/env python3
"""按冻结 inventory 逐条运行 RAW_SESSION 重建，并逐阶段记录结果。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from traceforge.reconstruction.batch_process import run_batch_process

BATCH_SCHEMA = "traceforge.raw-session-batch.v1"


class BatchInputError(ValueError):
    """冻结输入未满足批处理门禁。"""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchInputError(f"无法读取 JSON：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BatchInputError(f"JSON 顶层不是对象：{path}")
    return payload


def load_inventory(manifest_path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_file = Path(manifest_path).resolve()
    manifest = _read_json(manifest_file)
    if manifest.get("schema") != "traceforge.session-source-manifest.v1":
        raise BatchInputError("source_manifest schema 不匹配")
    if manifest.get("status") != "READY" or manifest.get("coverage_complete") is not True:
        raise BatchInputError("source_manifest 不是 READY/coverage_complete=true，拒绝批处理")
    sessions_ref = manifest.get("sessions")
    if not isinstance(sessions_ref, str):
        raise BatchInputError("source_manifest 缺少 sessions")
    sessions_path = Path(sessions_ref)
    if not sessions_path.is_absolute():
        sessions_path = (manifest_file.parent / sessions_path).resolve()
    if not sessions_path.is_file():
        raise BatchInputError(f"sessions.jsonl 不存在：{sessions_path}")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(sessions_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BatchInputError(f"sessions.jsonl 第 {number} 行非法 JSON") from exc
        if not isinstance(row, dict):
            raise BatchInputError(f"sessions.jsonl 第 {number} 行不是对象")
        if row.get("input_status") != "PENDING":
            raise BatchInputError(
                f"{row.get('session_id', number)} input_status={row.get('input_status')}"
            )
        for key in ("session_id", "input", "rubric", "line_number", "line_sha256"):
            if not row.get(key):
                raise BatchInputError(f"session row 缺字段：{key}")
        rows.append(row)
    if len(rows) != int(manifest.get("pending_sessions") or 0):
        raise BatchInputError(
            f"inventory pending_sessions={manifest.get('pending_sessions')}，实际={len(rows)}"
        )
    if len({str(row["session_id"]) for row in rows}) != len(rows):
        raise BatchInputError("session_id 重复，拒绝批处理")
    return manifest, rows


def _selected(
    rows: list[dict[str, Any]], *, offset: int, limit: int | None
) -> list[dict[str, Any]]:
    if offset < 0 or (limit is not None and limit < 0):
        raise BatchInputError("offset/limit 不能为负数")
    selected = rows[offset:]
    return selected if limit is None else selected[:limit]


def _plan_payload(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    rows: list[dict[str, Any]],
    offset: int,
    limit: int | None,
    output_root: Path,
    domain: str,
) -> dict[str, Any]:
    return {
        "schema_version": BATCH_SCHEMA,
        "status": "PLANNED",
        "domain": domain,
        "created_at": datetime.now(UTC).isoformat(),
        "inventory_manifest": str(manifest_path),
        "inventory_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "coverage_complete": bool(manifest.get("coverage_complete")),
        "rubrics": list(manifest.get("rubrics") or []),
        "total_inventory_sessions": len(rows),
        "offset": offset,
        "limit": limit,
        "selected_sessions": len(_selected(rows, offset=offset, limit=limit)),
        "output_root": str(output_root),
        "sessions": [
            {
                "session_id": row["session_id"],
                "rubric": row["rubric"],
                "label": row.get("label"),
                "input": row["input"],
                "line_number": row["line_number"],
                "line_sha256": row["line_sha256"],
                "status": "PLANNED",
            }
            for row in _selected(rows, offset=offset, limit=limit)
        ],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def execute_batch(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
    config: str | Path,
    domain: str,
    offset: int = 0,
    limit: int | None = None,
    workers: int = 1,
    sandbox: bool = True,
    execute_red: bool = False,
    execute_rollout: bool = False,
    rollout_trials: int = 2,
    session_timeout_seconds: int = 7200,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    if domain not in {"search", "terminal"}:
        raise BatchInputError("domain 必须由调用方指定为 search 或 terminal")
    if workers != 1:
        raise BatchInputError("当前批处理先固定 workers=1，避免共享 Hermes/AGS 资源污染")
    if session_timeout_seconds <= 0:
        raise BatchInputError("session_timeout_seconds 必须大于 0")
    manifest_file = Path(manifest_path).resolve()
    _manifest, rows = load_inventory(manifest_file)
    selected = _selected(rows, offset=offset, limit=limit)
    root = Path(output_root).resolve()
    if (root / "batch_manifest.json").exists():
        raise FileExistsError(f"拒绝覆盖已有批次：{root / 'batch_manifest.json'}")
    repo = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
    config_file = Path(config).resolve()
    if not config_file.is_file():
        raise BatchInputError(f"config 不存在：{config_file}")
    root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    result_file = root / "batch_results.jsonl"
    with result_file.open("w", encoding="utf-8") as result_stream:
        for row in selected:
            session_id = str(row["session_id"])
            run_root = root / "runs" / session_id
            run_root.mkdir(parents=True, exist_ok=False)
            command = [
                sys.executable,
                "-c",
                "from traceforge.cli import main; raise SystemExit(main())",
                "reconstruct",
                "raw-run",
                "--domain",
                domain,
                "--input",
                str(row["input"]),
                "--line-number",
                str(row["line_number"]),
                "--line-sha256",
                str(row["line_sha256"]),
                "--source-ref",
                f"{row['rubric']}:{row.get('label') or row['session_id']}:{row['input']}",
                "--output",
                str(run_root),
                "--config",
                str(config_file),
                "--rollout-trials",
                str(rollout_trials),
            ]
            command.append("--sandbox" if sandbox else "--no-sandbox")
            if execute_red:
                command.append("--execute-red")
            if execute_rollout:
                command.append("--execute-rollout")
            env = dict(os.environ)
            env["PYTHONPATH"] = str(repo / "src") + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )
            started = datetime.now(UTC).isoformat()
            return_code = run_batch_process(
                command,
                cwd=repo,
                env=env,
                stdout_path=run_root / "batch_stdout.txt",
                stderr_path=run_root / "batch_stderr.txt",
                timeout_seconds=session_timeout_seconds,
            )
            manifest_file_run = run_root / "reconstruction_manifest.json"
            if manifest_file_run.is_file():
                result_manifest = _read_json(manifest_file_run)
                status = str(result_manifest.get("status") or "UNKNOWN")
                stage_receipt = str(manifest_file_run)
            else:
                result_manifest = None
                segmentation_receipt = (
                    run_root / "session_segmentation" / "session_task_segmentation.json"
                )
                if segmentation_receipt.is_file():
                    receipt = _read_json(segmentation_receipt)
                    receipt_status = str(receipt.get("status") or "SESSION_TASK_REVIEW")
                    # 只有分段 READY 不能代表重建已经完成。
                    status = "PROCESS_ERROR" if receipt_status == "READY" else receipt_status
                    stage_receipt = str(segmentation_receipt)
                else:
                    status = "PROCESS_ERROR"
                    stage_receipt = None
            if return_code is None:
                status = "PROCESS_TIMEOUT"
            result = {
                "session_id": session_id,
                "rubric": row["rubric"],
                "line_number": row["line_number"],
                "status": status,
                "exit_code": return_code,
                "timeout_seconds": session_timeout_seconds,
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
                "manifest": str(manifest_file_run) if result_manifest is not None else None,
                "stage_receipt": stage_receipt,
            }
            result_stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            result_stream.flush()
            results.append(result)
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    final = {
        "schema_version": BATCH_SCHEMA,
        "domain": domain,
        "status": (
            "COMPLETED"
            if all(item["status"] in {"READY", "READY_VARIANT"} for item in results)
            else "COMPLETED_WITH_ERRORS"
        ),
        "created_at": datetime.now(UTC).isoformat(),
        "inventory_manifest": str(manifest_file),
        "coverage_complete": True,
        "offset": offset,
        "limit": limit,
        "selected_sessions": len(selected),
        "completed_sessions": len(results),
        "status_counts": counts,
        "results": str(result_file),
        "output_root": str(root),
    }
    _write_json(root / "batch_manifest.json", final)
    return final


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--domain", choices=("search", "terminal"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--sandbox", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--execute-red", action="store_true")
    parser.add_argument("--execute-rollout", action="store_true")
    parser.add_argument("--rollout-trials", type=int, default=2)
    parser.add_argument("--session-timeout-seconds", type=int, default=7200,
                        help="整条 session 的总时限（秒），包含重建、RED 和 rollout")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    try:
        manifest, rows = load_inventory(args.manifest)
        if args.plan_only:
            output = args.output.resolve()
            if (output / "batch_plan.json").exists():
                raise FileExistsError(f"拒绝覆盖已有计划：{output / 'batch_plan.json'}")
            _write_json(
                output / "batch_plan.json",
                _plan_payload(
                    manifest_path=Path(args.manifest).resolve(),
                    manifest=manifest,
                    rows=rows,
                    offset=args.offset,
                    limit=args.limit,
                    output_root=output,
                    domain=args.domain,
                ),
            )
            print(output / "batch_plan.json")
            return 0
        if args.config is None:
            raise BatchInputError("--config 是实际执行必填；只看计划使用 --plan-only")
        result = execute_batch(
            manifest_path=args.manifest,
            output_root=args.output,
            config=args.config,
            domain=args.domain,
            offset=args.offset,
            limit=args.limit,
            workers=args.workers,
            sandbox=args.sandbox,
            execute_red=args.execute_red,
            execute_rollout=args.execute_rollout,
            rollout_trials=args.rollout_trials,
            session_timeout_seconds=args.session_timeout_seconds,
        )
    except (BatchInputError, FileExistsError, OSError) as exc:
        print(f"批处理失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
