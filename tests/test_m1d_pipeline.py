"""M1D 发布编排测试：结构完整、确定性逐字节、回执不漏路径、失败不留半份产物（规格 §2.1/§7）。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from traceforge.query_turns import build_query_turns
from traceforge.query_turns.contracts import (
    QUERY_TURN_CONTRACT_VERSION,
    QUERY_TURN_COUNT_KEYS,
    QUERY_TURN_RUN_RECEIPT_SCHEMA,
    query_turn_run_id,
)
from traceforge.query_turns.reader import QueryTurnInputError
from traceforge.trajectory.artifacts import ArtifactPublishError

_PRIVATE = [
    "private/user_blocks.jsonl",
    "private/agent_steps.jsonl",
    "private/assistant_outcomes.jsonl",
    "private/query_turns.jsonl",
    "private/capture_turn_accounting.jsonl",
    "private/thread_turn_edges.jsonl",
]


def _capture(
    capture_factory: Callable[..., dict[str, Any]], request_ids: list[str], **kwargs: Any
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for index in range(len(request_ids)):
        messages.append({"role": "user", "content": f"用户回合 {index}"})
        messages.append({"role": "assistant", "content": f"助手回合 {index}"})
    depths = [2 * (index + 1) for index in range(len(request_ids))]
    return capture_factory(
        messages=messages, terminal_prefix_depths=depths, request_ids=request_ids, **kwargs
    )


def _dirs(output_root: Path) -> list[Path]:
    return [c for c in output_root.iterdir() if c.is_dir()] if output_root.exists() else []


def test_publish_structure_and_manifest_binding(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    two_boundary_capture: dict[str, Any],
    tmp_path: Path,
) -> None:
    m1b_run = compile_dataset(
        [_capture(capture_factory, ["r1", "r2"]), two_boundary_capture], label="m1d-structure"
    )
    published = build_query_turns(m1b_run_dir=m1b_run, output_root=tmp_path / "m1d_out")

    manifest = json.loads((published / "query_turn_manifest.json").read_text(encoding="utf-8"))
    # 内容寻址 run_id = 目录名 = 按上游身份独立重算之值。
    assert published.name == manifest["query_turn_run_id"]
    assert published.name == query_turn_run_id(
        m1b_run_id=manifest["m1b_run_id"],
        m1b_artifact_manifest_sha256=manifest["m1b_artifact_manifest_sha256"],
    )
    assert manifest["query_turn_contract_version"] == QUERY_TURN_CONTRACT_VERSION

    for relative in [*_PRIVATE, "reports/m1d_report.json", "artifact_manifest.json"]:
        assert (published / relative).is_file()

    # artifact_manifest 不含自身与 run_receipt（镜像 M1C）。
    files = {
        e["relative_path"]
        for e in json.loads((published / "artifact_manifest.json").read_text())["files"]
    }
    assert "artifact_manifest.json" not in files
    assert "run_receipt.json" not in files


def test_report_counts_closed_allowlist(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    m1b_run = compile_dataset(
        [_capture(capture_factory, ["r1", "r2"]), _capture(capture_factory, ["r3"])],
        label="m1d-report",
    )
    published = build_query_turns(m1b_run_dir=m1b_run, output_root=tmp_path / "m1d_out")
    report = json.loads((published / "reports/m1d_report.json").read_text(encoding="utf-8"))
    assert set(report["counts"]) == set(QUERY_TURN_COUNT_KEYS)
    assert report["counts"]["capture_count"] == 2


def test_double_build_business_artifacts_are_byte_identical(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    two_boundary_capture: dict[str, Any],
    tmp_path: Path,
) -> None:
    m1b_run = compile_dataset(
        [_capture(capture_factory, ["r1", "r2"]), two_boundary_capture], label="m1d-determinism"
    )
    first = build_query_turns(m1b_run_dir=m1b_run, output_root=tmp_path / "root_a")
    second = build_query_turns(m1b_run_dir=m1b_run, output_root=tmp_path / "root_b")

    assert first.name == second.name
    # artifact_manifest 逐字节一致 → 蕴含全部业务文件摘要一致。
    assert (first / "artifact_manifest.json").read_bytes() == (
        second / "artifact_manifest.json"
    ).read_bytes()
    artifact_manifest = json.loads((first / "artifact_manifest.json").read_text(encoding="utf-8"))
    for entry in artifact_manifest["files"]:
        relative = entry["relative_path"]
        assert (first / relative).read_bytes() == (second / relative).read_bytes()
    assert "run_receipt.json" not in {e["relative_path"] for e in artifact_manifest["files"]}


def test_run_receipt_fields_and_no_path_leak(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    m1b_run = compile_dataset([_capture(capture_factory, ["r1", "r2"])], label="m1d-receipt")
    output_root = tmp_path / "m1d_out"
    published = build_query_turns(m1b_run_dir=m1b_run, output_root=output_root)

    receipt_text = (published / "run_receipt.json").read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert receipt["schema_version"] == QUERY_TURN_RUN_RECEIPT_SCHEMA
    assert receipt["run_id"] == published.name
    assert receipt["git_provenance_verified_at_completion"] is True
    # 绝对路径字面量不得出现在回执任何位置。
    assert str(m1b_run) not in receipt_text
    assert str(output_root) not in receipt_text
    assert str(tmp_path) not in receipt_text


def test_m1b_integrity_failure_leaves_no_partial_output(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """篡改上游私有表（不重签）→ 先校验拦下，绝不产出半份 M1D run。"""

    m1b_run = compile_dataset([_capture(capture_factory, ["r1", "r2"])], label="m1d-fail")
    tampered = m1b_run / "private" / "captures.jsonl"
    tampered.write_bytes(tampered.read_bytes() + b'{"injected":1}\n')

    output_root = tmp_path / "m1d_out"
    with pytest.raises(QueryTurnInputError):
        build_query_turns(m1b_run_dir=m1b_run, output_root=output_root)
    assert _dirs(output_root) == []


def test_content_address_refuses_overwrite(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    m1b_run = compile_dataset([_capture(capture_factory, ["r1", "r2"])], label="m1d-overwrite")
    output_root = tmp_path / "m1d_out"
    first = build_query_turns(m1b_run_dir=m1b_run, output_root=output_root)
    assert first.is_dir()
    with pytest.raises(ArtifactPublishError):
        build_query_turns(m1b_run_dir=m1b_run, output_root=output_root)
    assert _dirs(output_root) == [first]
