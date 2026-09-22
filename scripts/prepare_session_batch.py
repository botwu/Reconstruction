#!/usr/bin/env python3
"""冻结完整 R04/R05 数据，并生成不筛选、不去重的逐条输入清单。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from traceforge.reconstruction.session_inventory import prepare_session_batch


def main() -> int:
    """以可配置路径运行冻结盘点，覆盖不完整时返回非零。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="完整 R04/R05 的目录")
    parser.add_argument("--frozen-dir", type=Path, required=True, help="新的冻结输入目录")
    parser.add_argument("--output", type=Path, required=True, help="清单输出目录")
    parser.add_argument("--distribution", type=Path, help="默认读取源目录的 distribution.json")
    args = parser.parse_args()
    result = prepare_session_batch(
        args.source_dir,
        args.frozen_dir,
        args.output,
        distribution_path=args.distribution,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["coverage_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
