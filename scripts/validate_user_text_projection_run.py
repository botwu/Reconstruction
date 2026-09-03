#!/usr/bin/env python3
"""独立验收任意 TraceForge `UserTextProjection` 产物。

取上游 M1B run（与 manifest 绑定了 M1D 时的 M1D run）作复算 oracle。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from traceforge.source_projection.validation import validate_user_text_projection_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立验收 TraceForge UserTextProjection 产物")
    parser.add_argument("projection_run_path", type=Path, help="内容寻址投影 run 目录")
    parser.add_argument("m1b_run_path", type=Path, help="投影绑定的上游 M1B run 目录（只读）")
    parser.add_argument(
        "--m1d-run",
        type=Path,
        default=None,
        help="投影绑定了 M1D 时必填：对应 M1D run 目录（只读）；未绑定时不得提供",
    )
    arguments = parser.parse_args(argv)
    result = validate_user_text_projection_run(
        arguments.projection_run_path, arguments.m1b_run_path, arguments.m1d_run
    )
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
