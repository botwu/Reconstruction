"""`UserTextProjection` 纯 fold：确定性排序、M1D 绑定语义、契约违约 fail-closed（规格 §2.1/§3）。

直接以视图 dataclass 驱动 builder，不建 run；专门覆盖 pipeline 测试构造不出的"上游 validator 已过
却仍违约"的输入（如 M1D 绑定但观测事件无归属）。
"""

from __future__ import annotations

import pytest

from traceforge.source_projection.builder import build_user_text_projection_graph
from traceforge.source_projection.contracts import (
    PROJECTION_COUNT_KEYS,
    UserTextProjectionInputError,
)
from traceforge.source_projection.view import M1bUserTextView, M1dBlockView, UserEventInput

_OBSERVED = "OBSERVED_REQUEST_WINDOW"
_PREFIX = "PRE_FIRST_OBSERVED_TERMINAL"


def _event(
    event_id: str,
    capture: str,
    sequence: int,
    *,
    scope: str = _OBSERVED,
    boundary: str | None = "boundary-1",
    head: str | None = "普通文本",
    form: str = "TEXT_STRING",
) -> UserEventInput:
    if scope == _PREFIX and boundary == "boundary-1":
        boundary = None
    return UserEventInput(
        event_id=event_id,
        capture_occurrence_id=capture,
        sequence_number=sequence,
        event_scope=scope,
        request_boundary_id=boundary,
        content_form=form,
        head_window=head,
        utf8_byte_length=None if head is None else len(head.encode("utf-8")),
    )


def _view(*events: UserEventInput, captures: frozenset[str] | None = None) -> M1bUserTextView:
    return M1bUserTextView(
        m1b_run_id="m1b-run",
        m1b_artifact_manifest_sha256="a" * 64,
        source_schema="fixture-schema",
        capture_occurrence_ids=captures or frozenset(e.capture_occurrence_id for e in events),
        user_events=tuple(events),
    )


def _blocks(mapping: dict[str, str]) -> M1dBlockView:
    return M1dBlockView(
        m1d_run_id="m1d-run",
        m1d_artifact_manifest_sha256="b" * 64,
        user_block_id_by_event_id=mapping,
        capture_id_by_user_block_id={block: "cap-a" for block in mapping.values()},
    )


def test_fold_orders_by_capture_sequence_and_is_input_order_independent() -> None:
    events = (
        _event("e-3", "cap-b", 5),
        _event("e-1", "cap-a", 9, scope=_PREFIX),
        _event("e-2", "cap-a", 12),
    )
    forward = build_user_text_projection_graph(view=_view(*events), block_view=None)
    backward = build_user_text_projection_graph(view=_view(*reversed(events)), block_view=None)

    assert forward == backward
    assert [a.event_occurrence_id for a in forward.annotations] == ["e-1", "e-2", "e-3"]
    assert frozenset(forward.report_counts) == PROJECTION_COUNT_KEYS
    assert forward.report_counts["capture_count"] == 2
    assert forward.report_counts["user_event_count"] == 3
    assert forward.report_counts["captures_with_plain_user_text"] == 2
    assert forward.report_counts["captures_with_observed_plain_user_text"] == 2
    assert forward.report_counts["captures_with_plain_user_text_only_in_prefix"] == 0


def test_capture_count_is_taken_from_m1b_not_from_user_events() -> None:
    """诚实分母：没有任何 USER 事件的 capture 也计入 capture_count。"""

    view = _view(_event("e-1", "cap-a", 1), captures=frozenset({"cap-a", "cap-silent"}))
    graph = build_user_text_projection_graph(view=view, block_view=None)
    assert graph.report_counts["capture_count"] == 2
    assert graph.report_counts["captures_with_plain_user_text"] == 1


def test_m1d_binding_back_references_observed_and_never_prefix() -> None:
    view = _view(_event("e-1", "cap-a", 1, scope=_PREFIX), _event("e-2", "cap-a", 3))
    graph = build_user_text_projection_graph(view=view, block_view=_blocks({"e-2": "block-1"}))
    by_id = {a.event_occurrence_id: a for a in graph.annotations}
    assert by_id["e-1"].user_block_id is None
    assert by_id["e-2"].user_block_id == "block-1"


def test_m1d_binding_with_unassigned_observed_event_fails_closed() -> None:
    """绑定 M1D 即要求每个观测 USER 事件都有 UserBlock 归属；缺一即整批失败（规格 §6）。"""

    view = _view(_event("e-1", "cap-a", 1), _event("e-2", "cap-a", 3))
    with pytest.raises(UserTextProjectionInputError):
        build_user_text_projection_graph(view=view, block_view=_blocks({"e-1": "block-1"}))


@pytest.mark.parametrize(
    "event",
    [
        _event("e-1", "cap-a", 1, scope="SOMETHING_ELSE"),
        _event("e-1", "cap-a", 1, scope=_OBSERVED, boundary=None),
        _event("e-1", "cap-a", 1, scope=_PREFIX, boundary="boundary-x"),
    ],
)
def test_upstream_contract_violations_fail_closed(event: UserEventInput) -> None:
    with pytest.raises(UserTextProjectionInputError):
        build_user_text_projection_graph(view=_view(event), block_view=None)


def test_only_in_prefix_denominator_counts_captures_without_observed_plain_text() -> None:
    view = _view(
        _event("e-1", "cap-a", 1, scope=_PREFIX),  # 只在前缀有普通文本
        _event("e-2", "cap-a", 3, head="<turn_aborted/>"),
        _event("e-3", "cap-b", 1, scope=_PREFIX),  # 前缀 + 观测都有普通文本
        _event("e-4", "cap-b", 3),
        _event("e-5", "cap-c", 1, scope=_PREFIX, head=None, form="CONTENT_BLOCKS"),  # 无普通文本
    )
    counts = build_user_text_projection_graph(view=view, block_view=None).report_counts
    assert counts["captures_with_plain_user_text"] == 2
    assert counts["captures_with_plain_user_text_only_in_prefix"] == 1
    assert counts["captures_with_observed_plain_user_text"] == 1
    assert counts["observed_control_signal_count"] == 1
    assert counts["prefix_unlocalized_no_leading_text_count"] == 1
