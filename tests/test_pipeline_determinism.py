"""M1A、M1B 确定性产物测试。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceforge.trajectory.contracts import COMPILER_CONTRACT_VERSION
from traceforge.trajectory.json_codec import stable_id
from traceforge.trajectory.source_adapter import RESTORED_LONG_CAPTURE_SCHEMA


def test_same_source_under_different_paths_has_identical_business_artifacts(
    compile_dataset: Callable[..., Path], two_boundary_capture: dict[str, Any]
) -> None:
    first = compile_dataset([two_boundary_capture], label="first")
    second = compile_dataset([two_boundary_capture], label="second")

    assert first.name == second.name
    assert (first / "source_manifest.json").read_bytes() == (
        second / "source_manifest.json"
    ).read_bytes()
    assert (first / "artifact_manifest.json").read_bytes() == (
        second / "artifact_manifest.json"
    ).read_bytes()

    manifest = json.loads((first / "artifact_manifest.json").read_text(encoding="utf-8"))
    source_manifest = json.loads((first / "source_manifest.json").read_text(encoding="utf-8"))
    assert source_manifest["source_schema"] == RESTORED_LONG_CAPTURE_SCHEMA
    assert manifest["source_schema"] == RESTORED_LONG_CAPTURE_SCHEMA
    assert first.name == stable_id(
        "trajectory-compile-run-v1",
        {
            "compiler_contract_version": COMPILER_CONTRACT_VERSION,
            "dataset_id": source_manifest["dataset_id"],
            "dataset_sha256": source_manifest["dataset_sha256"],
            "source_schema": RESTORED_LONG_CAPTURE_SCHEMA,
        },
    )
    for entry in manifest["files"]:
        relative_path = entry["relative_path"]
        assert (first / relative_path).read_bytes() == (second / relative_path).read_bytes()

    # 本地绝对路径只能存在于不参与业务摘要的运行回执中。
    assert (first / "run_receipt.json").read_bytes() != (second / "run_receipt.json").read_bytes()
