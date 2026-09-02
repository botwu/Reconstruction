"""M1C 纯函数建图内核测试：4 类 Grade-A 边的枚举、去重、排序与门④资格。

直接驱动 ``build_lineage_graph``（无 IO），用手构输入精确控制 srid 序列 / 指纹链 / 哈希，
覆盖每类关系的正常与边界（含空桶、单元素桶、自环抑制、跨 capture 去重）。跨候选组行为在
pipeline/validation 测试里用真实 M1B run 验证——建图内核本就组盲、看不到候选组。
"""

from __future__ import annotations

from traceforge.lineage.builder import build_lineage_graph
from traceforge.lineage.contracts import (
    LineageRelation,
    capture_relation_edge_id,
    request_node_id,
)

_RUN = "m1b-run-fixture"


def _build(**overrides):
    """以最小合法默认建图，允许逐字段覆盖。"""

    kwargs = {
        "m1b_run_id": _RUN,
        "capture_ids": ["cap-a", "cap-b"],
        "raw_request_hash_by_capture": {"cap-a": "a" * 64, "cap-b": "a" * 64},
        "boundaries_by_capture": {
            "cap-a": ((0, "req-1"),),
            "cap-b": ((0, "req-1"),),
        },
        "fingerprint_chain_by_capture": {
            "cap-a": (("ASSISTANT_MESSAGE", "d" * 64),),
            "cap-b": (("ASSISTANT_MESSAGE", "e" * 64),),
        },
    }
    kwargs.update(overrides)
    return build_lineage_graph(**kwargs)


def _edges_of(graph, relation: LineageRelation):
    return [e for e in graph.capture_relation_edges if e.relation == relation.value]


# --- SHARED_SOURCE_REQUEST ------------------------------------------------


def test_shared_source_request_pairs_sharing_captures() -> None:
    """共享同一 srid 的 capture 对建一条无向边，evidence 汇集共享 srid。"""

    graph = _build(
        boundaries_by_capture={
            "cap-a": ((0, "req-1"), (1, "req-2")),
            "cap-b": ((0, "req-2"), (1, "req-3")),
        },
    )
    shared = _edges_of(graph, LineageRelation.SHARED_SOURCE_REQUEST)
    assert len(shared) == 1
    edge = shared[0]
    assert edge.endpoint_capture_ids == ("cap-a", "cap-b")
    assert edge.evidence == {"shared_source_request_ids": ["req-2"]}
    assert graph.edge_counts_by_relation[LineageRelation.SHARED_SOURCE_REQUEST] == 1


def test_shared_source_request_three_way_bucket_is_pairwise() -> None:
    """三个 capture 同享一个 srid → 3 条两两边。"""

    graph = _build(
        capture_ids=["cap-a", "cap-b", "cap-c"],
        raw_request_hash_by_capture={c: "a" * 64 for c in ("cap-a", "cap-b", "cap-c")},
        boundaries_by_capture={
            "cap-a": ((0, "req-1"),),
            "cap-b": ((0, "req-1"),),
            "cap-c": ((0, "req-1"),),
        },
        fingerprint_chain_by_capture={
            "cap-a": (("K", "1" * 64),),
            "cap-b": (("K", "2" * 64),),
            "cap-c": (("K", "3" * 64),),
        },
    )
    shared = _edges_of(graph, LineageRelation.SHARED_SOURCE_REQUEST)
    assert {e.endpoint_capture_ids for e in shared} == {
        ("cap-a", "cap-b"),
        ("cap-a", "cap-c"),
        ("cap-b", "cap-c"),
    }


def test_shared_source_request_singleton_bucket_makes_no_edge() -> None:
    """srid 只出现在一个 capture → 不建边。"""

    graph = _build(
        boundaries_by_capture={
            "cap-a": ((0, "req-1"),),
            "cap-b": ((0, "req-2"),),
        },
    )
    assert _edges_of(graph, LineageRelation.SHARED_SOURCE_REQUEST) == []


# --- EXPLICIT_REQUEST_SUCCESSOR -------------------------------------------


def test_explicit_successor_adjacent_distinct_srid() -> None:
    """capture 内相邻不同 srid → parent→child 有向边。"""

    graph = _build(
        boundaries_by_capture={"cap-a": ((0, "req-1"), (1, "req-2"))},
        capture_ids=["cap-a"],
        raw_request_hash_by_capture={"cap-a": "a" * 64},
        fingerprint_chain_by_capture={"cap-a": (("K", "d" * 64),)},
    )
    assert len(graph.request_successor_edges) == 1
    edge = graph.request_successor_edges[0]
    assert edge.parent_request_node_id == request_node_id(
        m1b_run_id=_RUN, source_request_id="req-1"
    )
    assert edge.child_request_node_id == request_node_id(m1b_run_id=_RUN, source_request_id="req-2")
    assert edge.relation == LineageRelation.EXPLICIT_REQUEST_SUCCESSOR.value


def test_explicit_successor_no_self_loop_on_same_srid() -> None:
    """相邻相同 srid 不建自环（§5.2）。"""

    graph = _build(
        boundaries_by_capture={"cap-a": ((0, "req-1"), (1, "req-1"))},
        capture_ids=["cap-a"],
        raw_request_hash_by_capture={"cap-a": "a" * 64},
        fingerprint_chain_by_capture={"cap-a": (("K", "d" * 64),)},
    )
    assert graph.request_successor_edges == ()


def test_explicit_successor_dedups_across_captures_into_one_edge() -> None:
    """两个 capture 各自见证 req-1→req-2 → 去重为一条边、见证汇全。"""

    graph = _build(
        boundaries_by_capture={
            "cap-a": ((0, "req-1"), (1, "req-2")),
            "cap-b": ((0, "req-1"), (1, "req-2")),
        },
    )
    assert len(graph.request_successor_edges) == 1
    witnesses = graph.request_successor_edges[0].evidence
    assert {w["capture_occurrence_id"] for w in witnesses} == {"cap-a", "cap-b"}


# --- IDENTICAL_RAW_REQUEST_HASH -------------------------------------------


def test_identical_raw_request_hash_edge_and_counts() -> None:
    """两 capture 同一合格 hash → 边；qualified 计数为 2。"""

    graph = _build(raw_request_hash_by_capture={"cap-a": "a" * 64, "cap-b": "a" * 64})
    identical = _edges_of(graph, LineageRelation.IDENTICAL_RAW_REQUEST_HASH)
    assert len(identical) == 1
    assert identical[0].evidence == {"raw_request_hash": "a" * 64}
    assert graph.raw_request_hash_qualified_count == 2
    assert graph.raw_request_hash_unknown_count == 0


def test_identical_raw_request_hash_excludes_unknown() -> None:
    """非 64-hex 的 UNKNOWN capture 缺席建边，仅计入 unknown 计数（门④）。"""

    graph = _build(
        capture_ids=["cap-a", "cap-b", "cap-c"],
        raw_request_hash_by_capture={
            "cap-a": "a" * 64,
            "cap-b": "a" * 64,
            "cap-c": "NOT-A-HASH",
        },
        boundaries_by_capture={
            "cap-a": ((0, "req-1"),),
            "cap-b": ((0, "req-2"),),
            "cap-c": ((0, "req-3"),),
        },
        fingerprint_chain_by_capture={
            "cap-a": (("K", "1" * 64),),
            "cap-b": (("K", "2" * 64),),
            "cap-c": (("K", "3" * 64),),
        },
    )
    identical = _edges_of(graph, LineageRelation.IDENTICAL_RAW_REQUEST_HASH)
    assert len(identical) == 1
    assert identical[0].endpoint_capture_ids == ("cap-a", "cap-b")
    assert graph.raw_request_hash_qualified_count == 2
    assert graph.raw_request_hash_unknown_count == 1


def test_identical_raw_request_hash_distinct_hashes_no_edge() -> None:
    """不同合格 hash 各自成桶 → 无边。"""

    graph = _build(raw_request_hash_by_capture={"cap-a": "a" * 64, "cap-b": "b" * 64})
    assert _edges_of(graph, LineageRelation.IDENTICAL_RAW_REQUEST_HASH) == []


# --- COMPLETE_DUPLICATE_CAPTURE -------------------------------------------


def test_complete_duplicate_requires_both_signatures_equal() -> None:
    """指纹链摘要与 srid 序列摘要双双相等才建边。"""

    graph = _build(
        boundaries_by_capture={
            "cap-a": ((0, "req-1"),),
            "cap-b": ((0, "req-1"),),
        },
        fingerprint_chain_by_capture={
            "cap-a": (("K", "d" * 64),),
            "cap-b": (("K", "d" * 64),),
        },
    )
    dup = _edges_of(graph, LineageRelation.COMPLETE_DUPLICATE_CAPTURE)
    assert len(dup) == 1
    assert dup[0].endpoint_capture_ids == ("cap-a", "cap-b")
    assert set(dup[0].evidence) == {
        "visible_fingerprint_sha256",
        "source_request_sequence_sha256",
    }


def test_complete_duplicate_differing_fingerprint_no_edge() -> None:
    """srid 序列相同但指纹链不同 → 不建边。"""

    graph = _build(
        boundaries_by_capture={
            "cap-a": ((0, "req-1"),),
            "cap-b": ((0, "req-1"),),
        },
        fingerprint_chain_by_capture={
            "cap-a": (("K", "d" * 64),),
            "cap-b": (("K", "e" * 64),),
        },
    )
    assert _edges_of(graph, LineageRelation.COMPLETE_DUPLICATE_CAPTURE) == []


def test_complete_duplicate_differing_sequence_no_edge() -> None:
    """指纹链相同但 srid 序列不同 → 不建边。"""

    graph = _build(
        boundaries_by_capture={
            "cap-a": ((0, "req-1"),),
            "cap-b": ((0, "req-2"),),
        },
        fingerprint_chain_by_capture={
            "cap-a": (("K", "d" * 64),),
            "cap-b": (("K", "d" * 64),),
        },
    )
    assert _edges_of(graph, LineageRelation.COMPLETE_DUPLICATE_CAPTURE) == []


# --- 节点守恒、排序、确定性 ------------------------------------------------


def test_request_nodes_one_per_distinct_srid_with_full_occurrences() -> None:
    """每个 distinct srid 一个节点；boundary 归属保留全部、不去重丢弃。"""

    graph = _build(
        boundaries_by_capture={
            "cap-a": ((0, "req-1"), (1, "req-2")),
            "cap-b": ((0, "req-1"),),
        },
    )
    by_srid = {n.source_request_id: n for n in graph.request_nodes}
    assert set(by_srid) == {"req-1", "req-2"}
    assert graph.request_node_count == 2
    # req-1 在两个 capture 中各出现一次 → 两条 occurrence。
    owners = {o["capture_occurrence_id"] for o in by_srid["req-1"].boundary_occurrences}
    assert owners == {"cap-a", "cap-b"}


def test_edges_and_nodes_sorted_by_stable_id() -> None:
    """所有产物按稳定 ID 升序，保证两次构建逐字节一致。"""

    graph = _build(
        capture_ids=["cap-a", "cap-b", "cap-c"],
        raw_request_hash_by_capture={c: "a" * 64 for c in ("cap-a", "cap-b", "cap-c")},
        boundaries_by_capture={
            "cap-a": ((0, "req-1"),),
            "cap-b": ((0, "req-1"),),
            "cap-c": ((0, "req-1"),),
        },
        fingerprint_chain_by_capture={
            "cap-a": (("K", "1" * 64),),
            "cap-b": (("K", "2" * 64),),
            "cap-c": (("K", "3" * 64),),
        },
    )
    node_ids = [n.request_node_id for n in graph.request_nodes]
    assert node_ids == sorted(node_ids)
    edge_ids = [e.edge_id for e in graph.capture_relation_edges]
    assert edge_ids == sorted(edge_ids)


def test_endpoint_ids_canonically_sorted_in_edge() -> None:
    """无向边端点写入前排序，且 edge_id 与端点排序一致（§5.6）。"""

    graph = _build(raw_request_hash_by_capture={"cap-z": "a" * 64, "cap-a": "a" * 64})
    identical = _edges_of(graph, LineageRelation.IDENTICAL_RAW_REQUEST_HASH)
    assert len(identical) == 1
    edge = identical[0]
    assert edge.endpoint_capture_ids == ("cap-a", "cap-z")
    assert edge.edge_id == capture_relation_edge_id(
        m1b_run_id=_RUN,
        relation=LineageRelation.IDENTICAL_RAW_REQUEST_HASH,
        endpoint_capture_ids=("cap-a", "cap-z"),
        evidence_key="a" * 64,
    )
