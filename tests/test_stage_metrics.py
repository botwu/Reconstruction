from __future__ import annotations

import pytest

from traceforge.reconstruction.stage_metrics import reconstruction_stage_metrics


@pytest.mark.parametrize(
    ("decision", "ready_count", "review_count"),
    [
        ("READY_ORIGINAL", 1, 0),
        ("REVIEW_TASK_FIT", 0, 1),
        ("INCOMPATIBLE", 0, 0),
        (None, 0, 0),
    ],
)
def test_task_fit_counts_preserve_decision(
    decision: str | None, ready_count: int, review_count: int
) -> None:
    metrics = reconstruction_stage_metrics(
        {"selected_task_ids": ["task-1"]},
        {},
        [{"task_id": "task-1", "task_fit": {"decision": decision}}],
    )

    assert metrics["task_fit_status_counts"] == {decision or "NOT_RUN": 1}
    assert metrics["task_fit_ready_count"] == ready_count
    assert metrics["task_fit_review_count"] == review_count
    assert metrics["task_fit_ready_variant_count"] == 0
