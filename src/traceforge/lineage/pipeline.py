"""M1C 建图编排与 artifact 发布，镜像 `trajectory.pipeline.compile_trajectory` 的发布骨架。

流程：校验读入 M1B run → 纯函数派生关系图 → 内容寻址 lineage run → staging 写全部产物 →
原子发布。任一异常 fail-closed：中止 writers 与 workspace，不留半份 lineage（规格 §2.1）。
"""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from traceforge import __version__
from traceforge.lineage.builder import build_lineage_graph
from traceforge.lineage.contracts import (
    LINEAGE_ARTIFACT_MANIFEST_SCHEMA,
    LINEAGE_CONTRACT_VERSION,
    LINEAGE_MANIFEST_SCHEMA,
    LINEAGE_REPORT_SCHEMA,
    LINEAGE_RUN_RECEIPT_SCHEMA,
    LineageArtifactManifestV1,
    LineageManifestV1,
    LineageReportV1,
    artifact_entry_dicts,
    build_report_counts,
    lineage_run_id,
)
from traceforge.lineage.reader import load_m1b_run_view
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    JsonlArtifactWriter,
    write_json_artifact,
)
from traceforge.trajectory.provenance import collect_git_provenance

_JSONL_ARTIFACTS = {
    "request_nodes": "private/request_nodes.jsonl",
    "request_successor_edges": "private/request_successor_edges.jsonl",
    "capture_relation_edges": "private/capture_relation_edges.jsonl",
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


def build_lineage(*, m1b_run_dir: str | Path, output_root: str | Path) -> Path:
    """在一个已发布 M1B run 之上派生并发布 M1C 关系图产物，返回发布后的 lineage run 目录。"""

    started_at = datetime.now(UTC)
    git_provenance = collect_git_provenance()

    view = load_m1b_run_view(m1b_run_dir)
    graph = build_lineage_graph(
        m1b_run_id=view.m1b_run_id,
        capture_ids=view.capture_ids,
        raw_request_hash_by_capture=view.raw_request_hash_by_capture,
        boundaries_by_capture=view.boundaries_by_capture,
        fingerprint_chain_by_capture=view.fingerprint_chain_by_capture,
    )
    candidate_group_count = len(set(view.candidate_group_by_capture.values()))

    run_id = lineage_run_id(
        m1b_run_id=view.m1b_run_id,
        m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
    )

    workspace = ArtifactWorkspace(Path(output_root), run_id)
    writers: dict[str, JsonlArtifactWriter] = {}
    entries = []
    try:
        lineage_manifest = LineageManifestV1(
            schema_version=LINEAGE_MANIFEST_SCHEMA,
            lineage_run_id=run_id,
            lineage_contract_version=LINEAGE_CONTRACT_VERSION,
            m1b_run_id=view.m1b_run_id,
            m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
            source_schema=view.source_schema,
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "lineage_manifest.json",
                lineage_manifest.to_dict(),
            )
        )

        writers = workspace.open_jsonl_writers(_JSONL_ARTIFACTS)
        for node in graph.request_nodes:
            writers["request_nodes"].write(node.to_dict())
        for edge in graph.request_successor_edges:
            writers["request_successor_edges"].write(edge.to_dict())
        for edge in graph.capture_relation_edges:
            writers["capture_relation_edges"].write(edge.to_dict())
        entries.extend(_close_writers(writers))

        report = LineageReportV1(
            schema_version=LINEAGE_REPORT_SCHEMA,
            lineage_run_id=run_id,
            m1b_run_id=view.m1b_run_id,
            counts=build_report_counts(
                capture_count=graph.capture_count,
                request_node_count=graph.request_node_count,
                candidate_group_count=candidate_group_count,
                raw_request_hash_qualified_count=graph.raw_request_hash_qualified_count,
                raw_request_hash_unknown_count=graph.raw_request_hash_unknown_count,
                edge_counts_by_relation=graph.edge_counts_by_relation,
            ),
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path,
                "reports/lineage_report.json",
                report.to_dict(),
            )
        )

        artifact_manifest = LineageArtifactManifestV1(
            schema_version=LINEAGE_ARTIFACT_MANIFEST_SCHEMA,
            lineage_run_id=run_id,
            lineage_contract_version=LINEAGE_CONTRACT_VERSION,
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
                "schema_version": LINEAGE_RUN_RECEIPT_SCHEMA,
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
