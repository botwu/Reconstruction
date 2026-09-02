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
用与 M1B/M1C 同构的自有代码路径核验。取双参 ``validate_query_turn_run(m1d_run_dir, m1b_run_dir)``：
M1D 只以 ``m1b_run_id`` + manifest sha256 内容寻址绑定 M1B（§7 禁存路径），故须同时拿到上游 run
作 oracle；validator 先断言所给 M1B run 的身份与 M1D 声明一致，再据此复算。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
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
from traceforge.trajectory.contracts import ArtifactEntryV1, EventKind, TerminalStatus
from traceforge.trajectory.json_codec import (
    StrictJsonError,
    canonical_json_bytes,
    canonical_json_line,
    sha256_bytes,
    strict_json_loads,
)
from traceforge.trajectory.privacy import find_privacy_violations


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
_RECEIPT_FIELDS = frozenset(
    {
        "artifact_manifest_sha256",
        "completed_at",
        "duration_seconds",
        "git_provenance",
        "git_provenance_verified_at_completion",
        "python",
        "run_id",
        "schema_version",
        "traceforge_version",
    }
)
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
class QueryTurnValidationIssue:
    """一条不携带业务原文的校验错误。"""

    code: str
    location: str
    message: str


@dataclass(frozen=True, slots=True)
class QueryTurnValidationResult:
    """完整 M1D run 的验收结果。"""

    ok: bool
    issues: tuple[QueryTurnValidationIssue, ...]
    observed_counts: dict[str, int]
    checked_file_count: int

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(f"[{issue.code}] {issue.location}: {issue.message}" for issue in self.issues)


class _Issues:
    """限制错误量，避免损坏的大文件反向耗尽内存。"""

    def __init__(self, limit: int = 300) -> None:
        self.items: list[QueryTurnValidationIssue] = []
        self.limit = limit
        self.dropped = 0

    def add(self, code: str, location: str, message: str) -> None:
        if len(self.items) < self.limit:
            self.items.append(QueryTurnValidationIssue(code, location, message))
        else:
            self.dropped += 1

    def finish(self) -> tuple[QueryTurnValidationIssue, ...]:
        if self.dropped:
            self.items.append(
                QueryTurnValidationIssue(
                    "ERROR_LIMIT_REACHED",
                    "run",
                    f"另有 {self.dropped} 条错误未展开",
                )
            )
        return tuple(self.items)


# --- 顶层编排 --------------------------------------------------------------


def validate_query_turn_run(
    m1d_run_dir: str | Path,
    m1b_run_dir: str | Path,
) -> QueryTurnValidationResult:
    """独立校验一个已发布 M1D run，取上游 M1B run 作回合图复算与不变量断言的 oracle。"""

    root = Path(m1d_run_dir)
    issues = _Issues()
    if not root.is_dir():
        issues.add("QUERY_TURN_RUN_NOT_DIRECTORY", "run", "m1d_run_dir 不是可读目录")
        return QueryTurnValidationResult(False, issues.finish(), {}, 0)

    artifact_manifest = _read_json(root / "artifact_manifest.json", issues)
    entries = _manifest_entries(root, artifact_manifest, issues)
    checked = _check_artifact_bytes(root, entries, issues)
    _check_inventory(root, issues)

    manifest = _read_json(root / "query_turn_manifest.json", issues)
    _check_contract(
        manifest,
        QueryTurnManifestV1,
        QUERY_TURN_MANIFEST_SCHEMA,
        "query_turn_manifest.json",
        issues,
    )
    report = _read_json(root / "reports/m1d_report.json", issues)
    _check_report_shape(report, issues)
    receipt = _read_json(root / "run_receipt.json", issues)
    _check_receipt(root, receipt, artifact_manifest, issues)

    for control, location in (
        (manifest, "query_turn_manifest.json"),
        (artifact_manifest, "artifact_manifest.json"),
        (report, "reports/m1d_report.json"),
        (receipt, "run_receipt.json"),
    ):
        _scan_control_pathlike(control, location, issues)

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


# --- 产物完整性（镜像 M1B/M1C validator 的信任边界，自有代码路径）------------


def _manifest_entries(
    root: Path,
    manifest: dict[str, Any] | None,
    issues: _Issues,
) -> dict[str, dict[str, Any]]:
    if not _check_contract(
        manifest,
        QueryTurnArtifactManifestV1,
        QUERY_TURN_ARTIFACT_MANIFEST_SCHEMA,
        "artifact_manifest.json",
        issues,
    ):
        return {}
    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        issues.add("MANIFEST_FILES_INVALID", "artifact_manifest.json/files", "files 必须是数组")
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(raw_entries):
        location = f"artifact_manifest.json/files/{index}"
        if not _check_contract(entry, ArtifactEntryV1, None, location, issues):
            continue
        relative = entry.get("relative_path")
        if not isinstance(relative, str) or not relative:
            issues.add("MANIFEST_PATH_INVALID", location, "relative_path 必须是非空字符串")
            continue
        try:
            (root / relative).resolve().relative_to(root.resolve())
        except ValueError:
            issues.add("MANIFEST_PATH_ESCAPES_RUN", location, "relative_path 越出 run 目录")
            continue
        if relative in entries:
            issues.add("MANIFEST_PATH_DUPLICATE", location, "relative_path 重复")
        entries[relative] = entry
    if frozenset(entries) != _DETERMINISTIC_FILES:
        issues.add(
            "MANIFEST_FILE_SET_MISMATCH",
            "artifact_manifest.json/files",
            "manifest 文件集合与当前 M1D 契约不一致",
        )
    return entries


def _check_artifact_bytes(
    root: Path,
    entries: Mapping[str, Mapping[str, Any]],
    issues: _Issues,
) -> int:
    checked = 0
    for relative, entry in sorted(entries.items()):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            issues.add("ARTIFACT_NOT_REGULAR_FILE", relative, "artifact 不存在或不是普通文件")
            continue
        digest = hashlib.sha256()
        size = lines = 0
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                lines += chunk.count(b"\n")
        checked += 1
        if digest.hexdigest() != entry.get("sha256"):
            issues.add("ARTIFACT_SHA256_MISMATCH", relative, "SHA-256 与 manifest 不一致")
        if size != entry.get("byte_length"):
            issues.add("ARTIFACT_SIZE_MISMATCH", relative, "字节数与 manifest 不一致")
        expected_records = entry.get("record_count")
        if relative.endswith(".jsonl"):
            if not _is_int(expected_records) or lines != expected_records:
                issues.add(
                    "ARTIFACT_RECORD_COUNT_MISMATCH",
                    relative,
                    "JSONL 行数与 manifest 不一致",
                )
        elif expected_records is not None:
            issues.add(
                "ARTIFACT_RECORD_COUNT_INVALID",
                relative,
                "单体 JSON 的 record_count 必须为 null",
            )
    return checked


def _check_inventory(root: Path, issues: _Issues) -> None:
    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if observed != _ALL_FILES:
        issues.add("RUN_FILE_SET_MISMATCH", "run", "run 文件集合与当前 M1D 契约不一致")


def _check_report_shape(report: dict[str, Any] | None, issues: _Issues) -> None:
    """公共报告：闭合 schema + 仅固定聚合计数键 + 非负整数（不含原始 ID/正文/路径）。"""

    if not _check_contract(
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
    if any(not _is_int(value) or value < 0 for value in counts.values()):
        issues.add(
            "QUERY_TURN_REPORT_VALUE_INVALID",
            "reports/m1d_report.json/counts",
            "公共报告计数必须是非负整数",
        )


def _check_receipt(
    root: Path,
    receipt: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
    issues: _Issues,
) -> None:
    if not isinstance(receipt, dict):
        return
    if frozenset(receipt) != _RECEIPT_FIELDS:
        issues.add("RUN_RECEIPT_SCHEMA_MISMATCH", "run_receipt.json", "运行回执字段不匹配")
    if receipt.get("schema_version") != QUERY_TURN_RUN_RECEIPT_SCHEMA:
        issues.add("RUN_RECEIPT_SCHEMA_INVALID", "run_receipt.json", "schema_version 不匹配")
    _check_git_provenance(receipt.get("git_provenance"), issues)
    if receipt.get("git_provenance_verified_at_completion") is not True:
        issues.add(
            "RUN_RECEIPT_GIT_PROVENANCE_UNVERIFIED",
            "run_receipt.json/git_provenance_verified_at_completion",
            "正式 run 必须确认 Git 来源在完成时仍一致",
        )
    manifest_path = root / "artifact_manifest.json"
    if manifest_path.is_file() and receipt.get("artifact_manifest_sha256") != sha256_bytes(
        manifest_path.read_bytes()
    ):
        issues.add(
            "RUN_RECEIPT_MANIFEST_HASH_MISMATCH",
            "run_receipt.json",
            "artifact manifest 摘要不匹配",
        )
    if isinstance(manifest, dict) and receipt.get("run_id") != manifest.get("query_turn_run_id"):
        issues.add("RUN_RECEIPT_RUN_ID_MISMATCH", "run_receipt.json", "run_id 不匹配")


def _check_git_provenance(value: Any, issues: _Issues) -> None:
    location = "run_receipt.json/git_provenance"
    if not isinstance(value, dict) or set(value) != {"available", "commit", "tree", "dirty"}:
        issues.add("RUN_RECEIPT_GIT_PROVENANCE_INVALID", location, "git_provenance 字段不闭合")
        return
    available = value.get("available")
    commit = value.get("commit")
    tree = value.get("tree")
    dirty = value.get("dirty")
    if available is True:
        if (
            not _is_git_object_id(commit)
            or not _is_git_object_id(tree)
            or len(commit) != len(tree)
            or not isinstance(dirty, bool)
        ):
            issues.add(
                "RUN_RECEIPT_GIT_PROVENANCE_INVALID",
                location,
                "available 来源必须携带同算法 commit/tree 与布尔 dirty",
            )
    elif available is False:
        if commit is not None or tree is not None or dirty is not None:
            issues.add(
                "RUN_RECEIPT_GIT_PROVENANCE_INVALID",
                location,
                "unavailable 来源必须显式使用 null",
            )
    else:
        issues.add("RUN_RECEIPT_GIT_PROVENANCE_INVALID", location, "available 必须是布尔值")


def _scan_control_pathlike(value: Any, location: str, issues: _Issues) -> None:
    """控制文件不得泄漏绝对路径/URL/主机身份；`find_privacy_violations` 不查这些，故显式扫描。

    relative_path 等业务字段是 run 内相对路径（不以 ``/`` 起头），不会误伤。
    """

    if isinstance(value, str):
        if value.startswith("/") or "://" in value:
            issues.add("QUERY_TURN_CONTROL_ABSOLUTE_PATH", location, "控制文件出现绝对路径或 URL")
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan_control_pathlike(item, f"{location}/{key}", issues)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_control_pathlike(item, f"{location}/{index}", issues)


# --- 内容寻址绑定 ----------------------------------------------------------


def _check_bindings(
    root: Path,
    view: M1bTurnView,
    manifest: dict[str, Any] | None,
    artifact_manifest: dict[str, Any] | None,
    report: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    issues: _Issues,
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
    issues: _Issues,
) -> dict[str, int]:
    """读入六张私有表；bijection 抓篡改，不变量抓派生缺陷；返回由已发布表重算的聚合计数。"""

    tables: _Tables = {
        relative: _read_business_table(root, relative, table, issues)
        for relative, table in _QUERY_TURN_PRIVATE_TABLES.items()
    }

    graph = build_query_turn_graph(m1b_run_id=view.m1b_run_id, captures=view.captures)
    for relative, table in _QUERY_TURN_PRIVATE_TABLES.items():
        _bijection(
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


def _bijection(
    prefix: str,
    location: str,
    expected: Mapping[str, bytes],
    observed: Mapping[str, bytes],
    issues: _Issues,
) -> None:
    """双向集合相等：expected 缺席=漏报（删产物），observed 多出=幻影，同键内容不符=篡改。"""

    for identifier in sorted(expected.keys() - observed.keys()):
        issues.add(f"{prefix}_MISSING", location, f"独立复算存在但自报缺失：{identifier}")
    for identifier in sorted(observed.keys() - expected.keys()):
        issues.add(f"{prefix}_PHANTOM", location, f"自报存在但独立复算不产出：{identifier}")
    for identifier in sorted(expected.keys() & observed.keys()):
        if expected[identifier] != observed[identifier]:
            issues.add(f"{prefix}_MISMATCH", location, f"自报内容与独立复算不一致：{identifier}")


def _check_partition(view: M1bTurnView, tables: _Tables, issues: _Issues) -> None:
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


def _cross_check_terminal_status(view: M1bTurnView, issues: _Issues) -> None:
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
    issues: _Issues,
) -> dict[str, dict[str, Any]]:
    """读入一张 M1D 私有表：逐行 canonical/schema/隐私核验，键于主键，检出同表重复 ID。"""

    observed: dict[str, dict[str, Any]] = {}
    for line_number, record in enumerate(
        _iter_query_turn_jsonl(root, relative, table.schema, table.contract, issues), 1
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
    issues: _Issues,
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


# --- 通用读入/校验原语（自有代码路径，不 import M1B/M1C validator 私有实现）----


def _iter_query_turn_jsonl(
    root: Path,
    relative: str,
    schema: str,
    contract: type[Any],
    issues: _Issues,
) -> Iterable[dict[str, Any]]:
    path = root / relative
    if not path.is_file():
        return
    with path.open("rb") as file:
        for line_number, raw in enumerate(file, 1):
            location = f"{relative}:{line_number}"
            if not raw.endswith(b"\n"):
                issues.add("JSONL_FINAL_LF_MISSING", location, "物理行未以 LF 结束")
            try:
                value = strict_json_loads(raw)
            except StrictJsonError:
                issues.add("JSONL_STRICT_JSON_INVALID", location, "不是严格 UTF-8 JSON")
                continue
            if canonical_json_line(value) != raw:
                issues.add("JSONL_NOT_CANONICAL", location, "不是 canonical JSON + LF")
            _check_privacy(value, location, issues)
            if _check_contract(value, contract, schema, location, issues):
                yield value


def _read_json(path: Path, issues: _Issues) -> dict[str, Any] | None:
    location = path.name if path.parent.name != "reports" else f"reports/{path.name}"
    if not path.is_file():
        issues.add("JSON_FILE_MISSING", location, "文件不存在")
        return None
    raw = path.read_bytes()
    try:
        value = strict_json_loads(raw)
    except StrictJsonError:
        issues.add("JSON_FILE_INVALID", location, "不是严格 UTF-8 JSON")
        return None
    if canonical_json_line(value) != raw:
        issues.add("JSON_FILE_NOT_CANONICAL", location, "不是 canonical JSON + LF")
    if not isinstance(value, dict):
        issues.add("JSON_FILE_NOT_OBJECT", location, "顶层必须是对象")
        return None
    _check_privacy(value, location, issues)
    return value


def _check_privacy(value: Any, location: str, issues: _Issues) -> None:
    """兜底扫描 Data URL / reasoning；普通 URL/绝对路径由控制文件路径扫描把关。"""

    for violation in find_privacy_violations(value):
        code = (
            "BASE64_DATA_URL_OBSERVED"
            if violation.code == "RAW_DATA_URL"
            else "RAW_REASONING_OBSERVED"
        )
        issues.add(code, f"{location}{violation.pointer}", "派生产物违反隐私摘要契约")


def _check_contract(
    value: Any,
    contract: type[Any],
    schema: str | None,
    location: str,
    issues: _Issues,
) -> bool:
    if not isinstance(value, dict):
        issues.add("SCHEMA_NOT_OBJECT", location, "记录顶层必须是对象")
        return False
    expected_fields = {contract_field.name for contract_field in fields(contract)}
    if set(value) != expected_fields:
        issues.add("SCHEMA_FIELDS_MISMATCH", location, "字段集合与当前 contract 不一致")
        return False
    if schema is not None and value.get("schema_version") != schema:
        issues.add("SCHEMA_VERSION_MISMATCH", location, "schema_version 不匹配")
        return False
    return True


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_git_object_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )
