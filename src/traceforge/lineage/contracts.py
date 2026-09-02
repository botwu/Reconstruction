"""M1C 跨 capture 关系图的稳定数据契约与稳定 ID 公式。

本模块是 M1C 契约的唯一权威来源（[`AGENTS.md`] §1 DRY）：关系枚举、产物 dataclass、
schema 常量、`relation → (grade, directionality)` 单一映射、`raw_request_hash` 格式契约
（规格 §7）与全部稳定 ID 公式（规格 §5.6）。builder 与独立 validator 都只从这里取用这些
定义，不各自复刻。

M1C 复用 `trajectory.json_codec` 的公开哈希/canonical 内核（`validation.py` 亦 import 它），
只在自有命名空间常量上独立取 ID，不 import M1B 私有派生公式（compiler.py 中的 run_id /
candidate_group_id 等，规格 §5.6 修正）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from traceforge.trajectory.contracts import ArtifactEntryV1, SerializableContract
from traceforge.trajectory.json_codec import stable_id

# --- 契约版本与 schema 常量 ------------------------------------------------

LINEAGE_CONTRACT_VERSION = "lineage-compiler-m1c-v1"

LINEAGE_MANIFEST_SCHEMA = "traceforge.lineage-manifest.v1"
LINEAGE_ARTIFACT_MANIFEST_SCHEMA = "traceforge.lineage-artifact-manifest.v1"
LINEAGE_RUN_RECEIPT_SCHEMA = "traceforge.lineage-run-receipt.v1"
LINEAGE_REPORT_SCHEMA = "traceforge.lineage-report.v1"
REQUEST_NODE_SCHEMA = "traceforge.lineage-request-node.v1"
REQUEST_SUCCESSOR_EDGE_SCHEMA = "traceforge.lineage-request-successor-edge.v1"
CAPTURE_RELATION_EDGE_SCHEMA = "traceforge.lineage-capture-relation-edge.v1"

# 稳定 ID 命名空间（规格 §5.6）。均为 M1C 自有命名空间，不复用 M1B 命名空间字符串。
REQUEST_NODE_ID_NAMESPACE = "lineage-request-node-v1"
EDGE_ID_NAMESPACE = "lineage-edge-v1"
LINEAGE_RUN_ID_NAMESPACE = "lineage-run-v1"

_LOWERCASE_HEX = frozenset("0123456789abcdef")


# --- 关系枚举（本次只实现 4 类 Grade-A；Grade-B 预留、本次不实现）-----------


class LineageRelation(StrEnum):
    """封闭关系枚举。本次仅实现 4 类 Grade-A 显式关系。

    规格 §5 的 Grade-B ``NORMALIZED_VISIBLE_PREFIX_OF`` 预留、本次不实现，因此**不出现在
    本枚举中**，代码里也不放任何占位值（[`AGENTS.md`] §1 YAGNI）。
    """

    SHARED_SOURCE_REQUEST = "SHARED_SOURCE_REQUEST"
    EXPLICIT_REQUEST_SUCCESSOR = "EXPLICIT_REQUEST_SUCCESSOR"
    IDENTICAL_RAW_REQUEST_HASH = "IDENTICAL_RAW_REQUEST_HASH"
    COMPLETE_DUPLICATE_CAPTURE = "COMPLETE_DUPLICATE_CAPTURE"


class RelationGrade(StrEnum):
    # 本迭代仅物化 Grade-A（4 类关系）。Grade-B（如 NORMALIZED_VISIBLE_PREFIX_OF）按授权门缓做，
    # 不预留枚举占位；将来实现时再新增成员并扩展 `_RELATION_PROPERTIES`。
    A = "A"


class RelationDirectionality(StrEnum):
    UNDIRECTED = "UNDIRECTED"
    DIRECTED = "DIRECTED"


class RawRequestHashStatus(StrEnum):
    """逐 capture 资格状态（规格 §5/§7），不是边关系。"""

    QUALIFIED = "QUALIFIED"
    UNKNOWN = "UNKNOWN"


# `relation → (grade, directionality)` 的单一权威映射（规格 §4.3）。grade 与
# directionality 是 relation 的固定函数，不物化进每条边；读取方由此派生，validator 据此断言。
_RELATION_PROPERTIES: dict[LineageRelation, tuple[RelationGrade, RelationDirectionality]] = {
    LineageRelation.SHARED_SOURCE_REQUEST: (RelationGrade.A, RelationDirectionality.UNDIRECTED),
    LineageRelation.EXPLICIT_REQUEST_SUCCESSOR: (RelationGrade.A, RelationDirectionality.DIRECTED),
    LineageRelation.IDENTICAL_RAW_REQUEST_HASH: (
        RelationGrade.A,
        RelationDirectionality.UNDIRECTED,
    ),
    LineageRelation.COMPLETE_DUPLICATE_CAPTURE: (
        RelationGrade.A,
        RelationDirectionality.UNDIRECTED,
    ),
}

# capture 级无向关系存入 capture_relation_edges；请求级有向关系存入 request_successor_edges。
CAPTURE_LEVEL_RELATIONS = frozenset(
    {
        LineageRelation.SHARED_SOURCE_REQUEST,
        LineageRelation.IDENTICAL_RAW_REQUEST_HASH,
        LineageRelation.COMPLETE_DUPLICATE_CAPTURE,
    }
)
REQUEST_LEVEL_RELATIONS = frozenset({LineageRelation.EXPLICIT_REQUEST_SUCCESSOR})

# 公共报告的固定聚合计数键（validator 断言闭合，无原始 ID）。
LINEAGE_COUNT_KEYS = frozenset(
    {
        "capture_count",
        "request_node_count",
        "candidate_group_count",
        "raw_request_hash_qualified_count",
        "raw_request_hash_unknown_count",
        "shared_source_request_edge_count",
        "explicit_request_successor_edge_count",
        "identical_raw_request_hash_edge_count",
        "complete_duplicate_capture_edge_count",
    }
)

_EDGE_COUNT_KEY_BY_RELATION = {
    LineageRelation.SHARED_SOURCE_REQUEST: "shared_source_request_edge_count",
    LineageRelation.EXPLICIT_REQUEST_SUCCESSOR: "explicit_request_successor_edge_count",
    LineageRelation.IDENTICAL_RAW_REQUEST_HASH: "identical_raw_request_hash_edge_count",
    LineageRelation.COMPLETE_DUPLICATE_CAPTURE: "complete_duplicate_capture_edge_count",
}


def relation_properties(
    relation: LineageRelation | str,
) -> tuple[RelationGrade, RelationDirectionality]:
    """派生某关系的 (grade, directionality)。未知关系抛 ``KeyError``。"""

    return _RELATION_PROPERTIES[LineageRelation(relation)]


def edge_count_key(relation: LineageRelation | str) -> str:
    return _EDGE_COUNT_KEY_BY_RELATION[LineageRelation(relation)]


def classify_raw_request_hash(value: Any) -> RawRequestHashStatus:
    """规格 §7 冻结格式契约：字符串、长度恰好 64、全小写十六进制才 ``QUALIFIED``。

    以格式而非任何 R01 具体值判定，不把样本 hash 写入代码。
    """

    if (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _LOWERCASE_HEX for character in value)
    ):
        return RawRequestHashStatus.QUALIFIED
    return RawRequestHashStatus.UNKNOWN


# --- 稳定 ID 公式（规格 §5.6，单一权威）------------------------------------


def request_node_id(*, m1b_run_id: str, source_request_id: str) -> str:
    return stable_id(
        REQUEST_NODE_ID_NAMESPACE,
        {"m1b_run_id": m1b_run_id, "source_request_id": source_request_id},
    )


def capture_relation_edge_id(
    *,
    m1b_run_id: str,
    relation: LineageRelation | str,
    endpoint_capture_ids: Sequence[str],
    evidence_key: Any,
) -> str:
    """capture 级无向边的稳定 ID：端点按排序写入，evidence_key 承载关系区分证据。"""

    return stable_id(
        EDGE_ID_NAMESPACE,
        {
            "m1b_run_id": m1b_run_id,
            "relation": LineageRelation(relation).value,
            "endpoints": sorted(endpoint_capture_ids),
            "evidence_key": evidence_key,
        },
    )


def request_successor_edge_id(
    *,
    m1b_run_id: str,
    parent_request_node_id: str,
    child_request_node_id: str,
) -> str:
    """请求级有向后继边的稳定 ID：身份 = (relation, 有向端点)，见证不进身份（跨 capture 去重）。"""

    return stable_id(
        EDGE_ID_NAMESPACE,
        {
            "m1b_run_id": m1b_run_id,
            "relation": LineageRelation.EXPLICIT_REQUEST_SUCCESSOR.value,
            "endpoints": [parent_request_node_id, child_request_node_id],
            "evidence_key": None,
        },
    )


def lineage_run_id(*, m1b_run_id: str, m1b_artifact_manifest_sha256: str) -> str:
    return stable_id(
        LINEAGE_RUN_ID_NAMESPACE,
        {
            "lineage_contract_version": LINEAGE_CONTRACT_VERSION,
            "m1b_run_id": m1b_run_id,
            "m1b_artifact_manifest_sha256": m1b_artifact_manifest_sha256,
        },
    )


# --- 产物 dataclass --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestNodeV1(SerializableContract):
    """一个 distinct ``source_request_id`` 及其在各 capture 中的 boundary 归属。

    ``boundary_occurrences`` 是唯一权威归属字段（保留全部，不去重丢弃，规格 §4.2）。
    ``owner_capture_ids`` 是它的 distinct 投影，为固定函数，遵循规格 §4.3 DRY 不再物化，
    读取方按需派生。
    """

    schema_version: str
    request_node_id: str
    source_request_id: str
    boundary_occurrences: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class RequestSuccessorEdgeV1(SerializableContract):
    """请求级 Grade-A 有向后继边（EXPLICIT_REQUEST_SUCCESSOR，规格 §5.2）。"""

    schema_version: str
    edge_id: str
    relation: str
    parent_request_node_id: str
    child_request_node_id: str
    evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class CaptureRelationEdgeV1(SerializableContract):
    """capture 级 Grade-A 无向关系边（SHARED / IDENTICAL / COMPLETE_DUP，规格 §5.1/§5.3/§5.4）。"""

    schema_version: str
    edge_id: str
    relation: str
    endpoint_capture_ids: tuple[str, ...]
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LineageManifestV1(SerializableContract):
    """输入身份绑定（内容寻址，非路径，规格 §10）。"""

    schema_version: str
    lineage_run_id: str
    lineage_contract_version: str
    m1b_run_id: str
    m1b_artifact_manifest_sha256: str
    source_schema: str


@dataclass(frozen=True, slots=True)
class LineageArtifactManifestV1(SerializableContract):
    """确定性业务文件清单；不含自身与 run_receipt.json。"""

    schema_version: str
    lineage_run_id: str
    lineage_contract_version: str
    m1b_run_id: str
    m1b_artifact_manifest_sha256: str
    files: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class LineageReportV1(SerializableContract):
    """公共报告：仅固定聚合计数，无原始 ID/正文/路径/URL。"""

    schema_version: str
    lineage_run_id: str
    m1b_run_id: str
    counts: dict[str, int]


def build_report_counts(
    *,
    capture_count: int,
    request_node_count: int,
    candidate_group_count: int,
    raw_request_hash_qualified_count: int,
    raw_request_hash_unknown_count: int,
    edge_counts_by_relation: Mapping[LineageRelation, int],
) -> dict[str, int]:
    """按固定 allowlist 组装报告计数，缺省边计数补零，键集恒等于 ``LINEAGE_COUNT_KEYS``。"""

    counts = {
        "capture_count": capture_count,
        "request_node_count": request_node_count,
        "candidate_group_count": candidate_group_count,
        "raw_request_hash_qualified_count": raw_request_hash_qualified_count,
        "raw_request_hash_unknown_count": raw_request_hash_unknown_count,
    }
    for relation in LineageRelation:
        counts[edge_count_key(relation)] = 0
    for relation, value in edge_counts_by_relation.items():
        counts[edge_count_key(relation)] = value
    return dict(sorted(counts.items()))


def artifact_entry_dicts(entries: Iterable[ArtifactEntryV1]) -> tuple[dict[str, Any], ...]:
    """把 writer 返回的 ArtifactEntryV1 按 relative_path 排序后转为 manifest files 项。"""

    return tuple(entry.to_dict() for entry in sorted(entries, key=lambda item: item.relative_path))
