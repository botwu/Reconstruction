"""`UserTextProjection` 编排与 artifact 发布，镜像 `query_turns.pipeline` 的发布骨架。

流程：校验读入 M1B run（+ 可选 M1D run）→ 纯 fold 注解 → 内容寻址 run_id（绑定 M1B 与可选 M1D 的
身份）→ staging 写全部产物 → 原子发布。任一异常 fail-closed：中止 writers 与 workspace，不留半份
产物（规格 §2.1/§4）。零模型调用；产物不含任何正文与未知标签名。
"""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from traceforge import __version__
from traceforge.source_projection.builder import build_user_text_projection_graph
from traceforge.source_projection.contracts import (
    ANNOTATIONS_RELATIVE_PATH,
    PROJECTION_ARTIFACT_MANIFEST_SCHEMA,
    PROJECTION_MANIFEST_RELATIVE_PATH,
    PROJECTION_MANIFEST_SCHEMA,
    PROJECTION_REPORT_RELATIVE_PATH,
    PROJECTION_REPORT_SCHEMA,
    PROJECTION_RUN_RECEIPT_SCHEMA,
    USER_TEXT_PROJECTION_CONTRACT_VERSION,
    ProjectionArtifactManifestV1,
    ProjectionManifestV1,
    ProjectionReportV1,
    projection_run_id,
)
from traceforge.source_projection.reader import load_m1b_user_text_view, load_m1d_block_view
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    JsonlArtifactWriter,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.trajectory.provenance import collect_git_provenance

_JSONL_ARTIFACTS = {"annotations": ANNOTATIONS_RELATIVE_PATH}


def _abort_writers(writers: dict[str, JsonlArtifactWriter]) -> list[BaseException]:
    errors: list[BaseException] = []
    for writer in writers.values():
        try:
            writer.abort()
        except BaseException as exc:  # 汇总清理异常，附注后重抛主异常
            errors.append(exc)
    return errors


def build_user_text_projection(
    *,
    m1b_run_dir: str | Path,
    output_root: str | Path,
    m1d_run_dir: str | Path | None = None,
) -> Path:
    """在一个已发布 M1B run（+ 可选 M1D run）之上派生并发布 USER 事件结构注解，返回发布目录。"""

    started_at = datetime.now(UTC)
    git_provenance = collect_git_provenance()

    view = load_m1b_user_text_view(m1b_run_dir)
    block_view = None if m1d_run_dir is None else load_m1d_block_view(m1d_run_dir, m1b_run_dir)
    graph = build_user_text_projection_graph(view=view, block_view=block_view)

    m1d_run_id = None if block_view is None else block_view.m1d_run_id
    m1d_manifest_sha256 = None if block_view is None else block_view.m1d_artifact_manifest_sha256
    run_id = projection_run_id(
        m1b_run_id=view.m1b_run_id,
        m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
        m1d_run_id=m1d_run_id,
        m1d_artifact_manifest_sha256=m1d_manifest_sha256,
    )

    workspace = ArtifactWorkspace(Path(output_root), run_id)
    writers: dict[str, JsonlArtifactWriter] = {}
    entries: list[Any] = []
    try:
        manifest = ProjectionManifestV1(
            schema_version=PROJECTION_MANIFEST_SCHEMA,
            projection_run_id=run_id,
            projection_contract_version=USER_TEXT_PROJECTION_CONTRACT_VERSION,
            m1b_run_id=view.m1b_run_id,
            m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
            m1d_run_id=m1d_run_id,
            m1d_artifact_manifest_sha256=m1d_manifest_sha256,
            source_schema=view.source_schema,
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path, PROJECTION_MANIFEST_RELATIVE_PATH, manifest.to_dict()
            )
        )

        writers = workspace.open_jsonl_writers(_JSONL_ARTIFACTS)
        for annotation in graph.annotations:
            writers["annotations"].write(annotation.to_dict())
        entries.append(writers["annotations"].close())

        report = ProjectionReportV1(
            schema_version=PROJECTION_REPORT_SCHEMA,
            projection_run_id=run_id,
            m1b_run_id=view.m1b_run_id,
            m1d_run_id=m1d_run_id,
            counts=graph.report_counts,
        )
        entries.append(
            write_json_artifact(
                workspace.staging_path, PROJECTION_REPORT_RELATIVE_PATH, report.to_dict()
            )
        )

        artifact_manifest = ProjectionArtifactManifestV1(
            schema_version=PROJECTION_ARTIFACT_MANIFEST_SCHEMA,
            projection_run_id=run_id,
            projection_contract_version=USER_TEXT_PROJECTION_CONTRACT_VERSION,
            m1b_run_id=view.m1b_run_id,
            m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
            m1d_run_id=m1d_run_id,
            m1d_artifact_manifest_sha256=m1d_manifest_sha256,
            files=artifact_entry_dicts(entries),
        )
        manifest_entry = write_json_artifact(
            workspace.staging_path, "artifact_manifest.json", artifact_manifest.to_dict()
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
                "schema_version": PROJECTION_RUN_RECEIPT_SCHEMA,
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
