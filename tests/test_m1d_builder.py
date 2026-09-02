"""M1D builder 纯函数核心测试（规格 §4/§5）。

全部手构 ``CaptureInput``（零 IO），穷举三种真实 capture 形态 + 中途插话 + 孤儿观测 +
SYSTEM 断开 UserBlock + 前缀记账守恒 + 观测事件分区守恒 + 空观测流 + 确定性。断言按配对
归属（gate④）不受到达顺序影响、前缀不产回合、outcome/turn_status 派生正确。
"""

from __future__ import annotations

import pytest

from traceforge.query_turns.builder import QueryTurnGraph, build_query_turn_graph
from traceforge.query_turns.contracts import RootStatus, TurnStatus
from traceforge.query_turns.view import (
    ActionBatchInfo,
    CaptureInput,
    ObservedEvent,
    ToolPairingLink,
)
from traceforge.trajectory.contracts import EventKind

M1B_RUN_ID = "m" * 64


def _ev(
    event_id: str,
    kind: EventKind,
    seq: int,
    *,
    boundary: str | None = "b0",
    msg_index: int = 0,
    sub_index: int | None = None,
    text_len: int | None = None,
    block_count: int | None = None,
    has_blocks: bool = False,
) -> ObservedEvent:
    return ObservedEvent(
        event_id=event_id,
        event_kind=kind.value,
        sequence_number=seq,
        request_boundary_id=boundary,
        message_index=msg_index,
        sub_index=sub_index,
        text_utf8_byte_length=text_len,
        block_count=block_count,
        has_non_text_blocks=has_blocks,
    )


def _cap(
    cid: str,
    observed: list[ObservedEvent],
    *,
    terminal: str = "TEXT_OUTCOME",
    compaction: bool = False,
    prefix_counts: dict[str, int] | None = None,
    batches: list[ActionBatchInfo] | None = None,
    pairings: list[ToolPairingLink] | None = None,
) -> CaptureInput:
    return CaptureInput(
        capture_occurrence_id=cid,
        terminal_status=terminal,
        has_compaction=compaction,
        observed_events=tuple(observed),
        prefix_event_counts_by_kind=dict(prefix_counts or {}),
        action_batch_by_assistant={b.assistant_event_id: b for b in (batches or [])},
        tool_pairings=tuple(pairings or []),
    )


def _ab(assistant_id: str, calls: list[str], *, semantics: str = "SEQUENTIAL") -> ActionBatchInfo:
    return ActionBatchInfo(
        action_batch_id=f"ab-{assistant_id}",
        assistant_event_id=assistant_id,
        tool_call_event_ids=tuple(calls),
        execution_semantics=semantics,
    )


def _turns_by_ordinal(graph, cid):
    return {t.turn_ordinal: t for t in graph.query_turns if t.capture_occurrence_id == cid}


def _step_by_assistant(graph, cid):
    return {s.assistant_event_id: s for s in graph.agent_steps if s.capture_occurrence_id == cid}


def _accounting(graph, cid):
    return next(a for a in graph.capture_accounting if a.capture_occurrence_id == cid)


# --- 形态甲：单终止 assistant（PREFIX_ROOTED，53%）------------------------


def test_form_jia_single_terminal_text_is_prefix_rooted_complete():
    cap = _cap(
        "cap-jia",
        [_ev("A", EventKind.ASSISTANT_MESSAGE, 10, text_len=42)],
        prefix_counts={"USER": 3, "ASSISTANT_MESSAGE": 1},
    )
    graph = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=[cap])

    turns = _turns_by_ordinal(graph, "cap-jia")
    assert len(turns) == 1
    turn = turns[0]
    assert turn.root_status == RootStatus.PREFIX_ROOTED
    assert turn.user_block_id is None
    assert turn.turn_status == TurnStatus.COMPLETE
    assert len(turn.agent_step_ids) == 1
    assert turn.assistant_outcome_id is not None
    assert graph.assistant_outcomes[0].outcome_kind == "TEXT_OUTCOME"

    acc = _accounting(graph, "cap-jia")
    assert acc.has_unlocalizable_prefix is True
    assert acc.prefix_rooted_turn_count == 1
    assert acc.observed_rooted_turn_count == 0
    assert acc.observed_event_counts_by_kind == {"ASSISTANT_MESSAGE": 1}


def test_form_jia_single_terminal_with_unresolved_call_is_incomplete():
    cap = _cap(
        "cap-jia2",
        [
            _ev("A", EventKind.ASSISTANT_MESSAGE, 10),
            _ev("c1", EventKind.TOOL_CALL, 11, sub_index=0),
        ],
        batches=[_ab("A", ["c1"])],
        pairings=[],  # 无配对结果 → 调用未决
        terminal="TOOL_CALL_PENDING",
    )
    graph = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=[cap])

    turn = _turns_by_ordinal(graph, "cap-jia2")[0]
    assert turn.root_status == RootStatus.PREFIX_ROOTED
    assert turn.turn_status == TurnStatus.INCOMPLETE
    assert turn.assistant_outcome_id is None
    assert graph.assistant_outcome_count == 0
    step = _step_by_assistant(graph, "cap-jia2")["A"]
    assert step.unresolved_tool_call_event_ids == ("c1",)
    assert step.tool_observation_event_ids == ()
    assert step.parallel_semantics == "SEQUENTIAL"


# --- 形态乙：无 USER 的多步工具循环（属 80% 无根之列）---------------------


def test_form_yi_toolloop_no_user_is_single_prefix_rooted_turn_with_orphan():
    observed = [
        _ev("r0", EventKind.TOOL_RESULT, 5),  # 配对到前缀调用 → orphan
        _ev("A1", EventKind.ASSISTANT_MESSAGE, 10),
        _ev("c1", EventKind.TOOL_CALL, 11, sub_index=0),
        _ev("r1", EventKind.TOOL_RESULT, 20),
        _ev("A2", EventKind.ASSISTANT_MESSAGE, 30, text_len=88),
    ]
    cap = _cap(
        "cap-yi",
        observed,
        batches=[_ab("A1", ["c1"])],
        pairings=[
            ToolPairingLink(matched_call_event_id="prefix-call", matched_result_event_id="r0"),
            ToolPairingLink(matched_call_event_id="c1", matched_result_event_id="r1"),
        ],
        prefix_counts={"USER": 1},
    )
    graph = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=[cap])

    turns = _turns_by_ordinal(graph, "cap-yi")
    assert len(turns) == 1
    turn = turns[0]
    assert turn.root_status == RootStatus.PREFIX_ROOTED
    steps = _step_by_assistant(graph, "cap-yi")
    assert list(turn.agent_step_ids) == [steps["A1"].agent_step_id, steps["A2"].agent_step_id]
    # r1 按配对归 A1（不按位置）
    assert steps["A1"].tool_observation_event_ids == ("r1",)
    assert steps["A2"].tool_observation_event_ids == ()
    # A2 文本无 batch → COMPLETE
    assert turn.turn_status == TurnStatus.COMPLETE
    acc = _accounting(graph, "cap-yi")
    assert acc.orphan_tool_observation_event_ids == ("r0",)


# --- 形态丙：含可定位 USER 的中途插话（实测 216 例）-----------------------


def test_form_bing_mid_loop_user_interjection_splits_cleanly():
    observed = [
        _ev("A1", EventKind.ASSISTANT_MESSAGE, 10),
        _ev("c1", EventKind.TOOL_CALL, 11, sub_index=0),
        _ev("r1", EventKind.TOOL_RESULT, 20),
        _ev("U", EventKind.USER, 25, msg_index=7),
        _ev("A2", EventKind.ASSISTANT_MESSAGE, 30, text_len=55),
    ]
    cap = _cap(
        "cap-bing",
        observed,
        batches=[_ab("A1", ["c1"])],
        pairings=[ToolPairingLink(matched_call_event_id="c1", matched_result_event_id="r1")],
    )
    graph = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=[cap])

    turns = _turns_by_ordinal(graph, "cap-bing")
    assert len(turns) == 2
    t0, t1 = turns[0], turns[1]
    steps = _step_by_assistant(graph, "cap-bing")

    # T0 前缀根，含 A1;插话前的 r1 正确归 A1
    assert t0.root_status == RootStatus.PREFIX_ROOTED
    assert list(t0.agent_step_ids) == [steps["A1"].agent_step_id]
    assert steps["A1"].tool_observation_event_ids == ("r1",)
    # A1 末步有 batch → T0 INCOMPLETE、无 outcome
    assert t0.turn_status == TurnStatus.INCOMPLETE
    assert t0.assistant_outcome_id is None

    # T1 由 USER 起头，OBSERVED_ROOTED，含 A2（文本）→ COMPLETE
    assert t1.root_status == RootStatus.OBSERVED_ROOTED
    assert t1.user_block_id is not None
    assert list(t1.agent_step_ids) == [steps["A2"].agent_step_id]
    assert t1.turn_status == TurnStatus.COMPLETE

    # 结构边 T0 → T1
    assert graph.thread_turn_edge_count == 1
    edge = graph.thread_turn_edges[0]
    assert edge.relation == "STRUCTURAL_NEXT_TURN"
    assert edge.parent_query_turn_id == t0.query_turn_id
    assert edge.child_query_turn_id == t1.query_turn_id

    ub = graph.user_blocks[0]
    assert ub.event_ids == ("U",)
    assert ub.message_index_start == 7 and ub.message_index_end == 7


# --- 连续 USER 收敛成一个 UserBlock -----------------------------------------


def test_consecutive_users_form_single_block():
    observed = [
        _ev("U1", EventKind.USER, 10, msg_index=1),
        _ev("U2", EventKind.USER, 11, msg_index=2, has_blocks=True),
        _ev("A", EventKind.ASSISTANT_MESSAGE, 12, text_len=5),
    ]
    graph = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=[_cap("cap-uu", observed)])
    assert graph.user_block_count == 1
    ub = graph.user_blocks[0]
    assert ub.event_ids == ("U1", "U2")
    assert ub.message_index_start == 1 and ub.message_index_end == 2
    assert ub.has_non_text_blocks is True
    turns = _turns_by_ordinal(graph, "cap-uu")
    assert len(turns) == 1 and turns[0].root_status == RootStatus.OBSERVED_ROOTED


# --- SYSTEM 在窗口内不起回合、断开相邻 USER --------------------------------


def test_system_event_splits_user_blocks_and_starts_no_turn():
    observed = [
        _ev("U1", EventKind.USER, 10, msg_index=1),
        _ev("S", EventKind.SYSTEM, 11, msg_index=2),
        _ev("U2", EventKind.USER, 12, msg_index=3),
        _ev("A", EventKind.ASSISTANT_MESSAGE, 13, text_len=9),
    ]
    graph = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=[_cap("cap-sys", observed)])
    assert graph.user_block_count == 2
    turns = _turns_by_ordinal(graph, "cap-sys")
    assert len(turns) == 2
    # T0 只有 UserBlock、无 agent step → INCOMPLETE、无 outcome
    assert turns[0].root_status == RootStatus.OBSERVED_ROOTED
    assert turns[0].agent_step_ids == ()
    assert turns[0].turn_status == TurnStatus.INCOMPLETE
    assert turns[0].assistant_outcome_id is None
    # T1 含 A（文本）→ COMPLETE
    assert turns[1].turn_status == TurnStatus.COMPLETE
    acc = _accounting(graph, "cap-sys")
    assert acc.observed_event_counts_by_kind == {
        "ASSISTANT_MESSAGE": 1,
        "SYSTEM": 1,
        "USER": 2,
    }


# --- EMPTY_OUTCOME：末步无 batch、content 空 --------------------------------


def test_empty_content_terminal_is_empty_outcome_incomplete():
    observed = [_ev("A", EventKind.ASSISTANT_MESSAGE, 10, text_len=0)]
    graph = build_query_turn_graph(
        m1b_run_id=M1B_RUN_ID,
        captures=[_cap("cap-empty", observed, terminal="EMPTY_OUTCOME")],
    )
    turn = _turns_by_ordinal(graph, "cap-empty")[0]
    assert turn.turn_status == TurnStatus.INCOMPLETE
    assert graph.assistant_outcomes[0].outcome_kind == "EMPTY_OUTCOME"
    assert turn.assistant_outcome_id is not None  # EMPTY 仍产 outcome（末步无 batch）


# --- content blocks（无文本、有块）→ TEXT_OUTCOME ---------------------------


def test_content_blocks_present_is_text_outcome():
    observed = [_ev("A", EventKind.ASSISTANT_MESSAGE, 10, block_count=2)]
    graph = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=[_cap("cap-blk", observed)])
    turn = _turns_by_ordinal(graph, "cap-blk")[0]
    assert turn.turn_status == TurnStatus.COMPLETE
    assert graph.assistant_outcomes[0].outcome_kind == "TEXT_OUTCOME"


# --- 规格 D-a：末步有 batch、结果**全到**仍 INCOMPLETE（本回合未合成答案）------


def test_all_results_arrived_but_last_step_has_batch_is_incomplete():
    observed = [
        _ev("U", EventKind.USER, 5, msg_index=1),
        _ev("A", EventKind.ASSISTANT_MESSAGE, 10),
        _ev("c1", EventKind.TOOL_CALL, 11, sub_index=0),
        _ev("c2", EventKind.TOOL_CALL, 12, sub_index=1),
        _ev("r1", EventKind.TOOL_RESULT, 20),
        _ev("r2", EventKind.TOOL_RESULT, 21),
    ]
    cap = _cap(
        "cap-da",
        observed,
        batches=[_ab("A", ["c1", "c2"], semantics="PARALLEL")],
        pairings=[ToolPairingLink("c1", "r1"), ToolPairingLink("c2", "r2")],
        terminal="TOOL_CALL_PENDING",
    )
    graph = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=[cap])

    step = _step_by_assistant(graph, "cap-da")["A"]
    assert step.tool_observation_event_ids == ("r1", "r2")
    assert step.unresolved_tool_call_event_ids == ()
    turn = _turns_by_ordinal(graph, "cap-da")[0]
    assert turn.root_status == RootStatus.OBSERVED_ROOTED
    assert turn.turn_status == TurnStatus.INCOMPLETE
    assert turn.assistant_outcome_id is None
    assert graph.assistant_outcome_count == 0
    assert _accounting(graph, "cap-da").orphan_tool_observation_event_ids == ()


# --- 空观测流（合法 M1B 输入上不可达，防御性固定行为）-------------------------


def test_empty_observed_stream_yields_accounting_only():
    cap = _cap("cap-empty-window", [], prefix_counts={"USER": 2, "ASSISTANT_MESSAGE": 2})
    graph = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=[cap])

    assert graph.capture_count == 1
    assert graph.query_turn_count == 0
    assert graph.user_block_count == graph.agent_step_count == 0
    assert graph.assistant_outcome_count == graph.thread_turn_edge_count == 0
    acc = _accounting(graph, "cap-empty-window")
    assert acc.has_unlocalizable_prefix is True
    assert acc.observed_event_counts_by_kind == {}
    assert acc.query_turn_count == 0
    assert graph.captures_with_unlocalizable_prefix_count == 1


# --- 观测事件分区守恒（规格 §8）：fold 产物恰好划分观测窗口 ---------------------


def _partition_claims(graph: QueryTurnGraph, cap: CaptureInput) -> list[str]:
    """枚举 fold 产物对观测事件的**全部**归属声明（保留重复，供检两两不交）。"""

    cid = cap.capture_occurrence_id
    claims: list[str] = []
    for block in graph.user_blocks:
        if block.capture_occurrence_id == cid:
            claims.extend(block.event_ids)
    for step in graph.agent_steps:
        if step.capture_occurrence_id != cid:
            continue
        claims.append(step.assistant_event_id)
        claims.extend(step.tool_observation_event_ids)
        batch = cap.action_batch_by_assistant.get(step.assistant_event_id)
        if batch is not None:
            claims.extend(batch.tool_call_event_ids)
    claims.extend(_accounting(graph, cid).orphan_tool_observation_event_ids)
    claims.extend(e.event_id for e in cap.observed_events if e.event_kind == EventKind.SYSTEM)
    return claims


def _all_forms() -> list[CaptureInput]:
    return [
        _cap("p-jia", [_ev("A", EventKind.ASSISTANT_MESSAGE, 10, text_len=42)]),
        _cap(
            "p-yi",
            [
                _ev("r0", EventKind.TOOL_RESULT, 5),
                _ev("A1", EventKind.ASSISTANT_MESSAGE, 10),
                _ev("c1", EventKind.TOOL_CALL, 11, sub_index=0),
                _ev("r1", EventKind.TOOL_RESULT, 20),
                _ev("A2", EventKind.ASSISTANT_MESSAGE, 30, text_len=88),
            ],
            batches=[_ab("A1", ["c1"])],
            pairings=[ToolPairingLink("prefix-call", "r0"), ToolPairingLink("c1", "r1")],
        ),
        _cap(
            "p-bing",
            [
                _ev("A1", EventKind.ASSISTANT_MESSAGE, 10),
                _ev("c1", EventKind.TOOL_CALL, 11, sub_index=0),
                _ev("c2", EventKind.TOOL_CALL, 12, sub_index=1),
                _ev("r1", EventKind.TOOL_RESULT, 20),
                _ev("U", EventKind.USER, 25, msg_index=7),
                _ev("A2", EventKind.ASSISTANT_MESSAGE, 30, text_len=55),
            ],
            batches=[_ab("A1", ["c1", "c2"])],
            pairings=[ToolPairingLink("c1", "r1")],  # c2 未决
        ),
        _cap(
            "p-sys",
            [
                _ev("U1", EventKind.USER, 10, msg_index=1),
                _ev("S", EventKind.SYSTEM, 11, msg_index=2),
                _ev("U2", EventKind.USER, 12, msg_index=3),
                _ev("A", EventKind.ASSISTANT_MESSAGE, 13, text_len=9),
                _ev("rx", EventKind.TOOL_RESULT, 14),  # 无配对 → orphan
            ],
        ),
    ]


@pytest.mark.parametrize("cap", _all_forms(), ids=lambda c: c.capture_occurrence_id)
def test_observed_events_are_exactly_partitioned(cap: CaptureInput):
    graph = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=[cap])
    claims = _partition_claims(graph, cap)

    assert len(claims) == len(set(claims)), "同一观测事件被覆盖两次"
    assert set(claims) == {e.event_id for e in cap.observed_events}, "有遗漏或越界引用"
    acc = _accounting(graph, cap.capture_occurrence_id)
    assert sum(acc.observed_event_counts_by_kind.values()) == len(cap.observed_events)


# --- 确定性：两次构建逐字段一致 --------------------------------------------


def test_build_is_deterministic():
    def make():
        return [
            _cap(
                "cap-b",
                [
                    _ev("A1", EventKind.ASSISTANT_MESSAGE, 10),
                    _ev("c1", EventKind.TOOL_CALL, 11, sub_index=0),
                    _ev("r1", EventKind.TOOL_RESULT, 20),
                    _ev("U", EventKind.USER, 25, msg_index=3),
                    _ev("A2", EventKind.ASSISTANT_MESSAGE, 30, text_len=7),
                ],
                batches=[_ab("A1", ["c1"])],
                pairings=[ToolPairingLink("c1", "r1")],
            ),
            _cap("cap-a", [_ev("A", EventKind.ASSISTANT_MESSAGE, 10, text_len=3)]),
        ]

    g1 = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=make())
    g2 = build_query_turn_graph(m1b_run_id=M1B_RUN_ID, captures=make())
    assert [t.to_dict() for t in g1.query_turns] == [t.to_dict() for t in g2.query_turns]
    assert [s.to_dict() for s in g1.agent_steps] == [s.to_dict() for s in g2.agent_steps]
    assert [e.to_dict() for e in g1.thread_turn_edges] == [
        e.to_dict() for e in g2.thread_turn_edges
    ]
    assert [a.to_dict() for a in g1.capture_accounting] == [
        a.to_dict() for a in g2.capture_accounting
    ]
