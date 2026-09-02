"""M1C 确定性：同一 M1B run 在不同 output root 建图两次 → 业务文件逐字节一致、run_id 稳定。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceforge.lineage import build_lineage
from traceforge.lineage.contracts import LINEAGE_CONTRACT_VERSION, lineage_run_id


def _capture(
    capture_factory: Callable[..., dict[str, Any]], request_ids: list[str]
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for index in range(len(request_ids)):
        messages.append({"role": "user", "content": f"用户回合 {index}"})
        messages.append({"role": "assistant", "content": f"助手回合 {index}"})
    depths = [2 * (index + 1) for index in range(len(request_ids))]
    return capture_factory(
        messages=messages, terminal_prefix_depths=depths, request_ids=request_ids
    )


def test_double_build_business_artifacts_are_byte_identical(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """两次建图：lineage_run_id 相同，所有业务文件逐字节一致，run_receipt 排除比对。"""

    m1b_run = compile_dataset(
        [
            _capture(capture_factory, ["r1", "r2"]),
            _capture(capture_factory, ["r2", "r3"]),
        ],
        label="determinism",
    )

    first = build_lineage(m1b_run_dir=m1b_run, output_root=tmp_path / "root_a")
    second = build_lineage(m1b_run_dir=m1b_run, output_root=tmp_path / "root_b")

    # 内容寻址 run_id 稳定，且等于按上游身份独立重算之值。
    assert first.name == second.name
    manifest = json.loads((first / "lineage_manifest.json").read_text(encoding="utf-8"))
    assert first.name == lineage_run_id(
        m1b_run_id=manifest["m1b_run_id"],
        m1b_artifact_manifest_sha256=manifest["m1b_artifact_manifest_sha256"],
    )
    assert manifest["lineage_contract_version"] == LINEAGE_CONTRACT_VERSION

    # artifact_manifest 逐字节一致 → 蕴含所有业务文件摘要一致。
    artifact_manifest = json.loads((first / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert (first / "artifact_manifest.json").read_bytes() == (
        second / "artifact_manifest.json"
    ).read_bytes()
    for entry in artifact_manifest["files"]:
        relative = entry["relative_path"]
        assert (first / relative).read_bytes() == (second / relative).read_bytes()
    # run_receipt 含时间/时长，本就非确定，明确排除于逐字节比对。
    assert "run_receipt.json" not in {
        entry["relative_path"] for entry in artifact_manifest["files"]
    }


def test_run_receipt_leaks_no_input_or_output_paths(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """run_receipt 不含 --m1b-run / --output 的任何绝对路径或路径键。"""

    m1b_run = compile_dataset([_capture(capture_factory, ["r1", "r2"])], label="receipt-paths")
    output_root = tmp_path / "lineage_out"
    published = build_lineage(m1b_run_dir=m1b_run, output_root=output_root)

    receipt_text = (published / "run_receipt.json").read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert "input_path" not in receipt
    assert "output_path" not in receipt
    assert "m1b_run_dir" not in receipt
    # 绝对路径字面量不得出现在回执任何位置。
    assert str(m1b_run) not in receipt_text
    assert str(output_root) not in receipt_text
    assert str(tmp_path) not in receipt_text
    assert receipt["git_provenance_verified_at_completion"] is True
