"""TraceForge 命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from traceforge.failure_analysis.mapping_reader import MappingInputError
from traceforge.failure_analysis.pipeline import build_failure_analysis
from traceforge.failure_analysis.reader import FailureAnalysisInputError
from traceforge.harbor_ags.adapter import HarborAgsAdapterError, build_boundary_plan
from traceforge.harbor_ags.results import HarborResultError, read_rollout_results
from traceforge.harbor_ags.rollout import (
    HarborRolloutConfig,
    HarborRolloutError,
    build_rollout_plan,
    execute_rollout_plan,
)
from traceforge.lineage.pipeline import build_lineage
from traceforge.lineage.reader import LineageInputError
from traceforge.query_turns.pipeline import build_query_turns
from traceforge.query_turns.reader import QueryTurnInputError
from traceforge.reconstruction.pipeline import (
    ReconstructionPipelineInputError,
    build_reconstruction_pipeline,
)
from traceforge.source_projection.contracts import UserTextProjectionInputError
from traceforge.source_projection.pipeline import build_user_text_projection
from traceforge.trajectory.artifacts import ArtifactPublishError
from traceforge.trajectory.pipeline import compile_trajectory
from traceforge.trajectory.source import SourceError
from traceforge.trajectory.source_adapter import SUPPORTED_SOURCE_SCHEMAS
from traceforge.trajectory_replay.pipeline import ReplayInputError, build_trajectory_replay


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
        help=f"显式来源契约；当前支持 {', '.join(sorted(SUPPORTED_SOURCE_SCHEMAS))}",
    )
    compile_parser.add_argument(
        "--expected-sha256",
        default=None,
        help="可选的冻结输入 SHA-256",
    )
    compile_parser.add_argument("--output", type=Path, required=True, help="artifact 根目录")

    failure_analysis = commands.add_parser(
        "failure-analysis", help="deterministic failure evidence analysis (M4)"
    )
    failure_analysis_commands = failure_analysis.add_subparsers(
        dest="failure_analysis_command", required=True
    )
    failure_analysis_build = failure_analysis_commands.add_parser(
        "build", help="build failure-analysis artifacts from a published M1B run"
    )
    failure_analysis_build.add_argument(
        "--m1b-run", type=Path, required=True, help="published M1B run (read only)"
    )
    failure_analysis_build.add_argument(
        "--m1d-run", type=Path, default=None, help="可选：已发布 M1D QueryTurn run"
    )
    failure_analysis_build.add_argument(
        "--output", type=Path, required=True, help="failure-analysis artifact root"
    )

    harbor_ags = commands.add_parser(
        "harbor-ags", help="Harbor/AGS Task Bundle 边界适配（不启动 rollout）"
    )
    harbor_ags_commands = harbor_ags.add_subparsers(dest="harbor_ags_command", required=True)
    harbor_ags_plan = harbor_ags_commands.add_parser(
        "plan", help="生成 public workspace/hidden control 执行计划"
    )
    harbor_ags_plan.add_argument(
        "--task-dir", type=Path, required=True, help="Harbor Task Bundle 目录"
    )
    harbor_ags_plan.add_argument("--output", type=Path, required=True, help="artifact 根目录")
    harbor_ags_plan.add_argument(
        "--harbor-root", type=Path, default=None, help="可选 harbor_ags 项目根目录"
    )
    harbor_ags_plan.add_argument(
        "--source-ref", action="append", default=[], help="来源 artifact 引用，可重复"
    )
    prepare_rollout = harbor_ags_commands.add_parser(
        "prepare-rollout", help="物化 Harbor Dataset 并生成显式 dry-run 计划"
    )
    prepare_rollout.add_argument("--task-dir", type=Path, required=True)
    prepare_rollout.add_argument("--harbor-root", type=Path, required=True)
    prepare_rollout.add_argument("--output", type=Path, required=True)
    prepare_rollout.add_argument("--jobs-root", type=Path, required=True)
    prepare_rollout.add_argument(
        "--agent-mode", choices=("hermes", "oracle", "nop"), default="hermes"
    )
    prepare_rollout.add_argument("--model", default="anthropic/claude-opus-4-8")
    prepare_rollout.add_argument("--trials", type=int, default=1)
    prepare_rollout.add_argument("--concurrency", type=int, default=1)
    prepare_rollout.add_argument("--timeout-seconds", type=int, default=900)
    prepare_rollout.add_argument("--expected-hermes-commit")
    execute_rollout = harbor_ags_commands.add_parser(
        "execute-rollout", help="显式执行已审核的 rollout plan"
    )
    execute_rollout.add_argument("--plan-dir", type=Path, required=True)
    execute_rollout.add_argument("--timeout-seconds", type=int, default=900)
    read_results = harbor_ags_commands.add_parser(
        "read-results", help="读取 Harbor Job 结果并计算 rollout 指标"
    )
    read_results.add_argument("--job-dir", type=Path, required=True)
    read_results.add_argument("--agent-mode", choices=("hermes", "oracle", "nop"), default="hermes")

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

    query_turns = commands.add_parser("query-turns", help="结构型 QueryTurn 回合图（M1D）")
    query_turns_commands = query_turns.add_subparsers(
        dest="query_turns_command",
        required=True,
    )
    query_turns_build = query_turns_commands.add_parser(
        "build",
        help="在一个已发布 M1B run 之上派生只读回合图产物",
    )
    query_turns_build.add_argument(
        "--m1b-run",
        type=Path,
        required=True,
        help="已发布 M1B run 目录（只读消费）",
    )
    query_turns_build.add_argument(
        "--output",
        type=Path,
        required=True,
        help="query-turns artifact 根目录",
    )

    reconstruct = commands.add_parser(
        "reconstruct", help="重建流程编排（模型阶段仅生成 pending refs）"
    )
    reconstruct_commands = reconstruct.add_subparsers(dest="reconstruct_command", required=True)
    reconstruct_pipeline = reconstruct_commands.add_parser(
        "pipeline", help="运行 M1B/M1D → M4 并生成执行计划"
    )
    reconstruct_pipeline.add_argument("--m1b-run", type=Path, required=True, help="已发布 M1B run")
    reconstruct_pipeline.add_argument(
        "--m1d-run", type=Path, default=None, help="可选：已发布 M1D run"
    )
    reconstruct_pipeline.add_argument(
        "--output", type=Path, required=True, help="pipeline artifact 根目录"
    )

    replay = commands.add_parser(
        "trajectory-replay", help="恢复任务开始前的初始 workspace（不执行历史命令）"
    )
    replay.add_argument("--normalized-run", type=Path, required=True, help="规范化 trajectory run")
    replay.add_argument("--capture-id", default=None, help="可选 capture occurrence id")
    replay.add_argument(
        "--source-workspace-root",
        type=Path,
        default=None,
        help="绝对路径映射的原始 workspace 根目录",
    )
    replay.add_argument("--output", type=Path, required=True, help="replay artifact 根目录")

    source_projection = commands.add_parser(
        "source-projection", help="来源投影：USER 事件结构注解（M2 前置）"
    )
    source_projection_commands = source_projection.add_subparsers(
        dest="source_projection_command",
        required=True,
    )
    source_projection_build = source_projection_commands.add_parser(
        "build",
        help="在一个已发布 M1B run（可选绑定 M1D run）之上派生只读 USER 文本结构注解",
    )
    source_projection_build.add_argument(
        "--m1b-run",
        type=Path,
        required=True,
        help="已发布 M1B run 目录（只读消费）",
    )
    source_projection_build.add_argument(
        "--m1d-run",
        type=Path,
        default=None,
        help="可选：已发布 M1D run 目录（只读消费；提供即绑定并回指 UserBlock）",
    )
    source_projection_build.add_argument(
        "--output",
        type=Path,
        required=True,
        help="source-projection artifact 根目录",
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
    if arguments.command == "failure-analysis" and arguments.failure_analysis_command == "build":
        try:
            output_path = build_failure_analysis(
                m1b_run_dir=arguments.m1b_run,
                output_root=arguments.output,
                m1d_run_dir=arguments.m1d_run,
            )
        except (
            FailureAnalysisInputError,
            MappingInputError,
            ArtifactPublishError,
            ValueError,
        ) as exc:
            print(f"失败分析构建失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "harbor-ags" and arguments.harbor_ags_command == "plan":
        try:
            output_path = build_boundary_plan(
                arguments.task_dir,
                output_root=arguments.output,
                harbor_root=arguments.harbor_root,
                source_refs=arguments.source_ref,
            )
        except (HarborAgsAdapterError, ValueError, OSError) as exc:
            print(f"Harbor/AGS 计划构建失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "harbor-ags" and arguments.harbor_ags_command == "prepare-rollout":
        try:
            output_path = build_rollout_plan(
                HarborRolloutConfig(
                    task_dir=arguments.task_dir,
                    harbor_root=arguments.harbor_root,
                    output_root=arguments.output,
                    jobs_root=arguments.jobs_root,
                    agent_mode=arguments.agent_mode,
                    model=arguments.model,
                    trials=arguments.trials,
                    concurrency=arguments.concurrency,
                    timeout_seconds=arguments.timeout_seconds,
                    expected_hermes_commit=arguments.expected_hermes_commit,
                )
            )
        except (HarborAgsAdapterError, HarborRolloutError, ArtifactPublishError, OSError) as exc:
            print(f"Harbor/AGS rollout 计划构建失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "harbor-ags" and arguments.harbor_ags_command == "execute-rollout":
        try:
            result = execute_rollout_plan(
                arguments.plan_dir, timeout_seconds=arguments.timeout_seconds
            )
        except (HarborRolloutError, OSError) as exc:
            print(f"Harbor/AGS rollout 执行失败：{exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "COMPLETED" else 2
    if arguments.command == "harbor-ags" and arguments.harbor_ags_command == "read-results":
        try:
            result = read_rollout_results(arguments.job_dir, agent_mode=arguments.agent_mode)
        except HarborResultError as exc:
            print(f"Harbor/AGS 结果读取失败：{exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["quality_gate"]["ok"] else 2
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
    if arguments.command == "query-turns" and arguments.query_turns_command == "build":
        try:
            output_path = build_query_turns(
                m1b_run_dir=arguments.m1b_run,
                output_root=arguments.output,
            )
        except (QueryTurnInputError, ArtifactPublishError, ValueError) as exc:
            print(f"回合图构建失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "reconstruct" and arguments.reconstruct_command == "pipeline":
        try:
            output_path = build_reconstruction_pipeline(
                m1b_run_dir=arguments.m1b_run,
                m1d_run_dir=arguments.m1d_run,
                output_root=arguments.output,
            )
        except (
            ReconstructionPipelineInputError,
            FailureAnalysisInputError,
            MappingInputError,
            ArtifactPublishError,
            ValueError,
        ) as exc:
            print(f"重建流程编排失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "trajectory-replay":
        try:
            output_path = build_trajectory_replay(
                normalized_run_dir=arguments.normalized_run,
                capture_id=arguments.capture_id,
                source_workspace_root=arguments.source_workspace_root,
                output_root=arguments.output,
            )
        except (ReplayInputError, ArtifactPublishError, ValueError, OSError) as exc:
            print(f"轨迹回放失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "source-projection" and arguments.source_projection_command == "build":
        try:
            output_path = build_user_text_projection(
                m1b_run_dir=arguments.m1b_run,
                output_root=arguments.output,
                m1d_run_dir=arguments.m1d_run,
            )
        except (UserTextProjectionInputError, ArtifactPublishError, ValueError) as exc:
            print(f"来源投影构建失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    parser.error("不支持的命令")
    return 2
