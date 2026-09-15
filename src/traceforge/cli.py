"""TraceForge 命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from traceforge.failure_analysis.agentrx_pipeline import run_agentrx_diagnosis
from traceforge.failure_analysis.mapping_reader import MappingInputError
from traceforge.failure_analysis.model_runner import run_failure_analysis_model
from traceforge.failure_analysis.pipeline import build_failure_analysis
from traceforge.failure_analysis.reader import FailureAnalysisInputError
from traceforge.failure_analysis.review_batch import build_review_batch
from traceforge.failure_analysis.trace_capabilities import aggregate_capability_runs
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
from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    build_chat_model,
    resolve_model_name,
)
from traceforge.reconstruction.pipeline import (
    ReconstructionPipelineInputError,
    build_reconstruction_pipeline,
)
from traceforge.reconstruction.prepare import (
    ReconstructionPrepareError,
    build_reconstruction_inputs,
)
from traceforge.reconstruction.workflow import run_reconstruction_workflow
from traceforge.screening import ScreeningInputError, run_reconstruction_screening
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
    failure_model = failure_analysis_commands.add_parser(
        "model-judge", help="对单条结构化失败报告执行模型裁决并发布 artifact"
    )
    failure_model.add_argument("--report-json", type=Path, required=True)
    failure_model.add_argument("--evidence-json", type=Path, required=True)
    failure_model.add_argument("--output", type=Path, required=True)
    failure_model.add_argument("--model-name", default="claude-opus-4-8")
    failure_model.add_argument("--config", type=Path, default=None, help="NewAPI 配置文件（可选）")
    failure_model.add_argument("--channel", default="gemini", help="配置中的 channel 名")
    failure_batch = failure_analysis_commands.add_parser(
        "review-batch", help="按固定规则生成待人工复核批次"
    )
    failure_batch.add_argument("--input-jsonl", type=Path, required=True)
    failure_batch.add_argument("--output", type=Path, required=True)
    failure_batch.add_argument("--limit", type=int, default=50)
    failure_batch.add_argument("--batch-name", default="initial-manual-review")
    failure_agentrx = failure_analysis_commands.add_parser(
        "agentrx", help="运行 AgentRx 静态/动态不变量与根因诊断适配"
    )
    failure_agentrx.add_argument("--trajectory-json", type=Path, required=True)
    failure_agentrx.add_argument("--output", type=Path, required=True)
    failure_agentrx.add_argument("--model-name", default="claude-opus-4-8")
    failure_agentrx.add_argument(
        "--config", type=Path, default=None, help="NewAPI 配置文件（可选）"
    )
    failure_agentrx.add_argument("--channel", default="gemini", help="配置中的 channel 名")
    capability_aggregate = failure_analysis_commands.add_parser(
        "capabilities-aggregate", help="按 TRACE 双阈值聚合独立能力标注 runs"
    )
    capability_aggregate.add_argument("--runs-json", type=Path, required=True)
    capability_aggregate.add_argument("--outcomes-json", type=Path, required=True)
    capability_aggregate.add_argument("--output", type=Path, required=True)
    capability_aggregate.add_argument("--rho", type=float, default=0.10)
    capability_aggregate.add_argument("--delta", type=float, default=0.20)
    capability_aggregate.add_argument("--consistency-k", type=int, default=None)

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
    reconstruct_prepare = reconstruct_commands.add_parser(
        "prepare",
        help="在已发布 M1B run 上确定性产出 report.json + 带正文 evidence.json（不调用模型）",
    )
    reconstruct_prepare.add_argument(
        "--m1b-run", type=Path, required=True, help="已发布 M1B run 目录（只读消费）"
    )
    reconstruct_prepare.add_argument(
        "--m1d-run",
        type=Path,
        default=None,
        help="可选：已发布 M1D run（提供即绑定 attempt_ref/episode_ref）",
    )
    reconstruct_prepare.add_argument(
        "--capture-id",
        default=None,
        help="可选：run 含多个 capture 时唯一指定 capture occurrence id",
    )
    # prepare 始终聚合完整 candidate session；request boundary 仅作为来源元数据保留。
    reconstruct_prepare.add_argument(
        "--output", type=Path, required=True, help="prepare artifact 输出目录"
    )
    reconstruct_workflow = reconstruct_commands.add_parser(
        "workflow", help="运行单条失败轨迹的任务、环境、验证器、rollout 与 SFT 闭环"
    )
    reconstruct_workflow.add_argument("--attempt-ref", required=True)
    reconstruct_workflow.add_argument("--source-report-id", required=True)
    reconstruct_workflow.add_argument("--report-json", type=Path, required=True)
    reconstruct_workflow.add_argument("--evidence-json", type=Path, required=True)
    # 回放输入二选一：推荐 --normalized-run（内部 build_trajectory_replay 直接消费确定性回放产物），
    # 或手工提供 --replay-workspace + --replay-files-json。两条路径互斥。
    replay_source = reconstruct_workflow.add_mutually_exclusive_group(required=True)
    replay_source.add_argument(
        "--normalized-run",
        type=Path,
        default=None,
        help="已发布规范化 trajectory run；内部自动回放出任务开始前的初始 workspace（推荐）",
    )
    replay_source.add_argument(
        "--replay-workspace",
        type=Path,
        default=None,
        help="手工路径：任务开始前的公开 workspace 目录（须配合 --replay-files-json）",
    )
    reconstruct_workflow.add_argument(
        "--replay-files-json",
        type=Path,
        default=None,
        help="手工路径：replay 文件索引 JSON（与 --replay-workspace 搭配）",
    )
    reconstruct_workflow.add_argument(
        "--capture-id",
        default=None,
        help="可选：--normalized-run 含多个 capture 时唯一指定 capture occurrence id",
    )
    reconstruct_workflow.add_argument("--harbor-root", type=Path, required=True)
    reconstruct_workflow.add_argument("--output", type=Path, required=True)
    reconstruct_workflow.add_argument("--model-name", default="claude-opus-4-8")
    reconstruct_workflow.add_argument(
        "--rollout-model",
        default=None,
        help=("Harbor agent 的 provider/model；非 Claude 模型必须显式指定，"
              "例如 vol/deepseek-v4-flash-0731"),
    )
    reconstruct_workflow.add_argument(
        "--config", type=Path, default=None, help="NewAPI 配置文件（可选）"
    )
    reconstruct_workflow.add_argument("--channel", default="gemini", help="配置中的 channel 名")
    reconstruct_workflow.add_argument("--rollout-trials", type=int, default=1)
    reconstruct_workflow.add_argument(
        "--execute-rollout", action="store_true", help="显式执行 Harbor/AGS 与 RED-check"
    )

    screening = commands.add_parser(
        "screening", help="重建筛选：对原始 session 做规则分流，不编译轨迹"
    )
    screening_commands = screening.add_subparsers(dest="screening_command", required=True)
    screening_run = screening_commands.add_parser(
        "run", help="扫描 JSONL 并发布 SelectionManifest"
    )
    screening_run.add_argument("--input", type=Path, required=True, help="原始 session JSONL")
    screening_run.add_argument("--output", type=Path, required=True, help="筛选 artifact 根目录")
    screening_run.add_argument("--limit", type=int, default=None, help="最多处理的记录数")
    screening_run.add_argument("--offset", type=int, default=0, help="跳过的前部记录数")
    screening_run.add_argument(
        "--rules-only",
        action="store_true",
        help="只跑规则粗筛，不调用模型；无法产生 ELIGIBLE",
    )
    screening_run.add_argument("--model-name", default="claude-opus-4-8")
    screening_run.add_argument(
        "--config", type=Path, default=None, help="NewAPI 配置文件（可选）"
    )
    screening_run.add_argument("--channel", default="gemini", help="配置中的 channel 名")

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
            failure_kwargs = {
                "m1b_run_dir": arguments.m1b_run,
                "output_root": arguments.output,
            }
            if arguments.m1d_run is not None:
                failure_kwargs["m1d_run_dir"] = arguments.m1d_run
            output_path = build_failure_analysis(**failure_kwargs)
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
    if (
        arguments.command == "failure-analysis"
        and arguments.failure_analysis_command == "model-judge"
    ):
        try:
            report = json.loads(arguments.report_json.read_text(encoding="utf-8"))
            evidence = json.loads(arguments.evidence_json.read_text(encoding="utf-8"))
            output_path = run_failure_analysis_model(
                report=report,
                evidence=evidence,
                model=build_chat_model(config_path=arguments.config, channel=arguments.channel),
                output_root=arguments.output,
                model_name=resolve_model_name(
                    arguments.model_name,
                    config_path=arguments.config,
                    channel=arguments.channel,
                ),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
            print(f"模型失败分析失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if (
        arguments.command == "failure-analysis"
        and arguments.failure_analysis_command == "review-batch"
    ):
        try:
            output_path = build_review_batch(
                input_jsonl=arguments.input_jsonl,
                output_root=arguments.output,
                limit=arguments.limit,
                batch_name=arguments.batch_name,
            )
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            print(f"人工复核批次构建失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "failure-analysis" and arguments.failure_analysis_command == "agentrx":
        try:
            trajectory = json.loads(arguments.trajectory_json.read_text(encoding="utf-8"))
            report = run_agentrx_diagnosis(
                trajectory,
                build_chat_model(config_path=arguments.config, channel=arguments.channel),
                model_name=resolve_model_name(
                    arguments.model_name,
                    config_path=arguments.config,
                    channel=arguments.channel,
                ),
            )
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
            print(f"AgentRx 轨迹诊断失败：{exc}", file=sys.stderr)
            return 2
        print(arguments.output)
        return 0
    if (
        arguments.command == "failure-analysis"
        and arguments.failure_analysis_command == "capabilities-aggregate"
    ):
        try:
            runs = json.loads(arguments.runs_json.read_text(encoding="utf-8"))
            outcomes = json.loads(arguments.outcomes_json.read_text(encoding="utf-8"))
            result = aggregate_capability_runs(
                runs,
                outcomes,
                rho=arguments.rho,
                delta=arguments.delta,
                consistency_k=arguments.consistency_k,
            )
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
            print(f"TRACE 能力聚合失败：{exc}", file=sys.stderr)
            return 2
        print(arguments.output)
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
    if arguments.command == "reconstruct" and arguments.reconstruct_command == "prepare":
        try:
            prepared = build_reconstruction_inputs(
                m1b_run_dir=arguments.m1b_run,
                output_dir=arguments.output,
                capture_id=arguments.capture_id,
                m1d_run_dir=arguments.m1d_run,
            )
        except (
            ReconstructionPrepareError,
            FailureAnalysisInputError,
            MappingInputError,
            ArtifactPublishError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            print(f"重建输入准备失败：{exc}", file=sys.stderr)
            return 2
        print(prepared.manifest_path)
        return 0
    if arguments.command == "reconstruct" and arguments.reconstruct_command == "workflow":
        try:
            report = json.loads(arguments.report_json.read_text(encoding="utf-8"))
            evidence = json.loads(arguments.evidence_json.read_text(encoding="utf-8"))
            if not isinstance(report, dict) or not isinstance(evidence, list):
                raise ValueError("report 必须是对象，evidence 必须是数组")
            replay_workspace = None
            replay_files = None
            normalized_run_dir = None
            if arguments.normalized_run is not None:
                normalized_run_dir = arguments.normalized_run
            else:
                if arguments.replay_files_json is None:
                    raise ValueError("--replay-workspace 必须配合 --replay-files-json 一起提供")
                replay_files = json.loads(arguments.replay_files_json.read_text(encoding="utf-8"))
                if not isinstance(replay_files, list):
                    raise ValueError("replay-files 必须是数组")
                replay_workspace = arguments.replay_workspace
            output_path = run_reconstruction_workflow(
                attempt_ref=arguments.attempt_ref,
                source_report_id=arguments.source_report_id,
                report=report,
                evidence=evidence,
                replay_workspace=replay_workspace,
                replay_files=replay_files,
                normalized_run_dir=normalized_run_dir,
                capture_id=arguments.capture_id,
                model=build_chat_model(config_path=arguments.config, channel=arguments.channel),
                output_root=arguments.output,
                harbor_root=arguments.harbor_root,
                execute_rollout=arguments.execute_rollout,
                model_name=resolve_model_name(
                    arguments.model_name,
                    config_path=arguments.config,
                    channel=arguments.channel,
                ),
                rollout_model=arguments.rollout_model,
                rollout_trials=arguments.rollout_trials,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
            print(f"重建闭环执行失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "screening" and arguments.screening_command == "run":
        try:
            model = None
            model_name = arguments.model_name
            if not arguments.rules_only:
                model = build_chat_model(
                    config_path=arguments.config, channel=arguments.channel
                )
                model_name = resolve_model_name(
                    arguments.model_name,
                    config_path=arguments.config,
                    channel=arguments.channel,
                )
            output_path = run_reconstruction_screening(
                input_path=arguments.input,
                output_root=arguments.output,
                limit=arguments.limit,
                offset=arguments.offset,
                model=model,
                model_name=model_name,
            )
        except (
            ScreeningInputError,
            ArtifactPublishError,
            ModelGatewayError,
            OSError,
            UnicodeError,
            ValueError,
        ) as exc:
            print(f"重建筛选失败：{exc}", file=sys.stderr)
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
