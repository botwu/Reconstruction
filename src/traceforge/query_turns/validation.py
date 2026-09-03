"""对一个已发布 M1D QueryTurn run 做独立、可重算的验收（规格 §8）。

两层信任边界，各司其职（[`AGENTS.md`] §1 DRY/SRP；比照 M1B v4 R5「业务规则只有一个定义、
各方独立重建输入」）：

1. **篡改检测**——从上游 M1B 已发布字段重新读入（`load_m1b_turn_view`，内部先跑 M1B 权威
   `validate_compiled_run`），调用与 pipeline **同一个纯 fold** `builder.build_query_turn_graph`
   （无 IO、无私有公式）重建期望产物，与 M1D `private/` 做**双向集合相等**（canonical 字节比对）。
   首要威胁「悄悄改派生结果」——删产物、造幻影、改归属/状态/计数、同步重签——均被推翻。
   本模块**不复刻 fold**：复制同一算法对 fold 自身缺陷零检出（两份同 bug 必互相一致），只会把
   每次修改的维护面翻倍。
2. **正交不变量**——只用 M1B 视图 + M1D 已发布表、**不经 fold** 断言的守恒律，用于检出 fold
   自身的派生缺陷：观测事件被 UserBlock/AgentStep(assistant)/工具观测/孤儿/SYSTEM/TOOL_CALL(经
   batch) **恰好划分覆盖**（无遗漏、无重复、kind 相符、不引用前缀事件）；记账计数与 M1B 视图逐
   kind 对账；capture 末 assistant 的结构映射与 M1B 独立 oracle ``CaptureQualityV3.terminal_status``
   一致；公共报告计数由**已发布表**独立重算。

产物完整性（逐文件重哈希、manifest 自摘要、inventory、canonical、隐私摘要、控制文件无绝对路径）
用 `trajectory.run_validation` 的公共物理层原语核验。取双参 ``validate_query_turn_run(m1d_run_dir,
m1b_run_dir)``：M1D 只以 ``m1b_run_id`` + manifest sha256 内容寻址绑定 M1B（§7 禁存路径），故须同时
拿到上游 run 作 oracle；validator 先断言所给 M1B run 的身份与 M1D 声明一致，再据此复算。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceforge.query_turns.builder import QueryTurnGraph, build_query_turn_graph
from traceforge.query_turns.contracts import (
    AGENT_STEP_SCHEMA,
    ASSISTANT_OUTCOME_SCHEMA,
    CAPTURE_TURN_ACCOUNTING_SCHEMA,
    QUERY_TURN_ARTIFACT_MANIFEST_SCHEMA,
    QUERY_TURN_CONTRACT_VERSION,
    QUERY_TURN_COUNT_KEYS,
    QUERY_TURN_MANIFEST_SCHEMA,
    QUERY_TURN_REPORT_SCHEMA,
    QUERY_TURN_RUN_RECEIPT_SCHEMA,
    QUERY_TURN_SCHEMA,
    THREAD_TURN_EDGE_SCHEMA,
    USER_BLOCK_SCHEMA,
    AgentStepV1,
    AssistantOutcomeV1,
    CaptureTurnAccountingV1,
    QueryTurnArtifactManifestV1,
    QueryTurnManifestV1,
    QueryTurnReportV1,
    QueryTurnV1,
    RootStatus,
    ThreadTurnEdgeV1,
    TurnStatus,
    UserBlockV1,
    build_report_counts,
    content_is_present,
    query_turn_run_id,
    terminal_status_for_step,
)
from traceforge.query_turns.reader import QueryTurnInputError, load_m1b_turn_view
from traceforge.query_turns.view import M1bTurnView, ObservedEvent
from traceforge.trajectory.contracts import EventKind, TerminalStatus
from traceforge.trajectory.json_codec import canonical_json_bytes
from traceforge.trajectory.run_validation import (
    IssueCollector,
    ValidationIssue,
    bijection,
    check_artifact_bytes,
    check_contract,
    check_inventory,
    check_receipt,
    is_int,
    iter_canonical_jsonl,
    manifest_entries,
    read_json,
    scan_control_pathlike,
)


@dataclass(frozen=True, slots=True)
class _Table:
    """一张 M1D 私有业务表的静态描述。"""

    schema: str
    contract: type[Any]
    id_field: str
    code_prefix: str
    graph_field: str


_USER_BLOCKS = "private/user_blocks.jsonl"
_AGENT_STEPS = "private/agent_steps.jsonl"
_ASSISTANT_OUTCOMES = "private/assistant_outcomes.jsonl"
_QUERY_TURNS = "private/query_turns.jsonl"
_ACCOUNTING = "private/capture_turn_accounting.jsonl"
_THREAD_EDGES = "private/thread_turn_edges.jsonl"

_QUERY_TURN_PRIVATE_TABLES: dict[str, _Table] = {
    _USER_BLOCKS: _Table(
        USER_BLOCK_SCHEMA, UserBlockV1, "user_block_id", "M1D_USER_BLOCK", "user_blocks"
    ),
    _AGENT_STEPS: _Table(
        AGENT_STEP_SCHEMA, AgentStepV1, "agent_step_id", "M1D_AGENT_STEP", "agent_steps"
    ),
    _ASSISTANT_OUTCOMES: _Table(
        ASSISTANT_OUTCOME_SCHEMA,
        AssistantOutcomeV1,
        "assistant_outcome_id",
        "M1D_ASSISTANT_OUTCOME",
        "assistant_outcomes",
    ),
    _QUERY_TURNS: _Table(
        QUERY_TURN_SCHEMA, QueryTurnV1, "query_turn_id", "M1D_QUERY_TURN", "query_turns"
    ),
    _ACCOUNTING: _Table(
        CAPTURE_TURN_ACCOUNTING_SCHEMA,
        CaptureTurnAccountingV1,
        "capture_occurrence_id",
        "M1D_CAPTURE_ACCOUNTING",
        "capture_accounting",
    ),
    _THREAD_EDGES: _Table(
        THREAD_TURN_EDGE_SCHEMA, ThreadTurnEdgeV1, "edge_id", "M1D_THREAD_EDGE", "thread_turn_edges"
    ),
}
_DETERMINISTIC_FILES = frozenset(
    {"query_turn_manifest.json", "reports/m1d_report.json", *_QUERY_TURN_PRIVATE_TABLES}
)
_ALL_FILES = _DETERMINISTIC_FILES | {"artifact_manifest.json", "run_receipt.json"}
# terminal_status 交叉核对只对这三值断言结构；INVALID 属"末条非 assistant"，
# M1D 不建模（规格 §4 步 4）。
_CROSS_CHECKABLE_TERMINALS = frozenset(
    {
        TerminalStatus.TEXT_OUTCOME.value,
        TerminalStatus.TOOL_CALL_PENDING.value,
        TerminalStatus.EMPTY_OUTCOME.value,
    }
)

# 已发布表解析后的记录：表 → 主键 → 记录。
_Tables = dict[str, dict[str, dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class QueryTurnValidationResult:
    """完整 M1D run 的验收结果。"""

    ok: bool
    issues: tuple[ValidationIssue, ...]
    observed_counts: dict[str, int]
    checked_file_count: int

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(f"[{issue.code}] {issue.location}: {issue.message}" for issue in self.issues)


# --- 顶层编排 --------------------------------------------------------------


def validate_query_turn_run(
    m1d_run_dir: str | Path,
    m1b_run_dir: str | Path,
) -> QueryTurnValidationResult:
    """独立校验一个已发布 M1D run，取上游 M1B run 作回合图复算与不变量断言的 oracle。"""

    root = Path(m1d_run_dir)
    issues = IssueCollector()
    if not root.is_dir():
        issues.add("QUERY_TURN_RUN_NOT_DIRECTORY", "run", "m1d_run_dir 不是可读目录")
        return QueryTurnValidationResult(False, issues.finish(), {}, 0)

    artifact_manifest = read_json(root / "artifact_manifest.json", issues)
    entries = manifest_entries(
        root,
        artifact_manifest,
        contract=QueryTurnArtifactManifestV1,
        schema=QUERY_TURN_ARTIFACT_MANIFEST_SCHEMA,
        expected_files=_DETERMINISTIC_FILES,
        issues=issues,
    )
    checked = check_artifact_bytes(root, entries, issues)
    check_inventory(root, _ALL_FILES, issues)

    manifest = read_json(root / "query_turn_manifest.json", issues)
    check_contract(
        manifest,
        QueryTurnManifestV1,
        QUERY_TURN_MANIFEST_SCHEMA,
        "query_turn_manifest.json",
        issues,
    )
    report = read_json(root / "reports/m1d_report.json", issues)
    _check_report_shape(report, issues)
    receipt = read_json(root / "run_receipt.json", issues)
    check_receipt(
        root,
        receipt,
        artifact_manifest,
        receipt_schema=QUERY_TURN_RUN_RECEIPT_SCHEMA,
        manifest_run_id_field="query_turn_run_id",
        issues=issues,
    )

    for control, location in (
        (manifest, "query_turn_manifest.json"),
        (artifact_manifest, "artifact_manifest.json"),
        (report, "reports/m1d_report.json"),
        (receipt, "run_receipt.json"),
    ):
        scan_control_pathlike(
            control, location, code="QUERY_TURN_CONTROL_ABSOLUTE_PATH", issues=issues
        )

    # §2.1 先校验再消费：完整性与身份核验全权委托 M1B 权威 validator（load_m1b_turn_view 内部
    # 调 validate_compiled_run 并断言 run_id==目录名）。任一不符即无法复算回合图，跳过深度核对。
    view: M1bTurnView | None
    try:
        view = load_m1b_turn_view(m1b_run_dir)
    except QueryTurnInputError:
        issues.add("M1B_INPUT_INVALID", "m1b_run", "上游 M1B run 未通过独立完整性/身份校验")
        view = None

    observed_counts: dict[str, int] = {}
    if view is not None:
        _check_bindings(root, view, manifest, artifact_manifest, report, receipt, issues)
        observed_counts = _check_graph(root, view, report, issues)

    finished = issues.finish()
    return QueryTurnValidationResult(
        ok=not finished,
        issues=finished,
        observed_counts=observed_counts,
        checked_file_count=checked,
    )


# --- 公共报告形状（模块特有的闭合 allowlist）------------------------------------


def _check_report_shape(report: dict[str, Any] | None, issues: IssueCollector) -> None:
    """公共报告：闭合 schema + 仅固定聚合计数键 + 非负整数（不含原始 ID/正文/路径）。"""

    if not check_contract(
        report, QueryTurnReportV1, QUERY_TURN_REPORT_SCHEMA, "reports/m1d_report.json", issues
    ):
        return
    counts = report.get("counts")
    if not isinstance(counts, dict) or frozenset(counts) != QUERY_TURN_COUNT_KEYS:
        issues.add(
            "QUERY_TURN_REPORT_ALLOWLIST_MISMATCH",
            "reports/m1d_report.json/counts",
            "公共报告必须只包含固定聚合计数",
        )
        return
    if any(not is_int(value) or value < 0 for value in counts.values()):
        issues.add(
            "QUERY_TURN_REPORT_VALUE_INVALID",
            "reports/m1d_report.json/counts",
            "公共报告计数必须是非负整数",
        )


# --- 内容寻址绑定 ----------------------------------------------------------


def _check_bindings(
    root: Path,
    view: M1bTurnView,
    manifest: dict[str, Any] | None,
    artifact_manifest: dict[str, Any] | None,
    report: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    issues: IssueCollector,
) -> None:
    """断言 M1D 自报的上游身份==所给 M1B run 的已发布身份，且内容寻址 run_id 处处一致。"""

    expected_run_id = query_turn_run_id(
        m1b_run_id=view.m1b_run_id,
        m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
    )
    if root.name != expected_run_id:
        issues.add(
            "QUERY_TURN_RUN_DIRECTORY_ID_MISMATCH",
            "run",
            "目录名不等于内容寻址 query_turn_run_id",
        )

    for value, location in (
        (manifest, "query_turn_manifest.json"),
        (artifact_manifest, "artifact_manifest.json"),
    ):
        if not isinstance(value, dict):
            continue
        if value.get("m1b_run_id") != view.m1b_run_id:
            issues.add(
                "M1B_RUN_ID_BINDING_MISMATCH", location, "绑定的 m1b_run_id 与所给上游不一致"
            )
        if value.get("m1b_artifact_manifest_sha256") != view.m1b_artifact_manifest_sha256:
            issues.add(
                "M1B_MANIFEST_SHA_BINDING_MISMATCH",
                location,
                "绑定的 m1b_artifact_manifest_sha256 与所给上游不一致",
            )
        if value.get("query_turn_run_id") != expected_run_id:
            issues.add("QUERY_TURN_RUN_ID_MISMATCH", location, "内容寻址 query_turn_run_id 不匹配")
        if value.get("query_turn_contract_version") != QUERY_TURN_CONTRACT_VERSION:
            issues.add("QUERY_TURN_CONTRACT_VERSION_MISMATCH", location, "M1D 契约版本不匹配")

    if isinstance(manifest, dict) and manifest.get("source_schema") != view.source_schema:
        issues.add(
            "QUERY_TURN_SOURCE_SCHEMA_MISMATCH", "query_turn_manifest.json", "source_schema 不一致"
        )
    if isinstance(report, dict):
        if report.get("query_turn_run_id") != expected_run_id:
            issues.add(
                "QUERY_TURN_RUN_ID_MISMATCH", "reports/m1d_report.json", "query_turn_run_id 不匹配"
            )
        if report.get("m1b_run_id") != view.m1b_run_id:
            issues.add(
                "M1B_RUN_ID_BINDING_MISMATCH", "reports/m1d_report.json", "m1b_run_id 不一致"
            )
    if isinstance(receipt, dict) and receipt.get("run_id") != expected_run_id:
        issues.add(
            "QUERY_TURN_RUN_ID_MISMATCH", "run_receipt.json", "内容寻址 query_turn_run_id 不匹配"
        )


# --- 图完整性：篡改检测（同一纯 fold 双向 bijection）+ 正交不变量 ------------


def _check_graph(
    root: Path,
    view: M1bTurnView,
    report: dict[str, Any] | None,
    issues: IssueCollector,
) -> dict[str, int]:
    """读入六张私有表；bijection 抓篡改，不变量抓派生缺陷；返回由已发布表重算的聚合计数。"""

    tables: _Tables = {
        relative: _read_business_table(root, relative, table, issues)
        for relative, table in _QUERY_TURN_PRIVATE_TABLES.items()
    }

    graph = build_query_turn_graph(m1b_run_id=view.m1b_run_id, captures=view.captures)
    for relative, table in _QUERY_TURN_PRIVATE_TABLES.items():
        bijection(
            table.code_prefix,
            relative,
            _expected_bytes(graph, table),
            {
                identifier: canonical_json_bytes(record)
                for identifier, record in tables[relative].items()
            },
            issues,
        )

    _check_partition(view, tables, issues)
    _cross_check_terminal_status(view, issues)
    counts = _counts_from_tables(view, tables)
    _compare_report(report, counts, issues)
    return counts


def _expected_bytes(graph: QueryTurnGraph, table: _Table) -> dict[str, bytes]:
    expected: dict[str, bytes] = {}
    for record in getattr(graph, table.graph_field):
        payload = record.to_dict()
        expected[payload[table.id_field]] = canonical_json_bytes(payload)
    return expected


def _check_partition(view: M1bTurnView, tables: _Tables, issues: IssueCollector) -> None:
    """观测/前缀守恒（规格 §8）：不经 fold，只用 M1B 视图与 M1D 已发布表断言。

    对每个 capture，观测窗口事件 ID 集合必须**恰好**等于
    UserBlock.event_ids ∪ AgentStep.assistant_event_id ∪ AgentStep.tool_observation_event_ids
    ∪ 记账孤儿观测 ∪ 观测 SYSTEM ∪ 已发布 AgentStep 所属 ActionBatch 的 TOOL_CALL 的并，
    各成员两两不交、kind 与其划分相符、不得引用前缀或他 capture 的事件；
    记账的 observed/prefix 逐 kind 计数与 M1B 视图相等。
    """

    user_blocks = _group_by_capture(tables[_USER_BLOCKS].values())
    steps = _group_by_capture(tables[_AGENT_STEPS].values())
    accounting_by_capture = tables[_ACCOUNTING]

    for capture in view.captures:
        cid = capture.capture_occurrence_id
        accounting = accounting_by_capture.get(cid)
        if accounting is None:  # bijection 已报 MISSING，此处无分母可对
            continue
        location = f"{_ACCOUNTING}/{cid}"
        kind_by_id = {event.event_id: event.event_kind for event in capture.observed_events}

        claims: list[tuple[Any, str]] = []
        for block in user_blocks.get(cid, ()):
            claims.extend((event_id, EventKind.USER.value) for event_id in block["event_ids"])
        for step in steps.get(cid, ()):
            claims.append((step["assistant_event_id"], EventKind.ASSISTANT_MESSAGE.value))
            claims.extend(
                (event_id, EventKind.TOOL_RESULT.value)
                for event_id in step["tool_observation_event_ids"]
            )
            batch = capture.action_batch_by_assistant.get(step["assistant_event_id"])
            if batch is not None:
                claims.extend(
                    (event_id, EventKind.TOOL_CALL.value) for event_id in batch.tool_call_event_ids
                )
        claims.extend(
            (event_id, EventKind.TOOL_RESULT.value)
            for event_id in accounting["orphan_tool_observation_event_ids"]
        )
        claims.extend(
            (event.event_id, EventKind.SYSTEM.value)
            for event in capture.observed_events
            if event.event_kind == EventKind.SYSTEM
        )

        covered: set[str] = set()
        for event_id, expected_kind in claims:
            actual_kind = kind_by_id.get(event_id)
            if actual_kind is None:
                issues.add(
                    "M1D_PARTITION_FOREIGN_EVENT",
                    location,
                    f"划分引用了非本 capture 观测窗口的事件：{event_id}",
                )
                continue
            if actual_kind != expected_kind:
                issues.add(
                    "M1D_PARTITION_KIND_MISMATCH",
                    location,
                    f"事件被归入与其 kind 不符的划分：{event_id}",
                )
            if event_id in covered:
                issues.add("M1D_PARTITION_OVERLAP", location, f"观测事件被重复覆盖：{event_id}")
            covered.add(event_id)
        for event_id in sorted(kind_by_id.keys() - covered):
            issues.add("M1D_PARTITION_UNCOVERED", location, f"观测事件未被任何划分覆盖：{event_id}")

        observed_counts = dict(sorted(Counter(kind_by_id.values()).items()))
        if accounting["observed_event_counts_by_kind"] != observed_counts:
            issues.add(
                "M1D_ACCOUNTING_OBSERVED_COUNT_MISMATCH",
                location,
                "observed_event_counts_by_kind 与 M1B 观测窗口逐 kind 计数不一致",
            )
        prefix_counts = dict(sorted(capture.prefix_event_counts_by_kind.items()))
        if accounting["prefix_event_counts_by_kind"] != prefix_counts:
            issues.add(
                "M1D_ACCOUNTING_PREFIX_COUNT_MISMATCH",
                location,
                "prefix_event_counts_by_kind 与 M1B 前缀逐 kind 计数不一致",
            )


def _group_by_capture(records: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["capture_occurrence_id"], []).append(record)
    return grouped


def _cross_check_terminal_status(view: M1bTurnView, issues: IssueCollector) -> None:
    """把每个 capture 的终态结构映回 M1B 独立 oracle ``CaptureQualityV3.terminal_status``。

    编译 capture 的最后一个 boundary terminal 恒为 assistant（compiler 强制），即观测窗口内
    **序号最大的 ASSISTANT_MESSAGE**；据其 ActionBatch 与内容非空经 `terminal_status_for_step`
    复算应有终态，与上游 quality 声明比对。终态 INVALID（末条非 assistant）M1D 不建模，
    跳过（§4 步 4）。
    """

    for capture in view.captures:
        if capture.terminal_status not in _CROSS_CHECKABLE_TERMINALS:
            continue
        terminal = _terminal_assistant(capture.observed_events)
        location = f"{_ACCOUNTING}/{capture.capture_occurrence_id}"
        if terminal is None:
            issues.add(
                "QUERY_TURN_TERMINAL_STATUS_MISMATCH",
                location,
                "上游终态非 INVALID，但观测窗口无 assistant 事件可映射",
            )
            continue
        present = content_is_present(
            text_utf8_byte_length=terminal.text_utf8_byte_length,
            block_count=terminal.block_count,
        )
        expected = terminal_status_for_step(
            has_action_batch=terminal.event_id in capture.action_batch_by_assistant,
            content_present=present,
        )
        if expected.value != capture.terminal_status:
            issues.add(
                "QUERY_TURN_TERMINAL_STATUS_MISMATCH",
                location,
                "capture 末回合结构映射与 M1B terminal_status oracle 不一致",
            )


def _terminal_assistant(events: Sequence[ObservedEvent]) -> ObservedEvent | None:
    terminal: ObservedEvent | None = None
    for event in events:
        if event.event_kind != EventKind.ASSISTANT_MESSAGE:
            continue
        if terminal is None or event.sequence_number > terminal.sequence_number:
            terminal = event
    return terminal


def _counts_from_tables(view: M1bTurnView, tables: _Tables) -> dict[str, int]:
    """由**已发布表**独立重算公共报告计数（分母 capture_count 取 M1B 已编译 capture 数）。"""

    turns = tables[_QUERY_TURNS].values()
    accounting = tables[_ACCOUNTING].values()
    return build_report_counts(
        capture_count=len(view.captures),
        query_turn_count=len(tables[_QUERY_TURNS]),
        prefix_rooted_turn_count=sum(
            1 for turn in turns if turn["root_status"] == RootStatus.PREFIX_ROOTED.value
        ),
        observed_rooted_turn_count=sum(
            1 for turn in turns if turn["root_status"] == RootStatus.OBSERVED_ROOTED.value
        ),
        complete_turn_count=sum(
            1 for turn in turns if turn["turn_status"] == TurnStatus.COMPLETE.value
        ),
        incomplete_turn_count=sum(
            1 for turn in turns if turn["turn_status"] == TurnStatus.INCOMPLETE.value
        ),
        agent_step_count=len(tables[_AGENT_STEPS]),
        user_block_count=len(tables[_USER_BLOCKS]),
        assistant_outcome_count=len(tables[_ASSISTANT_OUTCOMES]),
        thread_turn_edge_count=len(tables[_THREAD_EDGES]),
        orphan_tool_observation_count=sum(
            len(row["orphan_tool_observation_event_ids"]) for row in accounting
        ),
        captures_with_unlocalizable_prefix_count=sum(
            1 for row in accounting if row["has_unlocalizable_prefix"] is True
        ),
        captures_with_compaction_count=sum(
            1 for row in accounting if row["has_compaction"] is True
        ),
    )


def _read_business_table(
    root: Path,
    relative: str,
    table: _Table,
    issues: IssueCollector,
) -> dict[str, dict[str, Any]]:
    """读入一张 M1D 私有表：逐行 canonical/schema/隐私核验，键于主键，检出同表重复 ID。"""

    observed: dict[str, dict[str, Any]] = {}
    for line_number, record in enumerate(
        iter_canonical_jsonl(
            root, relative, schema=table.schema, contract=table.contract, issues=issues
        ),
        1,
    ):
        location = f"{relative}:{line_number}"
        identifier = record.get(table.id_field)
        if not isinstance(identifier, str) or not identifier:
            issues.add("QUERY_TURN_PRIMARY_KEY_INVALID", location, f"{table.id_field} 非法")
            continue
        if identifier in observed:
            issues.add("QUERY_TURN_DUPLICATE_ID", location, f"{table.id_field} 重复")
        observed[identifier] = record
    return observed


# --- 聚合计数比对 ----------------------------------------------------------


def _compare_report(
    report: Mapping[str, Any] | None,
    observed: Mapping[str, int],
    issues: IssueCollector,
) -> None:
    if not isinstance(report, dict) or not isinstance(report.get("counts"), dict):
        return
    for key in sorted(QUERY_TURN_COUNT_KEYS):
        if report["counts"].get(key) != observed.get(key):
            issues.add(
                "QUERY_TURN_REPORT_COUNT_MISMATCH",
                f"reports/m1d_report.json/counts/{key}",
                f"报告={report['counts'].get(key)}，重算={observed.get(key)}",
            )
