"""M1C 契约层测试：关系枚举封闭性、映射一致性、稳定 ID 公式、报告计数键集。"""

from __future__ import annotations

import pytest

from traceforge.lineage.contracts import (
    CAPTURE_LEVEL_RELATIONS,
    LINEAGE_COUNT_KEYS,
    REQUEST_LEVEL_RELATIONS,
    LineageRelation,
    RawRequestHashStatus,
    RelationDirectionality,
    RelationGrade,
    build_report_counts,
    capture_relation_edge_id,
    classify_raw_request_hash,
    edge_count_key,
    lineage_run_id,
    relation_properties,
    request_node_id,
    request_successor_edge_id,
)


def test_relation_enum_is_exactly_four_grade_a() -> None:
    """本次范围锁定：枚举恰好 4 类 Grade-A，不多不少。"""

    assert {relation.value for relation in LineageRelation} == {
        "SHARED_SOURCE_REQUEST",
        "EXPLICIT_REQUEST_SUCCESSOR",
        "IDENTICAL_RAW_REQUEST_HASH",
        "COMPLETE_DUPLICATE_CAPTURE",
    }
    assert len(LineageRelation) == 4


@pytest.mark.parametrize("forbidden", ["NORMALIZED_VISIBLE_PREFIX_OF", "UNKNOWN_LINEAGE"])
def test_deferred_gradeb_and_placeholder_absent(forbidden: str) -> None:
    """守护：Grade-B 关系与任何占位值都不得提前混入枚举（YAGNI）。"""

    assert forbidden not in {relation.value for relation in LineageRelation}
    assert forbidden not in LineageRelation._value2member_map_


def test_every_relation_is_grade_a() -> None:
    """本次实现的四类关系分级全部为 A。"""

    for relation in LineageRelation:
        grade, _ = relation_properties(relation)
        assert grade is RelationGrade.A


def test_relation_directionality_mapping() -> None:
    """仅 EXPLICIT_REQUEST_SUCCESSOR 有向，其余三类无向（规格 §4.3）。"""

    assert relation_properties(LineageRelation.EXPLICIT_REQUEST_SUCCESSOR)[1] is (
        RelationDirectionality.DIRECTED
    )
    for relation in (
        LineageRelation.SHARED_SOURCE_REQUEST,
        LineageRelation.IDENTICAL_RAW_REQUEST_HASH,
        LineageRelation.COMPLETE_DUPLICATE_CAPTURE,
    ):
        assert relation_properties(relation)[1] is RelationDirectionality.UNDIRECTED


def test_relation_properties_accepts_raw_string() -> None:
    """映射函数亦接受关系的原始字符串值（读取方投影用）。"""

    assert relation_properties("SHARED_SOURCE_REQUEST") == (
        RelationGrade.A,
        RelationDirectionality.UNDIRECTED,
    )


def test_relation_properties_rejects_unknown() -> None:
    """未知关系名抛错，不静默返回默认。"""

    with pytest.raises(ValueError):
        relation_properties("NORMALIZED_VISIBLE_PREFIX_OF")


def test_capture_and_request_level_partition() -> None:
    """capture 级与请求级关系恰好二分覆盖全枚举且互斥。"""

    assert set(LineageRelation) == CAPTURE_LEVEL_RELATIONS | REQUEST_LEVEL_RELATIONS
    assert set() == CAPTURE_LEVEL_RELATIONS & REQUEST_LEVEL_RELATIONS


@pytest.mark.parametrize(
    "value",
    [
        "0" * 64,
        "a" * 64,
        "0123456789abcdef" * 4,
    ],
)
def test_classify_raw_request_hash_qualified(value: str) -> None:
    """恰好 64 位全小写十六进制才算合格。"""

    assert classify_raw_request_hash(value) is RawRequestHashStatus.QUALIFIED


@pytest.mark.parametrize(
    "value",
    [
        "A" * 64,  # 大写不合格
        "a" * 63,  # 过短
        "a" * 65,  # 过长
        "g" * 64,  # 非十六进制字符
        "",
        None,
        12345,
        ["a" * 64],
    ],
)
def test_classify_raw_request_hash_unknown(value: object) -> None:
    """任何不满足格式契约的取值一律 UNKNOWN（门④排除依据）。"""

    assert classify_raw_request_hash(value) is RawRequestHashStatus.UNKNOWN


def test_request_node_id_is_deterministic_and_binds_run() -> None:
    """同 (run, srid) 稳定；换 run 或 srid 即变。"""

    first = request_node_id(m1b_run_id="run-x", source_request_id="req-1")
    assert first == request_node_id(m1b_run_id="run-x", source_request_id="req-1")
    assert first != request_node_id(m1b_run_id="run-y", source_request_id="req-1")
    assert first != request_node_id(m1b_run_id="run-x", source_request_id="req-2")


def test_capture_relation_edge_id_is_endpoint_order_invariant() -> None:
    """无向边端点顺序不影响身份（内部排序）。"""

    left = capture_relation_edge_id(
        m1b_run_id="run-x",
        relation=LineageRelation.SHARED_SOURCE_REQUEST,
        endpoint_capture_ids=("cap-a", "cap-b"),
        evidence_key=["req-1"],
    )
    right = capture_relation_edge_id(
        m1b_run_id="run-x",
        relation=LineageRelation.SHARED_SOURCE_REQUEST,
        endpoint_capture_ids=("cap-b", "cap-a"),
        evidence_key=["req-1"],
    )
    assert left == right


def test_capture_relation_edge_id_separates_relation_and_evidence() -> None:
    """关系与 evidence_key 都进身份：任一不同即不同边。"""

    base = capture_relation_edge_id(
        m1b_run_id="run-x",
        relation=LineageRelation.SHARED_SOURCE_REQUEST,
        endpoint_capture_ids=("cap-a", "cap-b"),
        evidence_key=["req-1"],
    )
    other_relation = capture_relation_edge_id(
        m1b_run_id="run-x",
        relation=LineageRelation.IDENTICAL_RAW_REQUEST_HASH,
        endpoint_capture_ids=("cap-a", "cap-b"),
        evidence_key=["req-1"],
    )
    other_evidence = capture_relation_edge_id(
        m1b_run_id="run-x",
        relation=LineageRelation.SHARED_SOURCE_REQUEST,
        endpoint_capture_ids=("cap-a", "cap-b"),
        evidence_key=["req-2"],
    )
    assert base != other_relation
    assert base != other_evidence


def test_request_successor_edge_id_is_direction_sensitive() -> None:
    """有向后继边父子互换即不同边（不排序端点）。"""

    forward = request_successor_edge_id(
        m1b_run_id="run-x",
        parent_request_node_id="node-parent",
        child_request_node_id="node-child",
    )
    backward = request_successor_edge_id(
        m1b_run_id="run-x",
        parent_request_node_id="node-child",
        child_request_node_id="node-parent",
    )
    assert forward != backward


def test_lineage_run_id_binds_upstream_identity() -> None:
    """lineage run_id 内容寻址绑定 (m1b_run_id, manifest sha)。"""

    base = lineage_run_id(m1b_run_id="run-x", m1b_artifact_manifest_sha256="a" * 64)
    assert base == lineage_run_id(m1b_run_id="run-x", m1b_artifact_manifest_sha256="a" * 64)
    assert base != lineage_run_id(m1b_run_id="run-y", m1b_artifact_manifest_sha256="a" * 64)
    assert base != lineage_run_id(m1b_run_id="run-x", m1b_artifact_manifest_sha256="b" * 64)


def test_build_report_counts_key_set_is_closed_with_zero_fill() -> None:
    """报告计数键集恒等于白名单；缺省边关系补零。"""

    counts = build_report_counts(
        capture_count=3,
        request_node_count=2,
        candidate_group_count=1,
        raw_request_hash_qualified_count=3,
        raw_request_hash_unknown_count=0,
        edge_counts_by_relation={LineageRelation.SHARED_SOURCE_REQUEST: 1},
    )
    assert set(counts) == set(LINEAGE_COUNT_KEYS)
    assert counts[edge_count_key(LineageRelation.SHARED_SOURCE_REQUEST)] == 1
    assert counts[edge_count_key(LineageRelation.COMPLETE_DUPLICATE_CAPTURE)] == 0
    # 键序稳定（sorted），保证报告逐字节一致。
    assert list(counts) == sorted(counts)
