"""M1D builder 的冻结输入投影（reader 产出 / builder 消费的边界契约）。

纯 dataclass、无 IO。把已发布 M1B run 抽取成 builder 需要的最小结构：按 ``sequence_number``
升序的**可观测事件流** + ActionBatch 索引 + ToolPairing 严格 1:1 匹配对 + 前缀按 kind 记账
+ capture 级质量标量。

content 空/非空所需的**冻结标量**（``text_utf8_byte_length`` / ``block_count`` /
``has_non_text_blocks``）由 reader 经 `event_payload.py` typed reader 预抽取到
``ObservedEvent``——builder 不再触碰 payload（规格 §1 勘误、§3 门③、§4 步 4）。ToolObservation
的**按配对归属**（gate④）逻辑留在 builder（validator 独立复算），故此处只透传 pairing 的
matched 对，不预判归属。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObservedEvent:
    """观测窗口内一条事件（``event_scope=OBSERVED_REQUEST_WINDOW``）的最小投影。

    ``text_utf8_byte_length`` / ``block_count`` 为 content payload 的冻结标量（TEXT 时前者
    有值、CONTENT_BLOCKS 时后者有值，其余 None）；``has_non_text_blocks`` 为 content payload
    的 ``CONTENT_BLOCKS`` 种类位（规格 §3 D-b，不细分块类型）。SYSTEM/TOOL_CALL 等无 content
    语义的事件三者取 None/False。
    """

    event_id: str
    event_kind: str
    sequence_number: int
    request_boundary_id: str | None
    message_index: int
    sub_index: int | None
    text_utf8_byte_length: int | None
    block_count: int | None
    has_non_text_blocks: bool


@dataclass(frozen=True, slots=True)
class ActionBatchInfo:
    """一个观测 assistant 事件的 ActionBatch 投影（规格 §4 步 1）。"""

    action_batch_id: str
    assistant_event_id: str
    tool_call_event_ids: tuple[str, ...]
    execution_semantics: str


@dataclass(frozen=True, slots=True)
class ToolPairingLink:
    """ToolPairingRecordV3 的严格 1:1 匹配对投影（仅 matched 字段；规格 §4 步 1/步 2）。

    M1B 只在严格 1:1 时填 ``matched_*``；未匹配调用两者可为 None。builder 据此把结果按
    ``matched_call_event_id`` 归到调用所在 AgentStep（观测结果）或判为 orphan（调用在前缀）。
    """

    matched_call_event_id: str | None
    matched_result_event_id: str | None


@dataclass(frozen=True, slots=True)
class CaptureInput:
    """单个 capture 的 builder 输入投影（规格 §4，逐 capture 纯函数）。

    只含 M1B 已编译 capture（M1B validator 已断言其 ``processing_status`` 恒 ``COMPLETE``，
    quarantined 记录不产 capture 行与事件），故不携带 processing_status。
    """

    capture_occurrence_id: str
    terminal_status: str
    has_compaction: bool
    observed_events: tuple[ObservedEvent, ...]
    prefix_event_counts_by_kind: dict[str, int]
    action_batch_by_assistant: dict[str, ActionBatchInfo]
    tool_pairings: tuple[ToolPairingLink, ...]


@dataclass(frozen=True, slots=True)
class M1bTurnView:
    """reader 产出的整 run 只读投影（内容寻址身份 + 逐 capture 输入）。"""

    m1b_run_id: str
    m1b_artifact_manifest_sha256: str
    source_schema: str
    captures: tuple[CaptureInput, ...]
