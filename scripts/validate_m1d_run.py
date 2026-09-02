#!/usr/bin/env python3
"""独立验收任意 TraceForge M1D QueryTurn 产物（取上游 M1B run 作完整回合图复算 oracle）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from traceforge.query_turns.validation import validate_query_turn_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立验收 TraceForge M1D QueryTurn 产物")
    parser.add_argument("query_turn_run_path", type=Path, help="内容寻址 M1D run 目录")
    parser.add_argument("m1b_run_path", type=Path, help="M1D 绑定的上游 M1B run 目录（只读）")
    arguments = parser.parse_args(argv)
    result = validate_query_turn_run(arguments.query_turn_run_path, arguments.m1b_run_path)
    if not result.ok:
        for error in result.errors:
            print(error, file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "checked_file_count": result.checked_file_count,
                "observed_counts": result.observed_counts,
                "ok": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
