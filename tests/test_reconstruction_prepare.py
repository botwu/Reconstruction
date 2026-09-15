"""`reconstruct prepare` 确定性输入准备的回归测试。

覆盖三条契约：
1. 干净轨迹（工具调用全部配对+观测，或无工具调用）→ report.status=READY、无需人审；
2. 存在未观测工具调用 → pending>0、requires_review、status=REVIEW；
3. 显式指定不存在的 capture_id → 明确报错，不静默猜测。
"""

from __future__ import annotations

import json

import pytest

from traceforge.reconstruction.prepare import (
    ReconstructionPrepareError,
    build_reconstruction_inputs,
)


def test_prepare_clean_trajectory_report_is_ready(
    tmp_path, compile_dataset, stable_git_provenance, capture_factory
):
    capture = capture_factory(
        messages=[
            {"role": "user", "content": "请完成一个完全虚构的检索任务。"},
            {"role": "assistant", "content": "已完成虚构任务。"},
        ],
        terminal_prefix_depths=[2],
        request_ids=["clean-1"],
    )
    m1b = compile_dataset([capture])

    prepared = build_reconstruction_inputs(m1b_run_dir=m1b, output_dir=tmp_path / "prep")

    report = json.loads(prepared.report_path.read_text(encoding="utf-8"))
    evidence = json.loads(prepared.evidence_path.read_text(encoding="utf-8"))
    assert report["status"] == "READY"
    assert report["quality"]["requires_review"] is False
    assert report["selection"]["pending_tool_call_count"] == 0
    assert report["provenance"]["snapshot"] is False
    assert "failure_analysis" in report
    assert isinstance(evidence, list) and evidence
    assert any(item.get("phase") == "TARGET_REQUEST" for item in evidence)
    assert prepared.requires_review is False


def test_prepare_pending_tool_calls_force_review(
    tmp_path, compile_dataset, stable_git_provenance, two_boundary_capture
):
    m1b = compile_dataset([two_boundary_capture])

    prepared = build_reconstruction_inputs(m1b_run_dir=m1b, output_dir=tmp_path / "prep")

    report = json.loads(prepared.report_path.read_text(encoding="utf-8"))
    assert report["selection"]["pending_tool_call_count"] > 0
    assert report["quality"]["requires_review"] is True
    assert report["status"] == "REVIEW"
    assert prepared.requires_review is True
    assert prepared.source_report_id
    assert prepared.attempt_ref


def test_prepare_rejects_unknown_capture_id(
    tmp_path, compile_dataset, stable_git_provenance, two_boundary_capture
):
    m1b = compile_dataset([two_boundary_capture])

    with pytest.raises(ReconstructionPrepareError):
        build_reconstruction_inputs(
            m1b_run_dir=m1b, output_dir=tmp_path / "prep", capture_id="does-not-exist"
        )
