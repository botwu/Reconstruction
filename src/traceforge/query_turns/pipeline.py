"""M1D 回合派生编排与 artifact 发布，镜像 `lineage.pipeline.build_lineage` 的发布骨架。

流程：校验读入 M1B run → 纯函数派生回合图 → 内容寻址 M1D run → staging 写全部产物 →
原子发布。任一异常 fail-closed：中止 writers 与 workspace，不留半份产物（规格 §2.1）。
零模型调用、零语义关系；输出逐字节可复现（内容寻址 run_id 仅绑 M1B 输入身份）。
"""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from traceforge import __version__
from traceforge.query_turns.builder import build_query_turn_graph
from traceforge.query_turns.contracts import (
    QUERY_TURN_ARTIFACT_MANIFEST_SCHEMA,
    QUERY_TURN_CONTRACT_VERSION,
    QUERY_TURN_MANIFEST_SCHEMA,
    QUERY_TURN_REPORT_SCHEMA,
    QUERY_TURN_RUN_RECEIPT_SCHEMA,
    QueryTurnArtifactManifestV1,
    QueryTurnManifestV1,
    QueryTurnReportV1,
    artifact_entry_dicts,
    build_report_counts,
    query_turn_run_id,
)
from traceforge.query_turns.reader import load_m1b_turn_view
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    JsonlArtifactWriter,
    write_json_artifact,
)
from traceforge.trajectory.provenance import collect_git_provenance

_JSONL_ARTIFACTS = {
    "user_blocks": "private/user_blocks.jsonl",
    "agent_steps": "private/agent_steps.jsonl",
    "assistant_outcomes": "private/assistant_outcomes.jsonl",
    "query_turns": "private/query_turns.jsonl",
    "capture_turn_accounting": "private/capture_turn_accounting.jsonl",
    "thread_turn_edges": "private/thread_turn_edges.jsonl",
}


def _close_writers(writers: dict[str, JsonlArtifactWriter]) -> list[Any]:
    return [writers[name].close() for name in sorted(writers)]


def _abort_writers(writers: dict[str, JsonlArtifactWriter]) -> list[BaseException]:
    errors: list[BaseException] = []
    for writer in writers.values():
        try:
            writer.abort()
        except BaseException as exc:  # 汇总清理异常，附注后重抛主异常
            errors.append(exc)
    return errors


def build_query_turns(*, m1b_run_dir: str | Path, output_root: str | Path) -> Path:
    """在一个已发布 M1B run 之上派生并发布 M1D 回合图产物，返回发布后的 M1D run 目录。"""

    started_at = datetime.now(UTC)
    git_provenance = collect_git_provenance()

    view = load_m1b_turn_view(m1b_run_dir)
    graph = build_query_turn_graph(m1b_run_id=view.m1b_run_id, captures=view.captures)

    run_id = query_turn_run_id(
        m1b_run_id=view.m1b_run_id,
        m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
    )

    workspace = ArtifactWorkspace(Path(output_root), run_id)
    writers: dict[str, JsonlArtifactWriter] = {}
    entries = []
    try:
        manifest = QueryTurnManifestV1(
            schema_version=QUERY_TURN_MANIFEST_SCHEMA,
            query_turn_run_id=run_id,
            query_turn_contract_version=QUERY_TURN_CONTRACT_VERSION,
            m1b_run_id=view.m1b_run_id,
            m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
            source_schema=view.source_schema,
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "query_turn_manifest.json",
                manifest.to_dict(),
            )
        )

        writers = workspace.open_jsonl_writers(_JSONL_ARTIFACTS)
        for user_block in graph.user_blocks:
            writers["user_blocks"].write(user_block.to_dict())
        for agent_step in graph.agent_steps:
            writers["agent_steps"].write(agent_step.to_dict())
        for outcome in graph.assistant_outcomes:
            writers["assistant_outcomes"].write(outcome.to_dict())
        for query_turn in graph.query_turns:
            writers["query_turns"].write(query_turn.to_dict())
        for accounting in graph.capture_accounting:
            writers["capture_turn_accounting"].write(accounting.to_dict())
        for edge in graph.thread_turn_edges:
            writers["thread_turn_edges"].write(edge.to_dict())
        entries.extend(_close_writers(writers))

        report = QueryTurnReportV1(
            schema_version=QUERY_TURN_REPORT_SCHEMA,
            query_turn_run_id=run_id,
            m1b_run_id=view.m1b_run_id,
            counts=build_report_counts(
                capture_count=graph.capture_count,
                query_turn_count=graph.query_turn_count,
                prefix_rooted_turn_count=graph.prefix_rooted_turn_count,
                observed_rooted_turn_count=graph.observed_rooted_turn_count,
                complete_turn_count=graph.complete_turn_count,
                incomplete_turn_count=graph.incomplete_turn_count,
                agent_step_count=graph.agent_step_count,
                user_block_count=graph.user_block_count,
                assistant_outcome_count=graph.assistant_outcome_count,
                thread_turn_edge_count=graph.thread_turn_edge_count,
                orphan_tool_observation_count=graph.orphan_tool_observation_count,
                captures_with_unlocalizable_prefix_count=graph.captures_with_unlocalizable_prefix_count,
                captures_with_compaction_count=graph.captures_with_compaction_count,
            ),
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "reports/m1d_report.json",
                report.to_dict(),
            )
        )

        artifact_manifest = QueryTurnArtifactManifestV1(
            schema_version=QUERY_TURN_ARTIFACT_MANIFEST_SCHEMA,
            query_turn_run_id=run_id,
            query_turn_contract_version=QUERY_TURN_CONTRACT_VERSION,
            m1b_run_id=view.m1b_run_id,
            m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
            files=artifact_entry_dicts(entries),
        )
        manifest_entry = write_json_artifact(
            workspace.staging_path,
            "artifact_manifest.json",
            artifact_manifest.to_dict(),
        )

        completion_git_provenance = collect_git_provenance()
        git_provenance_verified_at_completion = (
            git_provenance.available
            and completion_git_provenance.available
            and git_provenance == completion_git_provenance
        )
        completed_at = datetime.now(UTC)
        write_json_artifact(
            workspace.staging_path,
            "run_receipt.json",
            {
                "artifact_manifest_sha256": manifest_entry.sha256,
                "completed_at": completed_at.isoformat(),
                "duration_seconds": (completed_at - started_at).total_seconds(),
                "git_provenance": git_provenance.to_dict(),
                "git_provenance_verified_at_completion": git_provenance_verified_at_completion,
                "python": platform.python_version(),
                "run_id": run_id,
                "schema_version": QUERY_TURN_RUN_RECEIPT_SCHEMA,
                "traceforge_version": __version__,
            },
        )
        return workspace.publish()
    except BaseException as primary_error:
        cleanup_errors = _abort_writers(writers)
        try:
            workspace.abort()
        except BaseException as cleanup_error:  # 附注清理异常后重抛主异常
            cleanup_errors.append(cleanup_error)
        for cleanup_error in cleanup_errors:
            primary_error.add_note(f"清理失败：{cleanup_error!r}")
        raise
