#!/usr/bin/env python3
"""冻结指定原始数据，并生成不筛选、不去重的逐条输入清单。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from traceforge.reconstruction.session_inventory import prepare_session_batch


def main() -> int:
    """以可配置路径运行冻结盘点，覆盖不完整时返回非零。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="原始 JSONL 所在目录")
    parser.add_argument("--source-codes", nargs="+", required=True, help="源文件名，不含 .jsonl")
    parser.add_argument("--domain", choices=("search", "terminal"), required=True)
    parser.add_argument("--frozen-dir", type=Path, required=True, help="新的冻结输入目录")
    parser.add_argument("--output", type=Path, required=True, help="清单输出目录")
    parser.add_argument("--distribution", type=Path, help="覆盖声明文件；也支持 data/debug-datasets.json，默认使用源目录 distribution.json")
    args = parser.parse_args()
    result = prepare_session_batch(
        args.source_dir,
        args.frozen_dir,
        args.output,
        source_codes=args.source_codes, domain=args.domain,
        distribution_path=args.distribution,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["coverage_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
