"""对一个已发布 M1C lineage run 做独立、可重算的验收（规格 §8）。

信任边界（镜像 `trajectory.validation` 对 M1B 的立场）：本模块**不 import `builder`/`pipeline`**，
而是从**上游 M1B 已发布字段**用自有代码路径**全局组盲地独立枚举完整关系集**，再与 lineage
`private/` 中自报的边/节点做**双向集合相等**比对。首要威胁是「悄悄删边洗白来源」——删边、造幻影
边、篡改稳定 ID、把候选组/`target_hash` 写进 evidence，均被上述可重算事实推翻，即使产物被同步重签。

取双参 ``validate_lineage_run(lineage_run_dir, m1b_run_dir)``：lineage run 只以 ``m1b_run_id`` +
manifest sha256 内容寻址绑定 M1B（§10 禁存路径），要复算完整边集必须回上游源表，故须同时拿到上游
run 作 oracle。validator 先断言所给 M1B run 的绑定身份与 lineage 声明一致，再据此复算；对该 M1B run
直接调用权威 ``validate_compiled_run`` 复用 M1B 验收（correctness > perf），不重算 M1B 私有公式。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from itertools import combinations, pairwise
from pathlib import Path
from typing import Any

from traceforge.lineage.contracts import (
    CAPTURE_LEVEL_RELATIONS,
    CAPTURE_RELATION_EDGE_SCHEMA,
    LINEAGE_ARTIFACT_MANIFEST_SCHEMA,
    LINEAGE_CONTRACT_VERSION,
    LINEAGE_COUNT_KEYS,
    LINEAGE_MANIFEST_SCHEMA,
    LINEAGE_REPORT_SCHEMA,
    LINEAGE_RUN_RECEIPT_SCHEMA,
    REQUEST_LEVEL_RELATIONS,
    REQUEST_NODE_SCHEMA,
    REQUEST_SUCCESSOR_EDGE_SCHEMA,
    CaptureRelationEdgeV1,
    LineageArtifactManifestV1,
    LineageManifestV1,
    LineageRelation,
    LineageReportV1,
    RelationDirectionality,
    RequestNodeV1,
    RequestSuccessorEdgeV1,
    build_report_counts,
    capture_relation_edge_id,
    lineage_run_id,
    relation_properties,
    request_node_id,
    request_successor_edge_id,
)
from traceforge.lineage.reader import LineageInputError, M1bRunView, load_m1b_run_view
from traceforge.trajectory.contracts import ArtifactEntryV1
from traceforge.trajectory.json_codec import (
    StrictJsonError,
    canonical_json_bytes,
    canonical_json_line,
    sha256_bytes,
    strict_json_loads,
)
from traceforge.trajectory.privacy import find_privacy_violations

_LINEAGE_PRIVATE_CONTRACTS = {
    "private/request_nodes.jsonl": (REQUEST_NODE_SCHEMA, RequestNodeV1),
    "private/request_successor_edges.jsonl": (
        REQUEST_SUCCESSOR_EDGE_SCHEMA,
        RequestSuccessorEdgeV1,
    ),
    "private/capture_relation_edges.jsonl": (CAPTURE_RELATION_EDGE_SCHEMA, CaptureRelationEdgeV1),
}
_DETERMINISTIC_FILES = frozenset(
    {"lineage_manifest.json", "reports/lineage_report.json", *_LINEAGE_PRIVATE_CONTRACTS}
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


@dataclass(frozen=True, slots=True)
class LineageValidationIssue:
    """一条不携带业务原文的校验错误。"""

    code: str
    location: str
    message: str


@dataclass(frozen=True, slots=True)
class LineageValidationResult:
    """完整 lineage run 的验收结果。"""

    ok: bool
    issues: tuple[LineageValidationIssue, ...]
    observed_counts: dict[str, int]
    checked_file_count: int

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(f"[{issue.code}] {issue.location}: {issue.message}" for issue in self.issues)


class _Issues:
    """限制错误量，避免损坏的大文件反向耗尽内存。"""

    def __init__(self, limit: int = 300) -> None:
        self.items: list[LineageValidationIssue] = []
        self.limit = limit
        self.dropped = 0

    def add(self, code: str, location: str, message: str) -> None:
        if len(self.items) < self.limit:
            self.items.append(LineageValidationIssue(code, location, message))
        else:
            self.dropped += 1

    def finish(self) -> tuple[LineageValidationIssue, ...]:
        if self.dropped:
            self.items.append(
                LineageValidationIssue(
                    "ERROR_LIMIT_REACHED",
                    "run",
                    f"另有 {self.dropped} 条错误未展开",
                )
            )
        return tuple(self.items)


# --- 顶层编排 --------------------------------------------------------------


def validate_lineage_run(
    lineage_run_dir: str | Path,
    m1b_run_dir: str | Path,
) -> LineageValidationResult:
    """独立校验一个已发布 M1C lineage run，取上游 M1B run 作完整边集复算的 oracle。"""

    root = Path(lineage_run_dir)
    issues = _Issues()
    if not root.is_dir():
        issues.add("LINEAGE_RUN_NOT_DIRECTORY", "run", "lineage_run_dir 不是可读目录")
        return LineageValidationResult(False, issues.finish(), {}, 0)

    artifact_manifest = _read_json(root / "artifact_manifest.json", issues)
    entries = _manifest_entries(root, artifact_manifest, issues)
    checked = _check_artifact_bytes(root, entries, issues)
    _check_inventory(root, issues)

    lineage_manifest = _read_json(root / "lineage_manifest.json", issues)
    _check_contract(
        lineage_manifest,
        LineageManifestV1,
        LINEAGE_MANIFEST_SCHEMA,
        "lineage_manifest.json",
        issues,
    )
    report = _read_json(root / "reports/lineage_report.json", issues)
    _check_report_shape(report, issues)
    receipt = _read_json(root / "run_receipt.json", issues)
    _check_receipt(root, receipt, artifact_manifest, issues)

    for control, location in (
        (lineage_manifest, "lineage_manifest.json"),
        (artifact_manifest, "artifact_manifest.json"),
        (report, "reports/lineage_report.json"),
        (receipt, "run_receipt.json"),
    ):
        _scan_control_pathlike(control, location, issues)

    # §2.1 先校验再消费：完整性与身份核验全权委托 M1B 权威 validator（load_m1b_run_view 内部
    # 调 validate_compiled_run 并断言 run_id==目录名）。任一不符即无法复算完整边集，跳过深度核对。
    view: M1bRunView | None
    try:
        view = load_m1b_run_view(m1b_run_dir)
    except LineageInputError:
        issues.add("M1B_INPUT_INVALID", "m1b_run", "上游 M1B run 未通过独立完整性/身份校验")
        view = None

    observed_counts: dict[str, int] = {}
    if view is not None:
        _check_bindings(root, view, lineage_manifest, artifact_manifest, report, receipt, issues)
        _check_candidate_group_partition(view, issues)
        observed_counts = _check_graph(root, view, report, issues)

    finished = issues.finish()
    return LineageValidationResult(
        ok=not finished,
        issues=finished,
        observed_counts=observed_counts,
        checked_file_count=checked,
    )


# --- 产物完整性（镜像 M1B validator 的信任边界，自有代码路径）----------------


def _manifest_entries(
    root: Path,
    manifest: dict[str, Any] | None,
    issues: _Issues,
) -> dict[str, dict[str, Any]]:
    if not _check_contract(
        manifest,
        LineageArtifactManifestV1,
        LINEAGE_ARTIFACT_MANIFEST_SCHEMA,
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
            "manifest 文件集合与当前 M1C 契约不一致",
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
        issues.add("RUN_FILE_SET_MISMATCH", "run", "run 文件集合与当前 M1C 契约不一致")


def _check_report_shape(report: dict[str, Any] | None, issues: _Issues) -> None:
    """公共报告：闭合 schema + 仅固定聚合计数键 + 非负整数（不含原始 ID/正文/路径）。"""

    if not _check_contract(
        report, LineageReportV1, LINEAGE_REPORT_SCHEMA, "reports/lineage_report.json", issues
    ):
        return
    counts = report.get("counts")
    if not isinstance(counts, dict) or frozenset(counts) != LINEAGE_COUNT_KEYS:
        issues.add(
            "LINEAGE_REPORT_ALLOWLIST_MISMATCH",
            "reports/lineage_report.json/counts",
            "公共报告必须只包含固定聚合计数",
        )
        return
    if any(not _is_int(value) or value < 0 for value in counts.values()):
        issues.add(
            "LINEAGE_REPORT_VALUE_INVALID",
            "reports/lineage_report.json/counts",
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
    if receipt.get("schema_version") != LINEAGE_RUN_RECEIPT_SCHEMA:
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
    if isinstance(manifest, dict) and receipt.get("run_id") != manifest.get("lineage_run_id"):
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
            issues.add("LINEAGE_CONTROL_ABSOLUTE_PATH", location, "控制文件出现绝对路径或 URL")
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan_control_pathlike(item, f"{location}/{key}", issues)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_control_pathlike(item, f"{location}/{index}", issues)


# --- 内容寻址绑定 ----------------------------------------------------------


def _check_bindings(
    root: Path,
    view: M1bRunView,
    lineage_manifest: dict[str, Any] | None,
    artifact_manifest: dict[str, Any] | None,
    report: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    issues: _Issues,
) -> None:
    """断言 lineage 自报的上游身份==所给 M1B run 的已发布身份，且内容寻址 run_id 处处一致。"""

    expected_run_id = lineage_run_id(
        m1b_run_id=view.m1b_run_id,
        m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
    )
    if root.name != expected_run_id:
        issues.add(
            "LINEAGE_RUN_DIRECTORY_ID_MISMATCH", "run", "目录名不等于内容寻址 lineage_run_id"
        )

    for value, location in (
        (lineage_manifest, "lineage_manifest.json"),
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
        if value.get("lineage_run_id") != expected_run_id:
            issues.add("LINEAGE_RUN_ID_MISMATCH", location, "内容寻址 lineage_run_id 不匹配")
        if value.get("lineage_contract_version") != LINEAGE_CONTRACT_VERSION:
            issues.add("LINEAGE_CONTRACT_VERSION_MISMATCH", location, "lineage 契约版本不匹配")

    if isinstance(lineage_manifest, dict) and lineage_manifest.get("source_schema") != (
        view.source_schema
    ):
        issues.add(
            "LINEAGE_SOURCE_SCHEMA_MISMATCH", "lineage_manifest.json", "source_schema 不一致"
        )
    if isinstance(report, dict):
        if report.get("lineage_run_id") != expected_run_id:
            issues.add(
                "LINEAGE_RUN_ID_MISMATCH", "reports/lineage_report.json", "lineage_run_id 不匹配"
            )
        if report.get("m1b_run_id") != view.m1b_run_id:
            issues.add(
                "M1B_RUN_ID_BINDING_MISMATCH", "reports/lineage_report.json", "m1b_run_id 不一致"
            )
    if isinstance(receipt, dict) and receipt.get("run_id") != expected_run_id:
        issues.add("LINEAGE_RUN_ID_MISMATCH", "run_receipt.json", "内容寻址 lineage_run_id 不匹配")


def _check_candidate_group_partition(view: M1bRunView, issues: _Issues) -> None:
    """门③：``candidate_group_id`` 分区与 ``(thread_id, account_id)`` 分区严格 1:1 双射。"""

    group_to_thread: dict[str, set[tuple[str, str]]] = defaultdict(set)
    thread_to_group: dict[tuple[str, str], set[str]] = defaultdict(set)
    for capture_id, group in view.candidate_group_by_capture.items():
        thread_account = view.thread_account_by_capture.get(capture_id)
        if thread_account is None:
            continue
        group_to_thread[group].add(thread_account)
        thread_to_group[thread_account].add(group)
    if any(len(threads) != 1 for threads in group_to_thread.values()) or any(
        len(groups) != 1 for groups in thread_to_group.values()
    ):
        issues.add(
            "LINEAGE_CANDIDATE_GROUP_PARTITION_MISMATCH",
            "m1b_run",
            "candidate_group_id 与 (thread_id, account_id) 分区非 1:1 双射",
        )


# --- 图完整性：独立枚举完整边集 + 双向 bijection -----------------------------


def _check_graph(
    root: Path,
    view: M1bRunView,
    report: dict[str, Any] | None,
    issues: _Issues,
) -> dict[str, int]:
    """从 M1B 已发布字段独立复算完整图，与 lineage private/ 双向比对；返回重算聚合计数。"""

    capture_ids = frozenset(view.capture_ids)
    candidate_groups = frozenset(view.candidate_group_by_capture.values())
    target_hashes = frozenset(
        value for value in view.target_hash_by_capture.values() if isinstance(value, str)
    )

    # 独立重算期望集（自有代码路径，不 import builder）。
    expected_nodes = _rederive_request_nodes(view)
    expected_capture_edges = _rederive_capture_edges(view)
    expected_successor_edges = _rederive_successor_edges(view)

    # 读入自报集并逐记录做闭合值域白名单 + 稳定 ID 自洽 + 门①/§5.5 evidence 扫描。
    observed_nodes = _read_nodes(root, view, capture_ids, issues)
    observed_capture_edges = _read_capture_edges(
        root, view, capture_ids, candidate_groups, target_hashes, issues
    )
    observed_successor_edges = _read_successor_edges(
        root, view, capture_ids, candidate_groups, target_hashes, expected_nodes, issues
    )

    _bijection(
        "LINEAGE_NODE", "private/request_nodes.jsonl", expected_nodes, observed_nodes, issues
    )
    _bijection(
        "LINEAGE_EDGE",
        "private/capture_relation_edges.jsonl",
        expected_capture_edges,
        observed_capture_edges,
        issues,
    )
    _bijection(
        "LINEAGE_EDGE",
        "private/request_successor_edges.jsonl",
        expected_successor_edges,
        observed_successor_edges,
        issues,
    )

    # 守恒：RequestNode 数 == distinct source_request_id 数。
    distinct_srid = {
        source_request_id
        for boundaries in view.boundaries_by_capture.values()
        for _ordinal, source_request_id in boundaries
    }
    if len(expected_nodes) != len(distinct_srid):
        issues.add(
            "LINEAGE_REQUEST_NODE_CONSERVATION",
            "private/request_nodes.jsonl",
            "RequestNode 数与 distinct source_request_id 数不一致",
        )

    observed_counts = _recompute_counts(
        view, expected_nodes, expected_capture_edges, expected_successor_edges
    )
    _compare_report(report, observed_counts, issues)
    return observed_counts


def _rederive_request_nodes(view: M1bRunView) -> dict[str, bytes]:
    """每个 distinct source_request_id 一个节点，boundary 归属保留全部（§4.2）。"""

    occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for capture_id, boundaries in view.boundaries_by_capture.items():
        for ordinal, source_request_id in boundaries:
            occurrences[source_request_id].append(
                {"boundary_ordinal": ordinal, "capture_occurrence_id": capture_id}
            )
    expected: dict[str, bytes] = {}
    for source_request_id, items in occurrences.items():
        node = RequestNodeV1(
            schema_version=REQUEST_NODE_SCHEMA,
            request_node_id=request_node_id(
                m1b_run_id=view.m1b_run_id, source_request_id=source_request_id
            ),
            source_request_id=source_request_id,
            boundary_occurrences=tuple(
                sorted(
                    items,
                    key=lambda item: (item["capture_occurrence_id"], item["boundary_ordinal"]),
                )
            ),
        )
        expected[node.request_node_id] = canonical_json_bytes(node.to_dict())
    return expected


def _rederive_capture_edges(view: M1bRunView) -> dict[str, bytes]:
    """独立重算 2 类 capture 级 Grade-A 边（SHARED / COMPLETE_DUP）。"""

    expected: dict[str, bytes] = {}
    for edge in (
        *_rederive_shared_source_request_edges(view),
        *_rederive_complete_duplicate_capture_edges(view),
    ):
        expected[edge.edge_id] = canonical_json_bytes(edge.to_dict())
    return expected


def _rederive_shared_source_request_edges(view: M1bRunView) -> list[CaptureRelationEdgeV1]:
    captures_by_srid: dict[str, set[str]] = defaultdict(set)
    for capture_id, boundaries in view.boundaries_by_capture.items():
        for _ordinal, source_request_id in boundaries:
            captures_by_srid[source_request_id].add(capture_id)
    shared_by_pair: dict[tuple[str, str], set[str]] = defaultdict(set)
    for source_request_id, capture_ids in captures_by_srid.items():
        if len(capture_ids) < 2:
            continue
        for left, right in combinations(sorted(capture_ids), 2):
            shared_by_pair[(left, right)].add(source_request_id)
    edges = []
    for (left, right), source_request_ids in shared_by_pair.items():
        shared_sorted = sorted(source_request_ids)
        edges.append(
            CaptureRelationEdgeV1(
                schema_version=CAPTURE_RELATION_EDGE_SCHEMA,
                edge_id=capture_relation_edge_id(
                    m1b_run_id=view.m1b_run_id,
                    relation=LineageRelation.SHARED_SOURCE_REQUEST,
                    endpoint_capture_ids=(left, right),
                    evidence_key=shared_sorted,
                ),
                relation=LineageRelation.SHARED_SOURCE_REQUEST.value,
                endpoint_capture_ids=(left, right),
                evidence={"shared_source_request_ids": shared_sorted},
            )
        )
    return edges


def _rederive_complete_duplicate_capture_edges(view: M1bRunView) -> list[CaptureRelationEdgeV1]:
    captures_by_signature: dict[tuple[str, str], list[str]] = defaultdict(list)
    for capture_id, chain in view.fingerprint_chain_by_capture.items():
        source_request_sequence = [
            source_request_id
            for _ordinal, source_request_id in view.boundaries_by_capture.get(capture_id, ())
        ]
        signature = (
            sha256_bytes(canonical_json_bytes([list(pair) for pair in chain])),
            sha256_bytes(canonical_json_bytes(source_request_sequence)),
        )
        captures_by_signature[signature].append(capture_id)
    edges = []
    for (fingerprint_digest, sequence_digest), capture_ids in captures_by_signature.items():
        if len(capture_ids) < 2:
            continue
        for left, right in combinations(sorted(capture_ids), 2):
            edges.append(
                CaptureRelationEdgeV1(
                    schema_version=CAPTURE_RELATION_EDGE_SCHEMA,
                    edge_id=capture_relation_edge_id(
                        m1b_run_id=view.m1b_run_id,
                        relation=LineageRelation.COMPLETE_DUPLICATE_CAPTURE,
                        endpoint_capture_ids=(left, right),
                        evidence_key=[fingerprint_digest, sequence_digest],
                    ),
                    relation=LineageRelation.COMPLETE_DUPLICATE_CAPTURE.value,
                    endpoint_capture_ids=(left, right),
                    evidence={
                        "visible_fingerprint_sha256": fingerprint_digest,
                        "source_request_sequence_sha256": sequence_digest,
                    },
                )
            )
    return edges


def _rederive_successor_edges(view: M1bRunView) -> dict[str, bytes]:
    witnesses_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for capture_id, boundaries in view.boundaries_by_capture.items():
        ordered = sorted(boundaries)
        for (parent_ordinal, parent_srid), (child_ordinal, child_srid) in pairwise(ordered):
            if parent_srid == child_srid:
                continue
            witnesses_by_pair[(parent_srid, child_srid)].append(
                {
                    "capture_occurrence_id": capture_id,
                    "parent_boundary_ordinal": parent_ordinal,
                    "child_boundary_ordinal": child_ordinal,
                }
            )
    expected: dict[str, bytes] = {}
    for (parent_srid, child_srid), witnesses in witnesses_by_pair.items():
        parent_node_id = request_node_id(m1b_run_id=view.m1b_run_id, source_request_id=parent_srid)
        child_node_id = request_node_id(m1b_run_id=view.m1b_run_id, source_request_id=child_srid)
        edge = RequestSuccessorEdgeV1(
            schema_version=REQUEST_SUCCESSOR_EDGE_SCHEMA,
            edge_id=request_successor_edge_id(
                m1b_run_id=view.m1b_run_id,
                parent_request_node_id=parent_node_id,
                child_request_node_id=child_node_id,
            ),
            relation=LineageRelation.EXPLICIT_REQUEST_SUCCESSOR.value,
            parent_request_node_id=parent_node_id,
            child_request_node_id=child_node_id,
            evidence=tuple(
                sorted(
                    witnesses,
                    key=lambda item: (
                        item["capture_occurrence_id"],
                        item["parent_boundary_ordinal"],
                        item["child_boundary_ordinal"],
                    ),
                )
            ),
        )
        expected[edge.edge_id] = canonical_json_bytes(edge.to_dict())
    return expected


def _bijection(
    prefix: str,
    location: str,
    expected: Mapping[str, bytes],
    observed: Mapping[str, bytes],
    issues: _Issues,
) -> None:
    """双向集合相等：expected 缺席=漏报（删边洗白），observed 多出=幻影，同键内容不符=篡改。"""

    for identifier in expected.keys() - observed.keys():
        issues.add(f"{prefix}_MISSING", location, f"独立复算存在但自报缺失：{identifier}")
    for identifier in observed.keys() - expected.keys():
        issues.add(f"{prefix}_PHANTOM", location, f"自报存在但独立复算不产出：{identifier}")
    for identifier in expected.keys() & observed.keys():
        if expected[identifier] != observed[identifier]:
            issues.add(f"{prefix}_MISMATCH", location, f"自报内容与独立复算不一致：{identifier}")


# --- 读入自报集 + 逐记录闭合值域白名单 --------------------------------------


def _read_nodes(
    root: Path,
    view: M1bRunView,
    capture_ids: frozenset[str],
    issues: _Issues,
) -> dict[str, bytes]:
    relative = "private/request_nodes.jsonl"
    observed: dict[str, bytes] = {}
    for line_number, record in enumerate(_iter_lineage_jsonl(root, relative, issues), 1):
        location = f"{relative}:{line_number}"
        node_id = record["request_node_id"]
        source_request_id = record["source_request_id"]
        if not _is_sha256(node_id):
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "request_node_id 非 64 位十六进制")
            continue
        if not isinstance(source_request_id, str) or not source_request_id:
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "source_request_id 非法")
        elif node_id != request_node_id(
            m1b_run_id=view.m1b_run_id, source_request_id=source_request_id
        ):
            issues.add("STABLE_ID_MISMATCH", location, "request_node_id 与固定身份公式不匹配")
        _check_boundary_occurrences(record["boundary_occurrences"], capture_ids, location, issues)
        if node_id in observed:
            issues.add("LINEAGE_DUPLICATE_ID", location, "request_node_id 重复")
        observed[node_id] = canonical_json_bytes(record)
    return observed


def _check_boundary_occurrences(
    value: Any,
    capture_ids: frozenset[str],
    location: str,
    issues: _Issues,
) -> None:
    if not isinstance(value, list):
        issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "boundary_occurrences 必须是数组")
        return
    for item in value:
        if not isinstance(item, dict) or set(item) != {"boundary_ordinal", "capture_occurrence_id"}:
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "boundary_occurrences 字段不闭合")
            continue
        if not _is_ordinal(item["boundary_ordinal"]):
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "boundary_ordinal 非法")
        if item["capture_occurrence_id"] not in capture_ids:
            issues.add("LINEAGE_ENDPOINT_NOT_IN_M1B", location, "capture_occurrence_id 不在 M1B")


def _read_capture_edges(
    root: Path,
    view: M1bRunView,
    capture_ids: frozenset[str],
    candidate_groups: frozenset[str],
    target_hashes: frozenset[str],
    issues: _Issues,
) -> dict[str, bytes]:
    relative = "private/capture_relation_edges.jsonl"
    observed: dict[str, bytes] = {}
    for line_number, record in enumerate(_iter_lineage_jsonl(root, relative, issues), 1):
        location = f"{relative}:{line_number}"
        edge_id = record["edge_id"]
        relation = record["relation"]
        endpoints = record["endpoint_capture_ids"]
        evidence = record["evidence"]
        if not _is_sha256(edge_id):
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "edge_id 非 64 位十六进制")
            continue
        if not _relation_at_level(
            relation, CAPTURE_LEVEL_RELATIONS, RelationDirectionality.UNDIRECTED
        ):
            issues.add(
                "LINEAGE_RELATION_LEVEL_MISMATCH", location, "relation 不是 capture 级无向 Grade-A"
            )
            continue
        if not _check_endpoints(endpoints, capture_ids, location, issues):
            continue
        _scan_forbidden_scalars(
            evidence, candidate_groups, "LINEAGE_EVIDENCE_CANDIDATE_GROUP", location, issues
        )
        _scan_forbidden_scalars(
            evidence, target_hashes, "LINEAGE_EVIDENCE_TARGET_HASH", location, issues
        )
        evidence_key = _check_capture_edge_evidence(relation, evidence, location, issues)
        if evidence_key is not None and edge_id != capture_relation_edge_id(
            m1b_run_id=view.m1b_run_id,
            relation=relation,
            endpoint_capture_ids=endpoints,
            evidence_key=evidence_key,
        ):
            issues.add("STABLE_ID_MISMATCH", location, "edge_id 与固定身份公式不匹配")
        if edge_id in observed:
            issues.add("LINEAGE_DUPLICATE_ID", location, "edge_id 重复")
        observed[edge_id] = canonical_json_bytes(record)
    return observed


def _check_capture_edge_evidence(
    relation: str,
    evidence: Any,
    location: str,
    issues: _Issues,
) -> Any:
    """按 relation 校验 evidence 闭合形状与值域，返回可复算 edge_id 的 evidence_key。"""

    if not isinstance(evidence, dict):
        issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "evidence 必须是对象")
        return None
    if relation == LineageRelation.SHARED_SOURCE_REQUEST.value:
        if set(evidence) != {"shared_source_request_ids"}:
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "SHARED evidence 字段不闭合")
            return None
        shared = evidence["shared_source_request_ids"]
        if (
            not isinstance(shared, list)
            or not shared
            or not all(isinstance(item, str) and item for item in shared)
            or list(shared) != sorted(shared)
            or len(set(shared)) != len(shared)
        ):
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "shared_source_request_ids 非法")
            return None
        return shared
    if relation == LineageRelation.COMPLETE_DUPLICATE_CAPTURE.value:
        if set(evidence) != {"visible_fingerprint_sha256", "source_request_sequence_sha256"}:
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "COMPLETE_DUP evidence 字段不闭合")
            return None
        fingerprint = evidence["visible_fingerprint_sha256"]
        sequence = evidence["source_request_sequence_sha256"]
        if not _is_sha256(fingerprint) or not _is_sha256(sequence):
            issues.add(
                "LINEAGE_PRIVATE_VALUE_DOMAIN", location, "COMPLETE_DUP 指纹非 64 位十六进制"
            )
            return None
        return [fingerprint, sequence]
    return None


def _read_successor_edges(
    root: Path,
    view: M1bRunView,
    capture_ids: frozenset[str],
    candidate_groups: frozenset[str],
    target_hashes: frozenset[str],
    expected_nodes: Mapping[str, bytes],
    issues: _Issues,
) -> dict[str, bytes]:
    relative = "private/request_successor_edges.jsonl"
    observed: dict[str, bytes] = {}
    for line_number, record in enumerate(_iter_lineage_jsonl(root, relative, issues), 1):
        location = f"{relative}:{line_number}"
        edge_id = record["edge_id"]
        relation = record["relation"]
        parent_node_id = record["parent_request_node_id"]
        child_node_id = record["child_request_node_id"]
        evidence = record["evidence"]
        if not _is_sha256(edge_id):
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "edge_id 非 64 位十六进制")
            continue
        if not _relation_at_level(
            relation, REQUEST_LEVEL_RELATIONS, RelationDirectionality.DIRECTED
        ):
            issues.add(
                "LINEAGE_RELATION_LEVEL_MISMATCH", location, "relation 不是请求级有向 Grade-A"
            )
            continue
        if not _is_sha256(parent_node_id) or not _is_sha256(child_node_id):
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "端点 request_node_id 非法")
            continue
        if parent_node_id == child_node_id:
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "后继边端点不得自环")
        if parent_node_id not in expected_nodes or child_node_id not in expected_nodes:
            issues.add("LINEAGE_ENDPOINT_NOT_IN_M1B", location, "端点 request_node_id 不在图中")
        _check_successor_evidence(evidence, capture_ids, location, issues)
        _scan_forbidden_scalars(
            evidence, candidate_groups, "LINEAGE_EVIDENCE_CANDIDATE_GROUP", location, issues
        )
        _scan_forbidden_scalars(
            evidence, target_hashes, "LINEAGE_EVIDENCE_TARGET_HASH", location, issues
        )
        if edge_id != request_successor_edge_id(
            m1b_run_id=view.m1b_run_id,
            parent_request_node_id=parent_node_id,
            child_request_node_id=child_node_id,
        ):
            issues.add("STABLE_ID_MISMATCH", location, "edge_id 与固定身份公式不匹配")
        if edge_id in observed:
            issues.add("LINEAGE_DUPLICATE_ID", location, "edge_id 重复")
        observed[edge_id] = canonical_json_bytes(record)
    return observed


def _check_successor_evidence(
    value: Any,
    capture_ids: frozenset[str],
    location: str,
    issues: _Issues,
) -> None:
    if not isinstance(value, list) or not value:
        issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "后继边 evidence 必须是非空数组")
        return
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "capture_occurrence_id",
            "parent_boundary_ordinal",
            "child_boundary_ordinal",
        }:
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "后继边见证字段不闭合")
            continue
        if item["capture_occurrence_id"] not in capture_ids:
            issues.add(
                "LINEAGE_ENDPOINT_NOT_IN_M1B", location, "见证 capture_occurrence_id 不在 M1B"
            )
        if not _is_ordinal(item["parent_boundary_ordinal"]) or not _is_ordinal(
            item["child_boundary_ordinal"]
        ):
            issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "见证 boundary_ordinal 非法")


def _check_endpoints(
    endpoints: Any,
    capture_ids: frozenset[str],
    location: str,
    issues: _Issues,
) -> bool:
    if (
        not isinstance(endpoints, list)
        or len(endpoints) != 2
        or not all(_is_sha256(item) for item in endpoints)
    ):
        issues.add(
            "LINEAGE_PRIVATE_VALUE_DOMAIN", location, "endpoint_capture_ids 必须是两个 hex ID"
        )
        return False
    left, right = endpoints
    if left == right:
        issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "无向边端点不得相同")
        return False
    if list(endpoints) != sorted(endpoints):
        issues.add("LINEAGE_PRIVATE_VALUE_DOMAIN", location, "endpoint_capture_ids 未按序写入")
        return False
    if left not in capture_ids or right not in capture_ids:
        issues.add("LINEAGE_ENDPOINT_NOT_IN_M1B", location, "endpoint_capture_ids 不在 M1B")
        return False
    return True


def _relation_at_level(
    relation: Any,
    level: frozenset[LineageRelation],
    directionality: RelationDirectionality,
) -> bool:
    """relation 是有效枚举、属该级别集合，且 (grade,directionality) 与 contracts 单一映射一致。"""

    try:
        member = LineageRelation(relation)
    except ValueError:
        return False
    if member not in level:
        return False
    _grade, observed_directionality = relation_properties(member)
    return observed_directionality is directionality


def _scan_forbidden_scalars(
    value: Any,
    forbidden: frozenset[str],
    code: str,
    location: str,
    issues: _Issues,
) -> None:
    """递归扫描 evidence，若任一标量落入禁用集合（候选组 / target_hash）即报错（门①/§5.5）。"""

    if isinstance(value, str):
        if value in forbidden:
            issues.add(code, location, "evidence 出现禁用标识")
    elif isinstance(value, dict):
        for item in value.values():
            _scan_forbidden_scalars(item, forbidden, code, location, issues)
    elif isinstance(value, list):
        for item in value:
            _scan_forbidden_scalars(item, forbidden, code, location, issues)


# --- 聚合计数重算 ----------------------------------------------------------


def _recompute_counts(
    view: M1bRunView,
    expected_nodes: Mapping[str, bytes],
    expected_capture_edges: Mapping[str, bytes],
    expected_successor_edges: Mapping[str, bytes],
) -> dict[str, int]:
    edge_counts: dict[LineageRelation, int] = {relation: 0 for relation in LineageRelation}
    edge_counts[LineageRelation.EXPLICIT_REQUEST_SUCCESSOR] = len(expected_successor_edges)
    for raw in expected_capture_edges.values():
        relation = LineageRelation(strict_json_loads(raw)["relation"])
        edge_counts[relation] += 1
    return build_report_counts(
        capture_count=len(view.capture_ids),
        request_node_count=len(expected_nodes),
        candidate_group_count=len(set(view.candidate_group_by_capture.values())),
        edge_counts_by_relation=edge_counts,
    )


def _compare_report(
    report: Mapping[str, Any] | None,
    observed: Mapping[str, int],
    issues: _Issues,
) -> None:
    if not isinstance(report, dict) or not isinstance(report.get("counts"), dict):
        return
    for key in sorted(LINEAGE_COUNT_KEYS):
        if report["counts"].get(key) != observed.get(key):
            issues.add(
                "LINEAGE_REPORT_COUNT_MISMATCH",
                f"reports/lineage_report.json/counts/{key}",
                f"报告={report['counts'].get(key)}，重算={observed.get(key)}",
            )


# --- 通用读入/校验原语（自有代码路径，不 import M1B validator 私有实现）--------


def _iter_lineage_jsonl(root: Path, relative: str, issues: _Issues) -> Iterable[dict[str, Any]]:
    path = root / relative
    if not path.is_file():
        return
    schema, contract = _LINEAGE_PRIVATE_CONTRACTS[relative]
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
    """兜底扫描 Data URL / reasoning；普通 URL/绝对路径不在其覆盖内，由值域白名单与路径扫描把关。"""

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
    expected_fields = {field.name for field in fields(contract)}
    if set(value) != expected_fields:
        issues.add("SCHEMA_FIELDS_MISMATCH", location, "字段集合与当前 contract 不一致")
        return False
    if schema is not None and value.get("schema_version") != schema:
        issues.add("SCHEMA_VERSION_MISMATCH", location, "schema_version 不匹配")
        return False
    return True


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_ordinal(value: Any) -> bool:
    return _is_int(value) and value >= 0


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_git_object_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )
