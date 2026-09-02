"""TraceForge 命令行入口。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from traceforge.lineage.pipeline import build_lineage
from traceforge.lineage.reader import LineageInputError
from traceforge.trajectory.artifacts import ArtifactPublishError
from traceforge.trajectory.pipeline import compile_trajectory
from traceforge.trajectory.source import SourceError
from traceforge.trajectory.source_adapter import RESTORED_LONG_CAPTURE_SCHEMA


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traceforge",
        description="将真实回流轨迹编译成可审计的结构化事实",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    trajectory = commands.add_parser("trajectory", help="轨迹来源与结构编译")
    trajectory_commands = trajectory.add_subparsers(
        dest="trajectory_command",
        required=True,
    )
    compile_parser = trajectory_commands.add_parser(
        "compile",
        help="编译冻结的 JSONL 回流来源",
    )
    compile_parser.add_argument("--input", type=Path, required=True, help="JSONL 输入路径")
    compile_parser.add_argument("--dataset-id", required=True, help="稳定的数据集标识")
    compile_parser.add_argument(
        "--source-schema",
        required=True,
        help=f"显式来源契约；当前支持 {RESTORED_LONG_CAPTURE_SCHEMA}",
    )
    compile_parser.add_argument(
        "--expected-sha256",
        default=None,
        help="可选的冻结输入 SHA-256",
    )
    compile_parser.add_argument("--output", type=Path, required=True, help="artifact 根目录")

    lineage = commands.add_parser("lineage", help="跨 capture 关系图（M1C）")
    lineage_commands = lineage.add_subparsers(
        dest="lineage_command",
        required=True,
    )
    build_parser = lineage_commands.add_parser(
        "build",
        help="在一个已发布 M1B run 之上派生只读关系图产物",
    )
    build_parser.add_argument(
        "--m1b-run",
        type=Path,
        required=True,
        help="已发布 M1B run 目录（只读消费）",
    )
    build_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="lineage artifact 根目录",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "trajectory" and arguments.trajectory_command == "compile":
        try:
            output_path = compile_trajectory(
                input_path=arguments.input,
                dataset_id=arguments.dataset_id,
                source_schema=arguments.source_schema,
                expected_sha256=arguments.expected_sha256,
                output_root=arguments.output,
            )
        except (SourceError, ArtifactPublishError, ValueError) as exc:
            print(f"轨迹编译失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "lineage" and arguments.lineage_command == "build":
        try:
            output_path = build_lineage(
                m1b_run_dir=arguments.m1b_run,
                output_root=arguments.output,
            )
        except (LineageInputError, ArtifactPublishError, ValueError) as exc:
            print(f"关系图构建失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    parser.error("不支持的命令")
    return 2
