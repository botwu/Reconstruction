"""M1D 结构型回合派生的纯函数内核（无 IO；规格 §4）。

在"按 ``sequence_number`` 升序的可观测事件流"上工作，boundary 只作证据锚点、不作切分单元。
逐 capture 折叠成 AgentStep／UserBlock／AssistantOutcome／QueryTurn／结构边，并对不可定位
前缀与孤儿观测显式记账。所有产出按稳定 ID 排序，保证逐字节可复现。零模型调用、零语义关系。

ToolObservation 的**按配对归属**（gate④）在此实现：结果归属其 ``tool_call_id`` 严格 1:1 配对
的调用所在 AgentStep（跨 boundary 亦然），绝不用到达顺序反推；配对到前缀调用或未配对的观测
结果判为 orphan。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from traceforge.trajectory.contracts import EventKind

from .contracts import (
    AGENT_STEP_SCHEMA,
    ASSISTANT_OUTCOME_SCHEMA,
    CAPTURE_TURN_ACCOUNTING_SCHEMA,
    QUERY_TURN_SCHEMA,
    THREAD_TURN_EDGE_SCHEMA,
    USER_BLOCK_SCHEMA,
    AgentStepV1,
    AssistantOutcomeV1,
    CaptureTurnAccountingV1,
    QueryTurnV1,
    RootStatus,
    ThreadTurnEdgeV1,
    ThreadTurnRelation,
    TurnStatus,
    UserBlockV1,
    agent_step_id,
    assistant_outcome_id,
    content_is_present,
    derive_outcome_kind,
    derive_turn_status,
    query_turn_id,
    thread_turn_edge_id,
    user_block_id,
)
from .view import CaptureInput, ObservedEvent


@dataclass(frozen=True, slots=True)
class QueryTurnGraph:
    """builder 的内存输出（pipeline 据此写盘）。计数供报告聚合。"""

    user_blocks: tuple[UserBlockV1, ...]
    agent_steps: tuple[AgentStepV1, ...]
    assistant_outcomes: tuple[AssistantOutcomeV1, ...]
    query_turns: tuple[QueryTurnV1, ...]
    capture_accounting: tuple[CaptureTurnAccountingV1, ...]
    thread_turn_edges: tuple[ThreadTurnEdgeV1, ...]
    capture_count: int
    query_turn_count: int
    prefix_rooted_turn_count: int
    observed_rooted_turn_count: int
    complete_turn_count: int
    incomplete_turn_count: int
    agent_step_count: int
    user_block_count: int
    assistant_outcome_count: int
    thread_turn_edge_count: int
    orphan_tool_observation_count: int
    captures_with_unlocalizable_prefix_count: int
    captures_with_compaction_count: int


@dataclass(slots=True)
class _TurnAcc:
    """capture 内构建中的可变回合累加器（私有，最后固化成 QueryTurnV1）。"""

    turn_ordinal: int
    root_status: RootStatus
    user_block_id: str | None
    agent_step_ids: list[str] = field(default_factory=list)
    boundary_ids: list[str] = field(default_factory=list)
    last_assistant: ObservedEvent | None = None

    def add_boundary(self, boundary_id: str | None) -> None:
        if boundary_id is not None and boundary_id not in self.boundary_ids:
            self.boundary_ids.append(boundary_id)


def build_query_turn_graph(*, m1b_run_id: str, captures: Sequence[CaptureInput]) -> QueryTurnGraph:
    """把逐 capture 的可观测输入折叠成结构型回合图（规格 §4，纯函数）。"""

    user_blocks: list[UserBlockV1] = []
    agent_steps: list[AgentStepV1] = []
    assistant_outcomes: list[AssistantOutcomeV1] = []
    query_turns: list[QueryTurnV1] = []
    capture_accounting: list[CaptureTurnAccountingV1] = []
    thread_turn_edges: list[ThreadTurnEdgeV1] = []

    complete_turns = 0
    incomplete_turns = 0
    prefix_rooted_turns = 0
    observed_rooted_turns = 0
    orphan_total = 0
    with_prefix = 0
    with_compaction = 0

    # capture 迭代序固定（不影响 turn_ordinal，仅保证确定性）。输入全集 = M1B 已编译 capture，
    # 无 eligibility 分级（quarantine 在 M1B 就没有 capture 行，规格 §6）。
    for capture in sorted(captures, key=lambda c: c.capture_occurrence_id):
        result = _build_capture(m1b_run_id=m1b_run_id, capture=capture)
        user_blocks.extend(result.user_blocks)
        agent_steps.extend(result.agent_steps)
        assistant_outcomes.extend(result.assistant_outcomes)
        query_turns.extend(result.query_turns)
        capture_accounting.append(result.accounting)
        thread_turn_edges.extend(result.edges)

        complete_turns += result.complete_turn_count
        incomplete_turns += result.incomplete_turn_count
        prefix_rooted_turns += result.accounting.prefix_rooted_turn_count
        observed_rooted_turns += result.accounting.observed_rooted_turn_count
        orphan_total += len(result.accounting.orphan_tool_observation_event_ids)
        if result.accounting.has_unlocalizable_prefix:
            with_prefix += 1
        if result.accounting.has_compaction:
            with_compaction += 1

    query_turns_total = complete_turns + incomplete_turns
    return QueryTurnGraph(
        user_blocks=tuple(sorted(user_blocks, key=lambda x: x.user_block_id)),
        agent_steps=tuple(sorted(agent_steps, key=lambda x: x.agent_step_id)),
        assistant_outcomes=tuple(sorted(assistant_outcomes, key=lambda x: x.assistant_outcome_id)),
        query_turns=tuple(sorted(query_turns, key=lambda x: x.query_turn_id)),
        capture_accounting=tuple(sorted(capture_accounting, key=lambda x: x.capture_occurrence_id)),
        thread_turn_edges=tuple(sorted(thread_turn_edges, key=lambda x: x.edge_id)),
        capture_count=len(captures),
        query_turn_count=query_turns_total,
        prefix_rooted_turn_count=prefix_rooted_turns,
        observed_rooted_turn_count=observed_rooted_turns,
        complete_turn_count=complete_turns,
        incomplete_turn_count=incomplete_turns,
        agent_step_count=len(agent_steps),
        user_block_count=len(user_blocks),
        assistant_outcome_count=len(assistant_outcomes),
        thread_turn_edge_count=len(thread_turn_edges),
        orphan_tool_observation_count=orphan_total,
        captures_with_unlocalizable_prefix_count=with_prefix,
        captures_with_compaction_count=with_compaction,
    )


@dataclass(frozen=True, slots=True)
class _CaptureResult:
    user_blocks: tuple[UserBlockV1, ...]
    agent_steps: tuple[AgentStepV1, ...]
    assistant_outcomes: tuple[AssistantOutcomeV1, ...]
    query_turns: tuple[QueryTurnV1, ...]
    edges: tuple[ThreadTurnEdgeV1, ...]
    accounting: CaptureTurnAccountingV1
    complete_turn_count: int
    incomplete_turn_count: int


def _build_capture(*, m1b_run_id: str, capture: CaptureInput) -> _CaptureResult:
    cid = capture.capture_occurrence_id
    observed = sorted(capture.observed_events, key=lambda e: e.sequence_number)
    observed_ids = {e.event_id for e in observed}

    # 配对索引（仅严格 1:1 的 matched 对进入，规格 §4 步 1/步 2）。
    result_by_call: dict[str, str] = {}
    call_by_result: dict[str, str] = {}
    for pairing in capture.tool_pairings:
        if pairing.matched_call_event_id is None or pairing.matched_result_event_id is None:
            continue
        result_by_call[pairing.matched_call_event_id] = pairing.matched_result_event_id
        call_by_result[pairing.matched_result_event_id] = pairing.matched_call_event_id

    seq_by_id = {e.event_id: e.sequence_number for e in observed}

    # --- 步 1：建 AgentStep（键=assistant 事件，与 boundary 无关）---
    step_by_assistant: dict[str, AgentStepV1] = {}
    for event in observed:
        if event.event_kind != EventKind.ASSISTANT_MESSAGE:
            continue
        batch = capture.action_batch_by_assistant.get(event.event_id)
        observation_ids: list[str] = []
        unresolved: list[str] = []
        action_batch_id: str | None = None
        parallel_semantics: str | None = None
        if batch is not None:
            action_batch_id = batch.action_batch_id
            parallel_semantics = batch.execution_semantics
            for call_event_id in batch.tool_call_event_ids:
                result_event_id = result_by_call.get(call_event_id)
                if result_event_id is not None and result_event_id in observed_ids:
                    observation_ids.append(result_event_id)
                else:
                    unresolved.append(call_event_id)
        observation_ids.sort(key=lambda rid: seq_by_id.get(rid, 0))
        step = AgentStepV1(
            schema_version=AGENT_STEP_SCHEMA,
            agent_step_id=agent_step_id(
                m1b_run_id=m1b_run_id,
                capture_occurrence_id=cid,
                assistant_event_id=event.event_id,
            ),
            capture_occurrence_id=cid,
            request_boundary_id=event.request_boundary_id,
            assistant_event_id=event.event_id,
            action_batch_id=action_batch_id,
            tool_observation_event_ids=tuple(observation_ids),
            unresolved_tool_call_event_ids=tuple(unresolved),
            parallel_semantics=parallel_semantics,
        )
        step_by_assistant[event.event_id] = step

    # --- 步 2：收孤儿观测（配对到前缀调用或无配对的观测 TOOL_RESULT）---
    orphan_ids: list[str] = []
    for event in observed:
        if event.event_kind != EventKind.TOOL_RESULT:
            continue
        matched_call = call_by_result.get(event.event_id)
        if matched_call is None or matched_call not in observed_ids:
            orphan_ids.append(event.event_id)

    # --- 步 3：切 QueryTurn（键=可定位 USER）---
    turns: list[_TurnAcc] = []
    turn_user_block: dict[int, UserBlockV1] = {}
    current: _TurnAcc | None = None
    index = 0
    total = len(observed)
    while index < total:
        event = observed[index]
        if event.event_kind == EventKind.USER:
            block_event_ids: list[str] = []
            message_start = event.message_index
            message_end = event.message_index
            has_non_text = False
            cursor = index
            while cursor < total and observed[cursor].event_kind == EventKind.USER:
                member = observed[cursor]
                block_event_ids.append(member.event_id)
                message_end = member.message_index
                has_non_text = has_non_text or member.has_non_text_blocks
                cursor += 1
            block = UserBlockV1(
                schema_version=USER_BLOCK_SCHEMA,
                user_block_id=user_block_id(
                    m1b_run_id=m1b_run_id,
                    capture_occurrence_id=cid,
                    event_ids=block_event_ids,
                ),
                capture_occurrence_id=cid,
                request_boundary_id=event.request_boundary_id,
                event_ids=tuple(block_event_ids),
                message_index_start=message_start,
                message_index_end=message_end,
                has_non_text_blocks=has_non_text,
            )
            current = _TurnAcc(
                turn_ordinal=len(turns),
                root_status=RootStatus.OBSERVED_ROOTED,
                user_block_id=block.user_block_id,
            )
            current.add_boundary(event.request_boundary_id)
            turns.append(current)
            turn_user_block[current.turn_ordinal] = block
            index = cursor
            continue
        if event.event_kind == EventKind.ASSISTANT_MESSAGE:
            if current is None:
                current = _TurnAcc(
                    turn_ordinal=len(turns),
                    root_status=RootStatus.PREFIX_ROOTED,
                    user_block_id=None,
                )
                turns.append(current)
            step = step_by_assistant[event.event_id]
            current.agent_step_ids.append(step.agent_step_id)
            current.add_boundary(event.request_boundary_id)
            current.last_assistant = event
            index += 1
            continue
        # SYSTEM / TOOL_CALL / TOOL_RESULT：不切分回合（归属已由步 1/2 按配对确定）。
        index += 1

    # --- 步 4：定 turn_status 与 AssistantOutcome ---
    finalized_turns: list[QueryTurnV1] = []
    finalized_outcomes: list[AssistantOutcomeV1] = []
    complete_count = 0
    for acc in turns:
        outcome_id: str | None = None
        last = acc.last_assistant
        if last is None:
            turn_status = TurnStatus.INCOMPLETE
        else:
            last_step = step_by_assistant[last.event_id]
            last_has_batch = last_step.action_batch_id is not None
            if last_has_batch:
                turn_status = derive_turn_status(last_step_has_action_batch=True, outcome_kind=None)
            else:
                present = content_is_present(
                    text_utf8_byte_length=last.text_utf8_byte_length,
                    block_count=last.block_count,
                )
                outcome_kind = derive_outcome_kind(content_present=present)
                outcome = AssistantOutcomeV1(
                    schema_version=ASSISTANT_OUTCOME_SCHEMA,
                    assistant_outcome_id=assistant_outcome_id(
                        m1b_run_id=m1b_run_id,
                        capture_occurrence_id=cid,
                        terminal_assistant_event_id=last.event_id,
                    ),
                    capture_occurrence_id=cid,
                    terminal_assistant_event_id=last.event_id,
                    outcome_kind=outcome_kind.value,
                )
                finalized_outcomes.append(outcome)
                outcome_id = outcome.assistant_outcome_id
                turn_status = derive_turn_status(
                    last_step_has_action_batch=False, outcome_kind=outcome_kind
                )
        if turn_status == TurnStatus.COMPLETE:
            complete_count += 1
        finalized_turns.append(
            QueryTurnV1(
                schema_version=QUERY_TURN_SCHEMA,
                query_turn_id=query_turn_id(
                    m1b_run_id=m1b_run_id,
                    capture_occurrence_id=cid,
                    turn_ordinal=acc.turn_ordinal,
                ),
                capture_occurrence_id=cid,
                turn_ordinal=acc.turn_ordinal,
                root_status=acc.root_status.value,
                user_block_id=acc.user_block_id,
                agent_step_ids=tuple(acc.agent_step_ids),
                assistant_outcome_id=outcome_id,
                turn_status=turn_status.value,
                boundary_ids_spanned=tuple(acc.boundary_ids),
            )
        )

    # --- 步 5：capture 内结构边（相邻 turn_ordinal）---
    edges: list[ThreadTurnEdgeV1] = []
    for position in range(len(finalized_turns) - 1):
        parent = finalized_turns[position]
        child = finalized_turns[position + 1]
        edges.append(
            ThreadTurnEdgeV1(
                schema_version=THREAD_TURN_EDGE_SCHEMA,
                edge_id=thread_turn_edge_id(
                    m1b_run_id=m1b_run_id,
                    parent_query_turn_id=parent.query_turn_id,
                    child_query_turn_id=child.query_turn_id,
                ),
                relation=ThreadTurnRelation.STRUCTURAL_NEXT_TURN.value,
                parent_query_turn_id=parent.query_turn_id,
                child_query_turn_id=child.query_turn_id,
                evidence={
                    "parent_turn_ordinal": parent.turn_ordinal,
                    "child_turn_ordinal": child.turn_ordinal,
                },
            )
        )

    # --- 步 6：记账 ---
    observed_counts: dict[str, int] = {}
    for event in observed:
        observed_counts[event.event_kind] = observed_counts.get(event.event_kind, 0) + 1
    prefix_rooted = sum(1 for t in finalized_turns if t.root_status == RootStatus.PREFIX_ROOTED)
    observed_rooted = sum(1 for t in finalized_turns if t.root_status == RootStatus.OBSERVED_ROOTED)
    accounting = CaptureTurnAccountingV1(
        schema_version=CAPTURE_TURN_ACCOUNTING_SCHEMA,
        capture_occurrence_id=cid,
        has_unlocalizable_prefix=sum(capture.prefix_event_counts_by_kind.values()) > 0,
        prefix_event_counts_by_kind=dict(sorted(capture.prefix_event_counts_by_kind.items())),
        observed_event_counts_by_kind=dict(sorted(observed_counts.items())),
        orphan_tool_observation_event_ids=tuple(orphan_ids),
        agent_step_count=len(step_by_assistant),
        query_turn_count=len(finalized_turns),
        prefix_rooted_turn_count=prefix_rooted,
        observed_rooted_turn_count=observed_rooted,
        has_compaction=capture.has_compaction,
    )

    return _CaptureResult(
        user_blocks=tuple(turn_user_block[ordinal] for ordinal in sorted(turn_user_block)),
        agent_steps=tuple(
            step_by_assistant[event.event_id]
            for event in observed
            if event.event_kind == EventKind.ASSISTANT_MESSAGE
        ),
        assistant_outcomes=tuple(finalized_outcomes),
        query_turns=tuple(finalized_turns),
        edges=tuple(edges),
        accounting=accounting,
        complete_turn_count=complete_count,
        incomplete_turn_count=len(finalized_turns) - complete_count,
    )
