from __future__ import annotations

import json
from pathlib import Path

from traceforge.reconstruction.pipeline import build_reconstruction_pipeline


def test_pipeline_materializes_selection_and_pending_execution_plan(tmp_path: Path, monkeypatch) -> None:
    m4 = tmp_path / out / m4 / m4-run
    (m4 / private).mkdir(parents=True)
    manifest = {
        failure_analysis_run_id: m4-run,
        m1b_run_id: m1b-run,
        m1d_run_id: m1d-run,
        m1b_artifact_manifest_sha256: a * 64,
    }
    (m4 / artifact_manifest.json).write_text(json.dumps(manifest), encoding=utf-8)
    report = {
        report_id: report-1, session_ref: session-1,
        episode_ref: episode-1, attempt_ref: attempt-1,
        primary_failure: INCONCLUSIVE, failure_layer: UNCLEAR,
        recoverability: UNKNOWN, confidence: 0.0,
        evidence_ref_ids: [e-1], reconstruction_targets: [],
    }
    (m4 / private/failure_analysis.jsonl).write_text(json.dumps(report) + n, encoding=utf-8)
    monkeypatch.setattr(traceforge.reconstruction.pipeline.build_failure_analysis, lambda **_: m4)

    out = build_reconstruction_pipeline(m1b_run_dir=tmp_path / m1b, m1d_run_dir=tmp_path / m1d, output_root=tmp_path / out)
    selection = json.loads((out / selection_manifest.json).read_text(encoding=utf-8))
    plan = json.loads((out / execution_plan.json).read_text(encoding=utf-8))
    assert selection[candidate_count] == 1
    assert selection[candidates][0][decision] == DEFER
    assert plan[model_calls] == 0
    assert {node[status] for node in plan[nodes]} >= {COMPLETED, PENDING_MODEL}
