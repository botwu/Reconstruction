#!/usr/bin/env python3
"""独立验收任意 TraceForge M1C lineage 产物（取上游 M1B run 作完整边集复算 oracle）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from traceforge.lineage.validation import validate_lineage_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立验收 TraceForge M1C lineage 产物")
    parser.add_argument("lineage_run_path", type=Path, help="内容寻址 lineage run 目录")
    parser.add_argument("m1b_run_path", type=Path, help="lineage 绑定的上游 M1B run 目录（只读）")
    arguments = parser.parse_args(argv)
    result = validate_lineage_run(arguments.lineage_run_path, arguments.m1b_run_path)
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
