"""TraceForge 命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from traceforge.failure_analysis.agentrx_pipeline import run_agentrx_diagnosis
from traceforge.failure_analysis.model_runner import run_failure_analysis_model
from traceforge.failure_analysis.review_batch import build_review_batch
from traceforge.failure_analysis.trace_capabilities import aggregate_capability_runs
from traceforge.harbor_ags.acceptance import read_rollout_acceptance
from traceforge.harbor_ags.adapter import HarborAgsAdapterError, build_boundary_plan
from traceforge.harbor_ags.results import HarborResultError, read_rollout_results
from traceforge.harbor_ags.rollout import (
    DEFAULT_RUNTIME_CONFIG,
    HarborRolloutConfig,
    HarborRolloutError,
    build_rollout_plan,
    execute_rollout_plan,
)
from traceforge.reconstruction.agents import HermesUnavailableError, build_hermes_runtime
from traceforge.reconstruction.agents.runtime import resolve_rollout_model
from traceforge.reconstruction.container_verification import (
    SandboxUnavailableError,
    build_ags_runtime_factory,
)
from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    build_chat_model,
    resolve_model_name,
)
from traceforge.reconstruction.pipeline import (
    ReconstructionError,
    run_raw_session_reconstruction,
)
from traceforge.reconstruction.run_config import (
    load_rollout_limits,
    resolve_role_matrix,
)
from traceforge.reconstruction.session_source import (
    ReconstructionSourceError,
    load_raw_line,
)
from traceforge.reconstruction.tls import pin_process_tls
from traceforge.reconstruction.verification import VerificationConfig
from traceforge.requery.cross_workspace import (
    build_cross_workspace_prompt,
    profile_workspace,
    retrieve_directional_pairs,
)
from traceforge.requery.multi_round import (
    RequirementTracker,
    RoundResult,
    build_followup_prompt,
    retain_verified_session,
)
from traceforge.requery.single_workspace import (
    SingleWorkspaceSynthesisError,
    synthesize_single_workspace_tasks,
)
from traceforge.trajectory.artifacts import ArtifactPublishError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traceforge",
        description="从原始 session 重建可验证任务与环境",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    failure_analysis = commands.add_parser(
        "failure-analysis", help="deterministic failure evidence analysis (M4)"
    )
    failure_analysis_commands = failure_analysis.add_subparsers(
        dest="failure_analysis_command", required=True
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
    prepare_rollout.add_argument(
        "--config", type=Path, default=DEFAULT_RUNTIME_CONFIG,
        help="读取 config.yaml 中 rollout 角色的执行预算",
    )
    prepare_rollout.add_argument("--timeout-seconds", type=int, default=None)
    prepare_rollout.add_argument("--max-iterations", type=int, default=None)
    prepare_rollout.add_argument("--expected-hermes-commit")
    execute_rollout = harbor_ags_commands.add_parser(
        "execute-rollout", help="显式执行已审核的 rollout plan"
    )
    execute_rollout.add_argument("--plan-dir", type=Path, required=True)
    execute_rollout.add_argument("--timeout-seconds", type=int, default=None)
    execute_rollout.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_RUNTIME_CONFIG,
        help="读取 e2bapikey 与 TokenHub channel 的 config.yaml",
    )
    execute_rollout.add_argument(
        "--channel", default="claude", help="config.yaml 中用于 Hermes 的 channel"
    )
    read_results = harbor_ags_commands.add_parser(
        "read-results", help="读取 Harbor Job 结果并验收独立 rollout"
    )
    read_results.add_argument("--job-dir", type=Path)
    read_results.add_argument("--plan-dir", type=Path, help="Hermes 验收必需；绑定输入、执行状态和响应合同")
    read_results.add_argument("--agent-mode", choices=("hermes", "oracle", "nop"))

    reconstruct = commands.add_parser(
        "reconstruct", help="从原始 session 重建（默认入口：raw-run）"
    )
    reconstruct_commands = reconstruct.add_subparsers(dest="reconstruct_command", required=True)
    raw_run = reconstruct_commands.add_parser(
        "raw-run",
        help="按物理原始 session 逐条解析并重建",
    )
    raw_run.add_argument("--input", type=Path, required=True, help="冻结的原始 session JSONL")
    raw_run.add_argument("--domain", choices=("search", "terminal"), required=True,
                         help="数据已知的 domain，由调用方指定，不由模型推断")
    raw_run.add_argument("--line-number", type=int, required=True)
    raw_run.add_argument("--line-sha256", default=None, help="冻结清单中的行 SHA256")
    raw_run.add_argument("--source-ref", default=None, help="冻结清单 source_ref")
    raw_run.add_argument("--output", type=Path, required=True)
    raw_run.add_argument("--model-name", default=None)
    raw_run.add_argument("--config", type=Path, required=True)
    raw_run.add_argument("--channel", default=None)
    raw_run.add_argument("--verifier-model", default=None)
    raw_run.add_argument("--verifier-channel", default=None)
    raw_run.add_argument("--hermes-home", type=Path, default=None)
    raw_run.add_argument(
        "--harbor-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "integrations/harbor_ags",
    )
    raw_run.add_argument("--sandbox", action=argparse.BooleanOptionalAction, default=True,
                         help="默认在 AGS 执行连续研究者；--no-sandbox 仅用于离线调试")
    raw_run.add_argument("--execute-red", action="store_true")
    raw_run.add_argument("--execute-rollout", action="store_true")
    raw_run.add_argument("--rollout-model", default=None)
    raw_run.add_argument("--rollout-channel", default=None)
    raw_run.add_argument("--rollout-trials", type=int, default=2)
    raw_run.add_argument("--disable-verification", action="store_true",
                         help=("交付任务环境并可执行未评分 rollout；"
                               "不生成评分器，不声明验收或 SFT 通过"))
    raw_run.add_argument("--manual-response-review", action="store_true",
                         help="文件验证通过后采集 rollout；响应内容保留人工核查，不标为完整验收或 SFT")
    raw_run.add_argument("--rollout-timeout-seconds", type=int, default=None)
    raw_run.add_argument("--rollout-max-iterations", type=int, default=None)
    raw_run.add_argument("--verifier-rounds", type=int, default=None,
                         help="可选正整数轮数预算；默认持续返修至通过、无进展或执行阻塞")


    retry_review = reconstruct_commands.add_parser(
        "retry-review", help="只重试已完成 native 任务的后审，原运行保持不变")
    retry_review.add_argument("--from-manifest", type=Path, required=True)
    retry_review.add_argument("--task-id", required=True)
    retry_review.add_argument("--output", type=Path, required=True)
    retry_review.add_argument(
        "--check-only", action="store_true", help="仅离线认证输入，不调用模型或 AGS")
    retry_review.add_argument("--config", type=Path)
    retry_review.add_argument("--hermes-home", type=Path)
    retry_review.add_argument(
        "--harbor-root", type=Path,
        default=Path(__file__).resolve().parents[2] / "integrations/harbor_ags")

    requery = commands.add_parser("requery", help="Terminal-Universe C.2/C.3/C.4 任务扩展")
    requery_commands = requery.add_subparsers(dest="requery_command", required=True)
    single_ws = requery_commands.add_parser(
        "single-ws", help="在一个重建 workspace 上生成五个候选并选择一个"
    )
    single_ws.add_argument("--workspace", type=Path, required=True)
    single_ws.add_argument("--output", type=Path, required=True)
    single_ws.add_argument("--model-name", default="claude-opus-4-8")
    single_ws.add_argument("--config", type=Path, default=None)
    single_ws.add_argument("--channel", default="gemini")
    single_ws.add_argument("--selection-seed", default="0")
    cross_ws = requery_commands.add_parser(
        "cross-ws", help="跨 workspace 能力缺口配对（C.3）"
    )
    cross_ws.add_argument(
        "--workspaces-json",
        type=Path,
        required=True,
        help='[{"id":"...","path":"..."}] 工作区清单',
    )
    cross_ws.add_argument("--output", type=Path, required=True)
    cross_ws.add_argument("--min-overlap", type=int, default=2)
    multi_round = requery_commands.add_parser(
        "multi-round", help="多轮需求账本与验证反馈（C.4）"
    )
    multi_round.add_argument(
        "--rounds-json",
        type=Path,
        required=True,
        help="含 requirements 与 rounds 的 JSON",
    )
    multi_round.add_argument("--output", type=Path, required=True)
    multi_round.add_argument("--minimum-passes", type=int, default=2)

    return parser


def _reconstruction_exit_code(output_path: Path) -> int:
    manifest = json.loads(output_path.read_text(encoding="utf-8"))
    print(output_path)
    if manifest.get("status") in {
        "READY", "READY_VARIANT", "COMPLETED", "ENVIRONMENT_READY", "ROLLOUT_COMPLETED",
    }:
        return 0
    print(f"重建未完成：{manifest.get('status')}；阶段：{manifest.get('stopped_at') or '见任务回执'}",
          file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    pin_process_tls()
    parser = _parser()
    arguments = parser.parse_args(argv)
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
            timeout_seconds, max_iterations = load_rollout_limits(
                arguments.config,
                timeout_seconds=arguments.timeout_seconds,
                max_iterations=arguments.max_iterations,
            )
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
                    timeout_seconds=timeout_seconds,
                    agent_max_iterations=max_iterations,
                    expected_hermes_commit=arguments.expected_hermes_commit,
                )
            )
        except (HarborAgsAdapterError, HarborRolloutError, ArtifactPublishError, ModelGatewayError, OSError) as exc:
            print(f"Harbor/AGS rollout 计划构建失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "harbor-ags" and arguments.harbor_ags_command == "execute-rollout":
        try:
            result = execute_rollout_plan(
                arguments.plan_dir,
                timeout_seconds=arguments.timeout_seconds,
                config_path=arguments.config,
                channel=arguments.channel,
            )
        except (HarborRolloutError, OSError) as exc:
            print(f"Harbor/AGS rollout 执行失败：{exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "COMPLETED" else 2
    if arguments.command == "harbor-ags" and arguments.harbor_ags_command == "read-results":
        try:
            if arguments.plan_dir is not None:
                result = read_rollout_acceptance(
                    arguments.plan_dir, job_dir=arguments.job_dir, agent_mode=arguments.agent_mode,
                )
            elif arguments.agent_mode in {"oracle", "nop"} and arguments.job_dir is not None:
                result = read_rollout_results(arguments.job_dir, agent_mode=arguments.agent_mode)
            else:
                raise HarborResultError("Hermes 完整验收需要 --plan-dir；oracle/nop 指标读取需要 --job-dir")
        except (HarborResultError, HarborRolloutError, OSError) as exc:
            print(f"Harbor/AGS 结果读取失败：{exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        acceptance = result.get("acceptance")
        passed = (
            acceptance["status"] == "PASS"
            or (acceptance["status"] == "NOT_ASSESSED"
                and result.get("execution_completed") is True and not acceptance["errors"])
        ) if acceptance is not None else result["quality_gate"]["ok"]
        return 0 if passed else 2

    if arguments.command == "reconstruct" and arguments.reconstruct_command == "retry-review":
        from traceforge.reconstruction.review_retry import prepare_review_retry, run_review_retry

        try:
            prepared = prepare_review_retry(
                manifest_path=arguments.from_manifest, task_id=arguments.task_id,
                output_root=arguments.output)
            if arguments.check_only:
                print(json.dumps({"status": "READY", "task_id": arguments.task_id,
                                  "native_trials": len(prepared["native_trials"]),
                                  "model_calls": 0, "ags_calls": 0}))
                return 0
            if arguments.config is None:
                raise ValueError("实际后审需要 --config；仅认证使用 --check-only")
            os.environ["HERMES_REDACT_SECRETS"] = "false"
            role = resolve_role_matrix(arguments.config)["reconstruction"]
            agent = build_hermes_runtime(
                config_path=arguments.config, channel=role.channel,
                model_name=role.model, hermes_home=arguments.hermes_home)
            factory = (build_ags_runtime_factory(
                harbor_root=arguments.harbor_root, output_root=arguments.output,
                config_path=arguments.config) if prepared["domain"] == "terminal" else None)
            result_path = run_review_retry(prepared, agent=agent, runtime_factory=factory)
            result = json.loads(result_path.read_text())
            print(result_path)
            return 0 if result["status"] == "ROLLOUT_COMPLETED" else 2
        except (ValueError, OSError, KeyError, TypeError, HarborResultError, HarborRolloutError,
                HermesUnavailableError, SandboxUnavailableError) as exc:
            print(f"后审重试失败：{exc}", file=sys.stderr)
            return 2

    if arguments.command == "reconstruct" and arguments.reconstruct_command == "raw-run":
        try:
            os.environ["HERMES_REDACT_SECRETS"] = "false"
            container_runtime_factory = None
            if arguments.config is None:
                raise ValueError("会话解析需要 --config 中的 session_parser 模型连接配置")
            raw_line = load_raw_line(
                arguments.input,
                line_number=arguments.line_number,
                line_sha256=arguments.line_sha256,
            )
            if arguments.sandbox and arguments.domain == "terminal":
                container_runtime_factory = build_ags_runtime_factory(
                    harbor_root=arguments.harbor_root,
                    output_root=arguments.output,
                    config_path=arguments.config,
                )
            matrix = resolve_role_matrix(
                arguments.config,
                overrides={
                    "reconstruction": (arguments.channel, arguments.model_name),
                    "verifier": (arguments.verifier_channel, arguments.verifier_model),
                    "rollout": (arguments.rollout_channel, arguments.rollout_model),
                },
            )
            reconstruction_role = matrix["reconstruction"]
            verifier_role = matrix["verifier"]
            rollout_role = matrix["rollout"]
            resolved_rollout = resolve_rollout_model(
                arguments.rollout_model,
                channel=rollout_role.channel,
                model_name=rollout_role.model,
            )
            arguments.output.mkdir(parents=True, exist_ok=True)
            (arguments.output / "model_roles.json").write_text(
                json.dumps(
                    {name: role.public() for name, role in matrix.items()},
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            reconstruction_agent = build_hermes_runtime(
                config_path=arguments.config,
                channel=reconstruction_role.channel,
                model_name=reconstruction_role.model,
                hermes_home=arguments.hermes_home,
            )
            verifier_agent = build_hermes_runtime(
                config_path=arguments.config,
                channel=verifier_role.channel,
                model_name=verifier_role.model,
                hermes_home=arguments.hermes_home,
            )
            verification_model = build_chat_model(
                config_path=arguments.config, channel=verifier_role.channel
            )
            rollout_timeout_seconds, rollout_max_iterations = load_rollout_limits(
                arguments.config,
                timeout_seconds=arguments.rollout_timeout_seconds,
                max_iterations=arguments.rollout_max_iterations,
            )
            output_path = run_raw_session_reconstruction(
                raw_line=raw_line,
                domain=arguments.domain,
                line_number=arguments.line_number,
                source_ref=(
                    arguments.source_ref
                    or f"{arguments.input}:{arguments.line_number}"
                ),
                agent=reconstruction_agent,
                parser_model=build_chat_model(
                    config_path=arguments.config, channel=matrix["session_parser"].channel
                ),
                parser_model_name=matrix["session_parser"].model,
                verifier_agent=verifier_agent,
                verification_model=verification_model,
                verification_config=VerificationConfig(
                    harbor_root=arguments.harbor_root,
                    model_name=verifier_role.model,
                    rollout_model=resolved_rollout,
                    execute_red=arguments.execute_red,
                    execute_rollout=arguments.execute_rollout,
                    manual_response_review=arguments.manual_response_review,
                    disable_verification=arguments.disable_verification,
                    rollout_trials=arguments.rollout_trials,
                    max_rounds=arguments.verifier_rounds,
                    config_path=arguments.config,
                    hermes_home=arguments.hermes_home,
                    channel=rollout_role.channel,
                    timeout_seconds=rollout_timeout_seconds,
                    rollout_max_iterations=rollout_max_iterations,
                ),
                output_root=arguments.output,
                container_runtime_factory=container_runtime_factory,
            )
        except (
            ReconstructionError,
            ReconstructionSourceError,
            ModelGatewayError,
            HermesUnavailableError,
            SandboxUnavailableError,
            ValueError,
        ) as exc:
            print(f"原始 session 重建失败：{exc}", file=sys.stderr)
            return 2
        return _reconstruction_exit_code(output_path)
    if arguments.command == "requery" and arguments.requery_command == "single-ws":
        try:
            workspace = arguments.workspace.resolve()
            if not workspace.is_dir():
                raise ValueError(f"workspace 不存在：{workspace}")
            files = {
                path.relative_to(workspace).as_posix(): path.read_text(
                    encoding="utf-8", errors="ignore"
                )[:20000]
                for path in sorted(workspace.rglob("*"))
                if path.is_file()
                and not any(part in {".git", "hidden_control", "solution"} for part in path.parts)
            }
            result = synthesize_single_workspace_tasks(
                workspace_inventory=sorted(files),
                workspace_files=files,
                model=build_chat_model(config_path=arguments.config, channel=arguments.channel),
                model_name=resolve_model_name(
                    arguments.model_name,
                    config_path=arguments.config,
                    channel=arguments.channel,
                ),
                selection_seed=arguments.selection_seed,
            )
            arguments.output.mkdir(parents=True, exist_ok=True)
            output_path = arguments.output / "single_workspace_synthesis.json"
            output_path.write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except (
            OSError,
            UnicodeError,
            ModelGatewayError,
            SingleWorkspaceSynthesisError,
            ValueError,
        ) as exc:
            print(f"Single-WS 任务合成失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "requery" and arguments.requery_command == "cross-ws":
        try:
            rows = json.loads(arguments.workspaces_json.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError("workspaces-json 必须是数组")
            profiles = []
            for row in rows:
                if not isinstance(row, dict) or not row.get("id") or not row.get("path"):
                    raise ValueError("每个 workspace 必须有 id 和 path")
                profiles.append(profile_workspace(str(row["id"]), Path(row["path"])))
            pairs = retrieve_directional_pairs(profiles, min_overlap=arguments.min_overlap)
            payload = {
                "schema_version": "traceforge.requery-cross-workspace.v1",
                "candidates": [
                    {
                        "reference_workspace_id": reference.workspace_id,
                        "target_workspace_id": target.workspace_id,
                        "gap": list(gap),
                        "prompt": build_cross_workspace_prompt(reference, target, gap),
                    }
                    for reference, target, gap in pairs
                ],
            }
            arguments.output.mkdir(parents=True, exist_ok=True)
            output_path = arguments.output / "cross_workspace.json"
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"跨 workspace 配对失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    if arguments.command == "requery" and arguments.requery_command == "multi-round":
        try:
            raw = json.loads(arguments.rounds_json.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("rounds-json 必须是对象")
            tracker = RequirementTracker()
            tracker.apply(list(raw.get("requirements") or []))
            rounds: list[RoundResult] = []
            followups: list[str] = []
            for index, item in enumerate(raw.get("rounds") or []):
                if not isinstance(item, dict):
                    raise ValueError("rounds 必须是对象数组")
                result = RoundResult(
                    int(item.get("round_index", index)),
                    str(item.get("verifier_status") or ""),
                    str(item.get("user_feedback") or ""),
                    tuple(item.get("requirement_ids") or ()),
                )
                rounds.append(result)
                followups.append(build_followup_prompt(tracker, result))
            payload = {
                "schema_version": "traceforge.requery-multi-round.v1",
                "tracker": tracker.to_dict(),
                "followups": followups,
                "retain_verified_session": retain_verified_session(
                    rounds, minimum_passes=arguments.minimum_passes
                ),
            }
            arguments.output.mkdir(parents=True, exist_ok=True)
            output_path = arguments.output / "multi_round.json"
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"多轮 requery 失败：{exc}", file=sys.stderr)
            return 2
        print(output_path)
        return 0
    parser.error("不支持的命令")
    return 2
