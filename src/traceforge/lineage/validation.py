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

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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
from traceforge.trajectory.json_codec import canonical_json_bytes, sha256_bytes, strict_json_loads
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


@dataclass(frozen=True, slots=True)
class LineageValidationResult:
    """完整 lineage run 的验收结果。"""

    ok: bool
    issues: tuple[ValidationIssue, ...]
    observed_counts: dict[str, int]
    checked_file_count: int

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(f"[{issue.code}] {issue.location}: {issue.message}" for issue in self.issues)


# --- 顶层编排 --------------------------------------------------------------


def validate_lineage_run(
    lineage_run_dir: str | Path,
    m1b_run_dir: str | Path,
) -> LineageValidationResult:
    """独立校验一个已发布 M1C lineage run，取上游 M1B run 作完整边集复算的 oracle。"""

    root = Path(lineage_run_dir)
    issues = IssueCollector()
    if not root.is_dir():
        issues.add("LINEAGE_RUN_NOT_DIRECTORY", "run", "lineage_run_dir 不是可读目录")
        return LineageValidationResult(False, issues.finish(), {}, 0)

    artifact_manifest = read_json(root / "artifact_manifest.json", issues)
    entries = manifest_entries(
        root,
        artifact_manifest,
        contract=LineageArtifactManifestV1,
        schema=LINEAGE_ARTIFACT_MANIFEST_SCHEMA,
        expected_files=_DETERMINISTIC_FILES,
        issues=issues,
    )
    checked = check_artifact_bytes(root, entries, issues)
    check_inventory(root, _ALL_FILES, issues)

    lineage_manifest = read_json(root / "lineage_manifest.json", issues)
    check_contract(
        lineage_manifest,
        LineageManifestV1,
        LINEAGE_MANIFEST_SCHEMA,
        "lineage_manifest.json",
        issues,
    )
    report = read_json(root / "reports/lineage_report.json", issues)
    _check_report_shape(report, issues)
    receipt = read_json(root / "run_receipt.json", issues)
    check_receipt(
        root,
        receipt,
        artifact_manifest,
        receipt_schema=LINEAGE_RUN_RECEIPT_SCHEMA,
        manifest_run_id_field="lineage_run_id",
        issues=issues,
    )

    for control, location in (
        (lineage_manifest, "lineage_manifest.json"),
        (artifact_manifest, "artifact_manifest.json"),
        (report, "reports/lineage_report.json"),
        (receipt, "run_receipt.json"),
    ):
        scan_control_pathlike(
            control, location, code="LINEAGE_CONTROL_ABSOLUTE_PATH", issues=issues
        )

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


# --- 公共报告形状（模块特有的闭合 allowlist）------------------------------------


def _check_report_shape(report: dict[str, Any] | None, issues: IssueCollector) -> None:
    """公共报告：闭合 schema + 仅固定聚合计数键 + 非负整数（不含原始 ID/正文/路径）。"""

    if not check_contract(
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
    if any(not is_int(value) or value < 0 for value in counts.values()):
        issues.add(
            "LINEAGE_REPORT_VALUE_INVALID",
            "reports/lineage_report.json/counts",
            "公共报告计数必须是非负整数",
        )


# --- 内容寻址绑定 ----------------------------------------------------------


def _check_bindings(
    root: Path,
    view: M1bRunView,
    lineage_manifest: dict[str, Any] | None,
    artifact_manifest: dict[str, Any] | None,
    report: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    issues: IssueCollector,
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


def _check_candidate_group_partition(view: M1bRunView, issues: IssueCollector) -> None:
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
    issues: IssueCollector,
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

    bijection("LINEAGE_NODE", "private/request_nodes.jsonl", expected_nodes, observed_nodes, issues)
    bijection(
        "LINEAGE_EDGE",
        "private/capture_relation_edges.jsonl",
        expected_capture_edges,
        observed_capture_edges,
        issues,
    )
    bijection(
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


# --- 读入自报集 + 逐记录闭合值域白名单 --------------------------------------


def _read_nodes(
    root: Path,
    view: M1bRunView,
    capture_ids: frozenset[str],
    issues: IssueCollector,
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
    issues: IssueCollector,
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
    issues: IssueCollector,
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
    issues: IssueCollector,
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
    issues: IssueCollector,
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
    issues: IssueCollector,
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
    issues: IssueCollector,
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
    issues: IssueCollector,
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
    issues: IssueCollector,
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


# --- 模块特有的读入/值域原语 ----------------------------------------------------


def _iter_lineage_jsonl(
    root: Path, relative: str, issues: IssueCollector
) -> Iterable[dict[str, Any]]:
    schema, contract = _LINEAGE_PRIVATE_CONTRACTS[relative]
    return iter_canonical_jsonl(root, relative, schema=schema, contract=contract, issues=issues)


def _is_ordinal(value: Any) -> bool:
    return is_int(value) and value >= 0


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
