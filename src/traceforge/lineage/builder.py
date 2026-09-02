"""M1C 关系派生的纯函数内核：RequestNode + 4 类 Grade-A 边。

本模块**无任何 IO**，只吃 `reader.M1bRunView` 抽好的结构/指纹字段，输出内存中的
``LineageGraph``。它**根本不接收 ``candidate_group_id`` 参数**——因此在类型层面就不可能把「同
候选组」写进任何边，结构性坐实门①（BLOCKING_HINT_ONLY）与门②（Grade-A 全局组盲）。

所有 Grade-A 一律「全局键桶」枚举（规格 §6），候选组至多影响枚举顺序、绝不改变边集。所有产出
在写盘前按稳定 ID 排序，保证两次构建逐字节一致。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations, pairwise
from typing import Any

from traceforge.lineage.contracts import (
    CAPTURE_RELATION_EDGE_SCHEMA,
    REQUEST_NODE_SCHEMA,
    REQUEST_SUCCESSOR_EDGE_SCHEMA,
    CaptureRelationEdgeV1,
    LineageRelation,
    RawRequestHashStatus,
    RequestNodeV1,
    RequestSuccessorEdgeV1,
    capture_relation_edge_id,
    classify_raw_request_hash,
    request_node_id,
    request_successor_edge_id,
)
from traceforge.trajectory.json_codec import canonical_json_bytes, sha256_bytes


@dataclass(frozen=True, slots=True)
class LineageGraph:
    """一次建图的全部内存产物与聚合计数（不含 IO/manifest）。"""

    request_nodes: tuple[RequestNodeV1, ...]
    request_successor_edges: tuple[RequestSuccessorEdgeV1, ...]
    capture_relation_edges: tuple[CaptureRelationEdgeV1, ...]
    capture_count: int
    request_node_count: int
    raw_request_hash_qualified_count: int
    raw_request_hash_unknown_count: int
    edge_counts_by_relation: dict[LineageRelation, int]


def _digest(value: Any) -> str:
    """对 canonical JSON 取 SHA-256，用于指纹链与 srid 序列的最小可重算摘要。"""

    return sha256_bytes(canonical_json_bytes(value))


def _build_request_nodes(
    *,
    m1b_run_id: str,
    boundaries_by_capture: Mapping[str, Sequence[tuple[int, str]]],
) -> tuple[RequestNodeV1, ...]:
    """每个 distinct ``source_request_id`` 一个节点；boundary 归属保留全部、不去重丢弃（§4.2）。"""

    occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for capture_id, boundaries in boundaries_by_capture.items():
        for ordinal, source_request_id in boundaries:
            occurrences[source_request_id].append(
                {"boundary_ordinal": ordinal, "capture_occurrence_id": capture_id}
            )
    nodes = [
        RequestNodeV1(
            schema_version=REQUEST_NODE_SCHEMA,
            request_node_id=request_node_id(
                m1b_run_id=m1b_run_id, source_request_id=source_request_id
            ),
            source_request_id=source_request_id,
            boundary_occurrences=tuple(
                sorted(
                    items,
                    key=lambda item: (item["capture_occurrence_id"], item["boundary_ordinal"]),
                )
            ),
        )
        for source_request_id, items in occurrences.items()
    ]
    return tuple(sorted(nodes, key=lambda node: node.request_node_id))


def _build_shared_source_request_edges(
    *,
    m1b_run_id: str,
    boundaries_by_capture: Mapping[str, Sequence[tuple[int, str]]],
) -> tuple[CaptureRelationEdgeV1, ...]:
    """全局倒排桶：共享 ≥1 个 srid 的每一对 capture 建一条边，evidence 汇全部共享 srid。"""

    captures_by_srid: dict[str, set[str]] = defaultdict(set)
    for capture_id, boundaries in boundaries_by_capture.items():
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
        evidence = {"shared_source_request_ids": shared_sorted}
        edges.append(
            CaptureRelationEdgeV1(
                schema_version=CAPTURE_RELATION_EDGE_SCHEMA,
                edge_id=capture_relation_edge_id(
                    m1b_run_id=m1b_run_id,
                    relation=LineageRelation.SHARED_SOURCE_REQUEST,
                    endpoint_capture_ids=(left, right),
                    evidence_key=shared_sorted,
                ),
                relation=LineageRelation.SHARED_SOURCE_REQUEST.value,
                endpoint_capture_ids=(left, right),
                evidence=evidence,
            )
        )
    return tuple(sorted(edges, key=lambda edge: edge.edge_id))


def _build_identical_raw_request_hash_edges(
    *,
    m1b_run_id: str,
    raw_request_hash_by_capture: Mapping[str, Any],
) -> tuple[tuple[CaptureRelationEdgeV1, ...], int, int]:
    """全局哈希桶：仅 §7 QUALIFIED 值参与；桶内两两建边。返回 (边, qualified 数, unknown 数)。"""

    qualified_count = 0
    unknown_count = 0
    captures_by_hash: dict[str, list[str]] = defaultdict(list)
    for capture_id, raw_request_hash in raw_request_hash_by_capture.items():
        if classify_raw_request_hash(raw_request_hash) is RawRequestHashStatus.QUALIFIED:
            qualified_count += 1
            captures_by_hash[raw_request_hash].append(capture_id)
        else:
            unknown_count += 1

    edges = []
    for raw_request_hash, capture_ids in captures_by_hash.items():
        if len(capture_ids) < 2:
            continue
        for left, right in combinations(sorted(capture_ids), 2):
            edges.append(
                CaptureRelationEdgeV1(
                    schema_version=CAPTURE_RELATION_EDGE_SCHEMA,
                    edge_id=capture_relation_edge_id(
                        m1b_run_id=m1b_run_id,
                        relation=LineageRelation.IDENTICAL_RAW_REQUEST_HASH,
                        endpoint_capture_ids=(left, right),
                        evidence_key=raw_request_hash,
                    ),
                    relation=LineageRelation.IDENTICAL_RAW_REQUEST_HASH.value,
                    endpoint_capture_ids=(left, right),
                    evidence={"raw_request_hash": raw_request_hash},
                )
            )
    return tuple(sorted(edges, key=lambda edge: edge.edge_id)), qualified_count, unknown_count


def _build_complete_duplicate_capture_edges(
    *,
    m1b_run_id: str,
    boundaries_by_capture: Mapping[str, Sequence[tuple[int, str]]],
    fingerprint_chain_by_capture: Mapping[str, Sequence[tuple[str, str]]],
) -> tuple[CaptureRelationEdgeV1, ...]:
    """全局桶：可见指纹链摘要与 srid 序列摘要**双双相等**才建边（§5.4），仅建边不删 capture。"""

    captures_by_signature: dict[tuple[str, str], list[str]] = defaultdict(list)
    for capture_id, chain in fingerprint_chain_by_capture.items():
        source_request_sequence = [
            source_request_id
            for _ordinal, source_request_id in boundaries_by_capture.get(capture_id, ())
        ]
        signature = (
            _digest([list(pair) for pair in chain]),
            _digest(source_request_sequence),
        )
        captures_by_signature[signature].append(capture_id)

    edges = []
    for (fingerprint_digest, sequence_digest), capture_ids in captures_by_signature.items():
        if len(capture_ids) < 2:
            continue
        evidence_key = [fingerprint_digest, sequence_digest]
        for left, right in combinations(sorted(capture_ids), 2):
            edges.append(
                CaptureRelationEdgeV1(
                    schema_version=CAPTURE_RELATION_EDGE_SCHEMA,
                    edge_id=capture_relation_edge_id(
                        m1b_run_id=m1b_run_id,
                        relation=LineageRelation.COMPLETE_DUPLICATE_CAPTURE,
                        endpoint_capture_ids=(left, right),
                        evidence_key=evidence_key,
                    ),
                    relation=LineageRelation.COMPLETE_DUPLICATE_CAPTURE.value,
                    endpoint_capture_ids=(left, right),
                    evidence={
                        "visible_fingerprint_sha256": fingerprint_digest,
                        "source_request_sequence_sha256": sequence_digest,
                    },
                )
            )
    return tuple(sorted(edges, key=lambda edge: edge.edge_id))


def _build_explicit_request_successor_edges(
    *,
    m1b_run_id: str,
    boundaries_by_capture: Mapping[str, Sequence[tuple[int, str]]],
) -> tuple[RequestSuccessorEdgeV1, ...]:
    """capture 内 boundary_ordinal 相邻的不同 srid → parent→child；跨 capture/位置去重为一条边。"""

    witnesses_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for capture_id, boundaries in boundaries_by_capture.items():
        ordered = sorted(boundaries)
        for (parent_ordinal, parent_srid), (child_ordinal, child_srid) in pairwise(ordered):
            if parent_srid == child_srid:  # 相同 srid 不建自环（§5.2）
                continue
            witnesses_by_pair[(parent_srid, child_srid)].append(
                {
                    "capture_occurrence_id": capture_id,
                    "parent_boundary_ordinal": parent_ordinal,
                    "child_boundary_ordinal": child_ordinal,
                }
            )

    edges = []
    for (parent_srid, child_srid), witnesses in witnesses_by_pair.items():
        parent_node_id = request_node_id(m1b_run_id=m1b_run_id, source_request_id=parent_srid)
        child_node_id = request_node_id(m1b_run_id=m1b_run_id, source_request_id=child_srid)
        evidence = tuple(
            sorted(
                witnesses,
                key=lambda item: (
                    item["capture_occurrence_id"],
                    item["parent_boundary_ordinal"],
                    item["child_boundary_ordinal"],
                ),
            )
        )
        edges.append(
            RequestSuccessorEdgeV1(
                schema_version=REQUEST_SUCCESSOR_EDGE_SCHEMA,
                edge_id=request_successor_edge_id(
                    m1b_run_id=m1b_run_id,
                    parent_request_node_id=parent_node_id,
                    child_request_node_id=child_node_id,
                ),
                relation=LineageRelation.EXPLICIT_REQUEST_SUCCESSOR.value,
                parent_request_node_id=parent_node_id,
                child_request_node_id=child_node_id,
                evidence=evidence,
            )
        )
    return tuple(sorted(edges, key=lambda edge: edge.edge_id))


def build_lineage_graph(
    *,
    m1b_run_id: str,
    capture_ids: Iterable[str],
    raw_request_hash_by_capture: Mapping[str, Any],
    boundaries_by_capture: Mapping[str, Sequence[tuple[int, str]]],
    fingerprint_chain_by_capture: Mapping[str, Sequence[tuple[str, str]]],
) -> LineageGraph:
    """从已抽取的 M1B 字段派生全部 RequestNode 与 4 类 Grade-A 边（全局组盲）。"""

    request_nodes = _build_request_nodes(
        m1b_run_id=m1b_run_id, boundaries_by_capture=boundaries_by_capture
    )
    shared_edges = _build_shared_source_request_edges(
        m1b_run_id=m1b_run_id, boundaries_by_capture=boundaries_by_capture
    )
    identical_edges, qualified_count, unknown_count = _build_identical_raw_request_hash_edges(
        m1b_run_id=m1b_run_id, raw_request_hash_by_capture=raw_request_hash_by_capture
    )
    complete_duplicate_edges = _build_complete_duplicate_capture_edges(
        m1b_run_id=m1b_run_id,
        boundaries_by_capture=boundaries_by_capture,
        fingerprint_chain_by_capture=fingerprint_chain_by_capture,
    )
    successor_edges = _build_explicit_request_successor_edges(
        m1b_run_id=m1b_run_id, boundaries_by_capture=boundaries_by_capture
    )

    capture_relation_edges = tuple(
        sorted(
            (*shared_edges, *identical_edges, *complete_duplicate_edges),
            key=lambda edge: edge.edge_id,
        )
    )
    edge_counts_by_relation = {
        LineageRelation.SHARED_SOURCE_REQUEST: len(shared_edges),
        LineageRelation.EXPLICIT_REQUEST_SUCCESSOR: len(successor_edges),
        LineageRelation.IDENTICAL_RAW_REQUEST_HASH: len(identical_edges),
        LineageRelation.COMPLETE_DUPLICATE_CAPTURE: len(complete_duplicate_edges),
    }
    return LineageGraph(
        request_nodes=request_nodes,
        request_successor_edges=successor_edges,
        capture_relation_edges=capture_relation_edges,
        capture_count=len(tuple(capture_ids)),
        request_node_count=len(request_nodes),
        raw_request_hash_qualified_count=qualified_count,
        raw_request_hash_unknown_count=unknown_count,
        edge_counts_by_relation=edge_counts_by_relation,
    )
