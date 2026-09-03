"""M1D 结构型 QueryTurn 的稳定数据契约、稳定 ID 公式与固定派生函数。

本模块是 M1D 契约的唯一权威来源（[`../../../docs/m1d-processing-spec.md`] §2/§7，
[`AGENTS.md`] §1 DRY）：产物 dataclass、schema 常量、闭合枚举、稳定 ID 公式，以及
`turn_status`/`outcome_kind`/终止状态交叉核对等**固定函数**（builder 与独立 validator
都只从这里取用，不各自复刻）。

M1D 复用 `trajectory.json_codec` 的公开稳定 ID 内核，只在自有命名空间常量上独立取 ID，
**不 import M1B/M1C 私有派生公式**（compiler / pipeline / lineage 的私有 ID 公式，规格 §7）；
上游 ID 一律当不透明外键。content 空/非空一律经 `event_payload.py` typed reader 的**冻结标量**
判定（规格 §1 勘误、§4 步 4），本模块只提供其上的纯函数，不在此读取 payload。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from traceforge.trajectory.contracts import (
    SerializableContract,
    TerminalStatus,
)
from traceforge.trajectory.json_codec import stable_id

# --- 契约版本与 schema 常量 ------------------------------------------------

QUERY_TURN_CONTRACT_VERSION = "query-turn-compiler-m1d-v1"

QUERY_TURN_MANIFEST_SCHEMA = "traceforge.query-turn-manifest.v1"
QUERY_TURN_ARTIFACT_MANIFEST_SCHEMA = "traceforge.query-turn-artifact-manifest.v1"
QUERY_TURN_RUN_RECEIPT_SCHEMA = "traceforge.query-turn-run-receipt.v1"
QUERY_TURN_REPORT_SCHEMA = "traceforge.query-turn-report.v1"
USER_BLOCK_SCHEMA = "traceforge.query-turn-user-block.v1"
AGENT_STEP_SCHEMA = "traceforge.query-turn-agent-step.v1"
ASSISTANT_OUTCOME_SCHEMA = "traceforge.query-turn-assistant-outcome.v1"
QUERY_TURN_SCHEMA = "traceforge.query-turn.v1"
CAPTURE_TURN_ACCOUNTING_SCHEMA = "traceforge.query-turn-capture-accounting.v1"
THREAD_TURN_EDGE_SCHEMA = "traceforge.query-turn-thread-edge.v1"

# 稳定 ID 命名空间（规格 §7）。均为 M1D 自有命名空间，不复用 M1B/M1C 命名空间字符串。
QUERY_TURN_RUN_ID_NAMESPACE = "m1d-run-v1"
USER_BLOCK_ID_NAMESPACE = "m1d-user-block-v1"
AGENT_STEP_ID_NAMESPACE = "m1d-agent-step-v1"
ASSISTANT_OUTCOME_ID_NAMESPACE = "m1d-assistant-outcome-v1"
QUERY_TURN_ID_NAMESPACE = "m1d-query-turn-v1"
THREAD_TURN_EDGE_ID_NAMESPACE = "m1d-thread-turn-edge-v1"


# --- 闭合枚举 --------------------------------------------------------------


class RootStatus(StrEnum):
    """QueryTurn 的根可定位状态（规格 §2/§4 步 3）。

    ``PREFIX_ROOTED``：回合的用户根落在不可定位的 ``PRE_FIRST_OBSERVED_TERMINAL`` 前缀里
    （实测 80% capture 的观测窗口 0 个可定位 USER）——回合仍如实产出，但根不可定位由本状态
    显式暴露给下游。``OBSERVED_ROOTED``：回合由一个可观测的 UserBlock 起头。
    """

    PREFIX_ROOTED = "PREFIX_ROOTED"
    OBSERVED_ROOTED = "OBSERVED_ROOTED"


class TurnStatus(StrEnum):
    """回合完成度（规格 §4 步 4）。INCOMPLETE 回合保留（overall-plan §4.5）。"""

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class OutcomeKind(StrEnum):
    """AssistantOutcome 的结构派生类别（规格 §4 步 4）。"""

    TEXT_OUTCOME = "TEXT_OUTCOME"
    EMPTY_OUTCOME = "EMPTY_OUTCOME"


class ThreadTurnRelation(StrEnum):
    """M1D 只产结构性"下一回合"边。

    语义关系（CONTINUES/REFINES/…）全部留 M2（规格 §11），本枚举**不放任何占位成员**
    （[`AGENTS.md`] §1 YAGNI，比照 M1C 对 Grade-B 的处理）。
    """

    STRUCTURAL_NEXT_TURN = "STRUCTURAL_NEXT_TURN"


# --- 产物 dataclass --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserBlockV1(SerializableContract):
    """观测窗口内按 sequence 严格相邻的极大 USER 段（规格 §2/§4 步 3）。"""

    schema_version: str
    user_block_id: str
    capture_occurrence_id: str
    request_boundary_id: str | None
    event_ids: tuple[str, ...]
    message_index_start: int
    message_index_end: int
    has_non_text_blocks: bool


@dataclass(frozen=True, slots=True)
class AgentStepV1(SerializableContract):
    """键于单个观测 assistant 事件、与 boundary 无关的最小可定位单元（规格 §2/§4 步 1）。

    ``tool_observation_event_ids`` 为**按配对**归属的观测结果；``unresolved_tool_call_event_ids``
    为发出但无严格 1:1 观测结果的调用（中断判据）。``parallel_semantics`` 透传 ActionBatch 的
    ``execution_semantics``（无 ActionBatch 时为 ``None``，不臆造）。
    """

    schema_version: str
    agent_step_id: str
    capture_occurrence_id: str
    request_boundary_id: str | None
    assistant_event_id: str
    action_batch_id: str | None
    tool_observation_event_ids: tuple[str, ...]
    unresolved_tool_call_event_ids: tuple[str, ...]
    parallel_semantics: str | None


@dataclass(frozen=True, slots=True)
class AssistantOutcomeV1(SerializableContract):
    """回合末个非工具调用步的答复归约（规格 §2/§4 步 4）。

    ``outcome_kind`` 为 ``OutcomeKind`` 的**结构派生**值（由读取方按 §4 复算、validator 断言，
    不物化二义来源）。
    """

    schema_version: str
    assistant_outcome_id: str
    capture_occurrence_id: str
    terminal_assistant_event_id: str
    outcome_kind: str


@dataclass(frozen=True, slots=True)
class QueryTurnV1(SerializableContract):
    """AgentStep 之上"带根可定位状态"的诚实分组（规格 §2/§4 步 3-4）。"""

    schema_version: str
    query_turn_id: str
    capture_occurrence_id: str
    turn_ordinal: int
    root_status: str
    user_block_id: str | None
    agent_step_ids: tuple[str, ...]
    assistant_outcome_id: str | None
    turn_status: str
    boundary_ids_spanned: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CaptureTurnAccountingV1(SerializableContract):
    """每 capture 一条的诚实记账（规格 §2/§4 步 6，background-and-goals §5 诚实分母）。

    ``prefix_event_counts_by_kind`` / ``observed_event_counts_by_kind`` 是前缀与观测窗口按
    ``event_kind`` 的完整计数（诚实分母 + 守恒可验：观测事件恰被 UserBlock/AgentStep/孤儿观测/
    SYSTEM/TOOL_CALL 划分覆盖，validator 独立断言）。不透传 ``processing_status``：M1D 的输入
    全集是 M1B 已编译 capture，M1B validator 已断言其恒为 ``COMPLETE``（quarantine 只在 M1B
    attrition 记账，M1D 结构上看不到、也不声称检查过）。
    """

    schema_version: str
    capture_occurrence_id: str
    has_unlocalizable_prefix: bool
    prefix_event_counts_by_kind: dict[str, int]
    observed_event_counts_by_kind: dict[str, int]
    orphan_tool_observation_event_ids: tuple[str, ...]
    agent_step_count: int
    query_turn_count: int
    prefix_rooted_turn_count: int
    observed_rooted_turn_count: int
    has_compaction: bool


@dataclass(frozen=True, slots=True)
class ThreadTurnEdgeV1(SerializableContract):
    """capture 内相邻回合的结构边（规格 §2/§4 步 5）。``relation`` 恒 STRUCTURAL_NEXT_TURN。"""

    schema_version: str
    edge_id: str
    relation: str
    parent_query_turn_id: str
    child_query_turn_id: str
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QueryTurnManifestV1(SerializableContract):
    """输入身份绑定（内容寻址，非路径，规格 §7/§9）。"""

    schema_version: str
    query_turn_run_id: str
    query_turn_contract_version: str
    m1b_run_id: str
    m1b_artifact_manifest_sha256: str
    source_schema: str


@dataclass(frozen=True, slots=True)
class QueryTurnArtifactManifestV1(SerializableContract):
    """确定性业务文件清单；不含自身与 run_receipt.json。"""

    schema_version: str
    query_turn_run_id: str
    query_turn_contract_version: str
    m1b_run_id: str
    m1b_artifact_manifest_sha256: str
    files: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class QueryTurnReportV1(SerializableContract):
    """公共报告：仅固定聚合计数，无原始 ID/正文/路径/URL（规格 §3 门⑤）。"""

    schema_version: str
    query_turn_run_id: str
    m1b_run_id: str
    counts: dict[str, int]


# --- 稳定 ID 公式（规格 §7，单一权威）-------------------------------------


def query_turn_run_id(*, m1b_run_id: str, m1b_artifact_manifest_sha256: str) -> str:
    return stable_id(
        QUERY_TURN_RUN_ID_NAMESPACE,
        {
            "query_turn_contract_version": QUERY_TURN_CONTRACT_VERSION,
            "m1b_run_id": m1b_run_id,
            "m1b_artifact_manifest_sha256": m1b_artifact_manifest_sha256,
        },
    )


def agent_step_id(*, m1b_run_id: str, capture_occurrence_id: str, assistant_event_id: str) -> str:
    return stable_id(
        AGENT_STEP_ID_NAMESPACE,
        {
            "m1b_run_id": m1b_run_id,
            "capture_occurrence_id": capture_occurrence_id,
            "assistant_event_id": assistant_event_id,
        },
    )


def user_block_id(*, m1b_run_id: str, capture_occurrence_id: str, event_ids: Sequence[str]) -> str:
    return stable_id(
        USER_BLOCK_ID_NAMESPACE,
        {
            "m1b_run_id": m1b_run_id,
            "capture_occurrence_id": capture_occurrence_id,
            "event_ids": list(event_ids),
        },
    )


def query_turn_id(*, m1b_run_id: str, capture_occurrence_id: str, turn_ordinal: int) -> str:
    return stable_id(
        QUERY_TURN_ID_NAMESPACE,
        {
            "m1b_run_id": m1b_run_id,
            "capture_occurrence_id": capture_occurrence_id,
            "turn_ordinal": turn_ordinal,
        },
    )


def assistant_outcome_id(
    *, m1b_run_id: str, capture_occurrence_id: str, terminal_assistant_event_id: str
) -> str:
    return stable_id(
        ASSISTANT_OUTCOME_ID_NAMESPACE,
        {
            "m1b_run_id": m1b_run_id,
            "capture_occurrence_id": capture_occurrence_id,
            "terminal_assistant_event_id": terminal_assistant_event_id,
        },
    )


def thread_turn_edge_id(
    *, m1b_run_id: str, parent_query_turn_id: str, child_query_turn_id: str
) -> str:
    return stable_id(
        THREAD_TURN_EDGE_ID_NAMESPACE,
        {
            "m1b_run_id": m1b_run_id,
            "relation": ThreadTurnRelation.STRUCTURAL_NEXT_TURN.value,
            "parent_query_turn_id": parent_query_turn_id,
            "child_query_turn_id": child_query_turn_id,
        },
    )


# --- 固定派生函数（规格 §4 步 4，builder 与 validator 共用，DRY）-----------


def content_is_present(*, text_utf8_byte_length: int | None, block_count: int | None) -> bool:
    """content 非空判据（规格 §1 勘误、§4 步 4）。

    仅取 typed reader 的**冻结标量**：``TextContent.utf8_byte_length`` 或
    ``ContentBlocks.block_count``。不读文本值/块内。合法 Data URL envelope 的
    ``utf8_byte_length`` 按 §201 恒 >0，故无需特判。**绝不**用
    ``EventOccurrenceV3.visible_payload_envelope_utf8_byte_length``（可见 payload 经 canonical
    JSON 编码后的外壳字节数、恒 >0，§199），它不是 content 文本长度。
    """

    return (text_utf8_byte_length or 0) > 0 or (block_count or 0) > 0


def derive_outcome_kind(*, content_present: bool) -> OutcomeKind:
    """仅在末步**无 ActionBatch** 时调用：content 存在→TEXT，否则→EMPTY（规格 §4 步 4）。"""

    return OutcomeKind.TEXT_OUTCOME if content_present else OutcomeKind.EMPTY_OUTCOME


def derive_turn_status(
    *, last_step_has_action_batch: bool, outcome_kind: OutcomeKind | None
) -> TurnStatus:
    """回合完成度（规格 §4 步 4，镜像 M1B ``_terminal_status`` 的 tool_calls 优先约定）。

    末步是工具调用步（有 ActionBatch）→ 无 outcome、``INCOMPLETE``（统一覆盖"发了调用未回结果"
    与"结果全到但本回合未合成答案"）；否则 ``TEXT_OUTCOME→COMPLETE``，其余 ``INCOMPLETE``。
    """

    if last_step_has_action_batch:
        return TurnStatus.INCOMPLETE
    if outcome_kind is OutcomeKind.TEXT_OUTCOME:
        return TurnStatus.COMPLETE
    return TurnStatus.INCOMPLETE


def terminal_status_for_step(*, has_action_batch: bool, content_present: bool) -> TerminalStatus:
    """把某 assistant step 的结构映成 M1B ``_terminal_status`` 会赋予该消息的状态。

    tool_calls 优先：有 ActionBatch→``TOOL_CALL_PENDING``；否则 content 存在→``TEXT_OUTCOME``、
    空→``EMPTY_OUTCOME``。供 validator 对 **capture 末个 turn** 交叉核对 ``CaptureQualityV2.
    terminal_status``（规格 §4 步 4 的独立 oracle）。不产 ``INVALID``——那属"capture 末条非
    assistant"，由 validator 另判。
    """

    if has_action_batch:
        return TerminalStatus.TOOL_CALL_PENDING
    return TerminalStatus.TEXT_OUTCOME if content_present else TerminalStatus.EMPTY_OUTCOME


# --- 报告聚合计数（固定 allowlist，validator 断言闭合）---------------------

QUERY_TURN_COUNT_KEYS = frozenset(
    {
        "capture_count",
        "query_turn_count",
        "prefix_rooted_turn_count",
        "observed_rooted_turn_count",
        "complete_turn_count",
        "incomplete_turn_count",
        "agent_step_count",
        "user_block_count",
        "assistant_outcome_count",
        "thread_turn_edge_count",
        "orphan_tool_observation_count",
        "captures_with_unlocalizable_prefix_count",
        "captures_with_compaction_count",
    }
)


def build_report_counts(
    *,
    capture_count: int,
    query_turn_count: int,
    prefix_rooted_turn_count: int,
    observed_rooted_turn_count: int,
    complete_turn_count: int,
    incomplete_turn_count: int,
    agent_step_count: int,
    user_block_count: int,
    assistant_outcome_count: int,
    thread_turn_edge_count: int,
    orphan_tool_observation_count: int,
    captures_with_unlocalizable_prefix_count: int,
    captures_with_compaction_count: int,
) -> dict[str, int]:
    """按固定 allowlist 组装报告计数，键集恒等于 ``QUERY_TURN_COUNT_KEYS``，按键排序。

    ``capture_count`` 即 M1B 已编译 capture 数（M1D 全部处理，无 eligibility 分级；
    quarantine 分母属 M1B attrition report，不在此重复或伪称）。
    """

    counts = {
        "capture_count": capture_count,
        "query_turn_count": query_turn_count,
        "prefix_rooted_turn_count": prefix_rooted_turn_count,
        "observed_rooted_turn_count": observed_rooted_turn_count,
        "complete_turn_count": complete_turn_count,
        "incomplete_turn_count": incomplete_turn_count,
        "agent_step_count": agent_step_count,
        "user_block_count": user_block_count,
        "assistant_outcome_count": assistant_outcome_count,
        "thread_turn_edge_count": thread_turn_edge_count,
        "orphan_tool_observation_count": orphan_tool_observation_count,
        "captures_with_unlocalizable_prefix_count": captures_with_unlocalizable_prefix_count,
        "captures_with_compaction_count": captures_with_compaction_count,
    }
    return dict(sorted(counts.items()))
