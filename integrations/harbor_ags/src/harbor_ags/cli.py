"""TraceForge Harbor × AGS 集成命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return value.__dict__
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight", help="检查版本、凭据和可选真实连通性")
    preflight.add_argument("--probe-ags", action="store_true")
    preflight.add_argument("--output", type=Path)

    validate = subparsers.add_parser("validate-trial", help="生成 certification.json")
    validate.add_argument("trial_dir", type=Path)

    cleanup = subparsers.add_parser("audit-cleanup", help="审计 Job sandbox ledger")
    cleanup.add_argument("ledger", type=Path)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "preflight":
        from .preflight import check_local, probe_ags

        report = (
            asyncio.run(probe_ags(args.output))
            if args.probe_ags
            else check_local(require_credentials=True)
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.ok else 2

    if args.command == "validate-trial":
        from .validator import validate_harbor_trial

        result = validate_harbor_trial(args.trial_dir)
        payload = _jsonable(result)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if bool(payload.get("certified", False)) else 2

    if args.command == "audit-cleanup":
        from .environment import audit_sandbox_ledger

        audit = audit_sandbox_ledger(args.ledger)
        payload = {
            "ok": audit.ok,
            "created_ids": audit.created_ids,
            "terminal_ids": audit.terminal_ids,
            "unverified_ids": audit.unverified_ids,
            "unresolved_create_operations": audit.unresolved_create_operations,
            "malformed_lines": audit.malformed_lines,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if audit.ok else 2

    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
