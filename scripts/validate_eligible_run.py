"""对筛选出的 ELIGIBLE 行跑 reconstruct run，只写摘要，不打印密钥或文件正文。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from traceforge.reconstruction.agents import build_hermes_runtime
from traceforge.reconstruction.eligible_reconstruction import run_eligible_reconstruction
from traceforge.reconstruction.session_source import load_eligible_record, load_raw_line


def _row(dest: Path, payload: dict) -> dict:
    task = payload.get("task") or {}
    row = {
        "status": payload.get("status"),
        "stopped_at": payload.get("stopped_at"),
        "errors": payload.get("errors"),
        "objective": (task.get("core_objective") or "")[:180],
        "criteria": [str(item)[:80] for item in (task.get("success_criteria") or [])[:3]],
        "constraints": [str(item)[:80] for item in (task.get("mandatory_constraints") or [])[:3]],
        "prohibitions": [str(item)[:80] for item in (task.get("prohibitions") or [])[:3]],
        "workspace": bool(payload.get("workspace")),
        "selected": payload.get("selected_index"),
        "audit": (payload.get("sufficiency_audit") or {}).get("status"),
    }
    replay_path = dest / "replay.json"
    if replay_path.is_file():
        replay = json.loads(replay_path.read_text(encoding="utf-8"))
        row["replay_files"] = [item.get("path") for item in replay.get("files") or []]
    intent_path = dest / "intent" / "intent.json"
    if intent_path.is_file():
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        row["intent_status"] = intent.get("status")
        row["intent_errors"] = intent.get("errors")
    completion_path = dest / "completion" / "completion.json"
    if completion_path.is_file():
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        row["completion_status"] = completion.get("status")
        row["completion_errors"] = completion.get("errors")
        row["cand"] = [
            {
                "i": item.get("index"),
                "d": item.get("decision"),
                "valid": item.get("valid"),
                "err": item.get("errors"),
            }
            for item in (completion.get("candidates") or [])
        ]
    sufficiency_root = dest / "sufficiency"
    if sufficiency_root.is_dir():
        labels = []
        for path in sorted(sufficiency_root.glob("*/sufficiency.json")):
            judge = json.loads(path.read_text(encoding="utf-8"))
            labels.append(
                {
                    "label": judge.get("label"),
                    "decision": judge.get("decision"),
                    "conf": judge.get("confidence"),
                    "reason": str(judge.get("reason") or "")[:160],
                    "missing": judge.get("missing_context"),
                }
            )
        row["sufficiency"] = labels
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--channel", default="claude")
    parser.add_argument("--model-name", default="claude-opus-4-6")
    parser.add_argument("--lines", default="49,41,5,43,35")
    parser.add_argument("--hermes-home", type=Path, default=None)
    arguments = parser.parse_args()
    agent = build_hermes_runtime(
        config_path=arguments.config,
        channel=arguments.channel,
        model_name=arguments.model_name,
        hermes_home=arguments.hermes_home,
    )
    summary = []
    arguments.output.mkdir(parents=True, exist_ok=True)
    for raw in arguments.lines.split(","):
        line_number = int(raw.strip())
        print("START", line_number, flush=True)
        record = load_eligible_record(arguments.records, line_number=line_number)
        raw_line = load_raw_line(
            arguments.input,
            line_number=line_number,
            line_sha256=str(record.get("line_sha256") or "") or None,
        )
        dest = arguments.output / f"L{line_number:02d}"
        try:
            manifest = run_eligible_reconstruction(
                raw_line=raw_line,
                record=record,
                agent=agent,
                output_root=dest,
            )
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            row = {"line": line_number, **_row(dest, payload)}
        except Exception as exc:  # noqa: BLE001 — 验证脚本要记下失败原因
            row = {"line": line_number, "error": type(exc).__name__, "msg": str(exc)[:200]}
        summary.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    (arguments.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("WROTE", arguments.output / "summary.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
