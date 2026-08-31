#!/usr/bin/env python3
"""验收任意 TraceForge M1A、M1B 编译产物。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from traceforge.trajectory.validation import validate_compiled_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="流式验收 TraceForge M1 编译产物")
    parser.add_argument("run_path", type=Path, help="内容寻址 run 目录")
    arguments = parser.parse_args(argv)
    result = validate_compiled_run(arguments.run_path)
    if not result.ok:
        for error in result.errors:
            print(error, file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "checked_file_count": result.checked_file_count,
                "event_occurrence_count": result.observed_counts.get(
                    "event_occurrence_count",
                    0,
                ),
                "ok": True,
                "physical_line_count": result.observed_counts.get(
                    "physical_line_count",
                    0,
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
