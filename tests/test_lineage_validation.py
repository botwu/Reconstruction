"""M1C 独立 validator：接受合法 run；对重签后的语义损坏 fail-closed（删边洗白为首要威胁）。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from traceforge.lineage import build_lineage
from traceforge.lineage.contracts import (
    CAPTURE_RELATION_EDGE_SCHEMA,
    REQUEST_NODE_SCHEMA,
    REQUEST_SUCCESSOR_EDGE_SCHEMA,
    LineageRelation,
    capture_relation_edge_id,
    request_node_id,
    request_successor_edge_id,
)
from traceforge.lineage.reader import LineageInputError, load_m1b_run_view
from traceforge.lineage.validation import validate_lineage_run
from traceforge.trajectory.json_codec import canonical_json_line
from traceforge.trajectory.validation import validate_compiled_run

_EDGES = "private/capture_relation_edges.jsonl"
_NODES = "private/request_nodes.jsonl"
_SUCCESSORS = "private/request_successor_edges.jsonl"
_REPORT = "reports/lineage_report.json"
_CAPTURES = "private/captures.jsonl"
_DATA_URL = "data:image/png;base64,U0VDUkVUX0JBU0U2NA=="


def _capture(
    capture_factory: Callable[..., dict[str, Any]], request_ids: list[str]
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for index in range(len(request_ids)):
        messages.append({"role": "user", "content": f"用户回合 {index}"})
        messages.append({"role": "assistant", "content": f"助手回合 {index}"})
    depths = [2 * (index + 1) for index in range(len(request_ids))]
    return capture_factory(
        messages=messages, terminal_prefix_depths=depths, request_ids=request_ids
    )


@pytest.fixture
def valid_run(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> tuple[Path, Path]:
    """建一个含 SHARED + successor 边的合法 lineage run，返回 (lineage, m1b)。"""

    m1b_run = compile_dataset(
        [
            _capture(capture_factory, ["r1", "r2"]),
            _capture(capture_factory, ["r2", "r3"]),
        ],
        label="validation",
    )
    lineage_run = build_lineage(m1b_run_dir=m1b_run, output_root=tmp_path / "lineage_out")
    return lineage_run, m1b_run


def _codes(lineage_run: Path, m1b_run: Path) -> set[str]:
    return {issue.code for issue in validate_lineage_run(lineage_run, m1b_run).issues}


def _rewrite(path: Path, transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> None:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records = transform(records)
    path.write_bytes(b"".join(canonical_json_line(record) for record in records))


def _resign(lineage_run: Path, relative: str) -> None:
    """重签物理 manifest 与 receipt，使语义损坏能越过纯摘要层（对抗「重签洗白」）。"""

    raw = (lineage_run / relative).read_bytes()
    manifest_path = lineage_run / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    [entry] = [item for item in manifest["files"] if item["relative_path"] == relative]
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    entry["byte_length"] = len(raw)
    if relative.endswith(".jsonl"):
        entry["record_count"] = raw.count(b"\n")
    manifest_path.write_bytes(canonical_json_line(manifest))

    receipt_path = lineage_run / "run_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["artifact_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    receipt_path.write_bytes(canonical_json_line(receipt))


def _index_of(records: list[dict[str, Any]], relation: LineageRelation) -> int:
    return next(i for i, record in enumerate(records) if record["relation"] == relation.value)


def _resign_m1b(m1b_run: Path, relative: str) -> None:
    """重签**新建测试** M1B run 的 manifest+receipt（结构与 lineage 侧同构，故直接复用 `_resign`）。

    内容寻址 M1B run_id 只绑输入、不绑输出字节，故对输出私有表的定向篡改重签物理摘要后仍能过
    `validate_compiled_run`——这正是门③负例与 reader 类型守卫得以 e2e 验证的前提。冻结 R01 绝不触碰。
    """

    _resign(m1b_run, relative)


def _m1b_captures(m1b_run: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in (m1b_run / _CAPTURES).read_text(encoding="utf-8").splitlines()
    ]


def _lineage_records(lineage_run: Path, relative: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (lineage_run / relative).read_text(encoding="utf-8").splitlines()
    ]


def _m1b_run_id(lineage_run: Path) -> str:
    manifest = json.loads((lineage_run / "lineage_manifest.json").read_text(encoding="utf-8"))
    return manifest["m1b_run_id"]


def _group_capture(
    capture_factory: Callable[..., dict[str, Any]],
    request_ids: list[str],
    *,
    thread_id: str,
    account_id: str,
) -> dict[str, Any]:
    """一个落在指定 (thread_id, account_id) 候选组的多回合 capture。

    正文带上 thread_id，使不同组的 capture 可见指纹链不同，测试只观察目标关系。
    """

    messages: list[dict[str, Any]] = []
    for index in range(len(request_ids)):
        messages.append({"role": "user", "content": f"{thread_id} 用户回合 {index}"})
        messages.append({"role": "assistant", "content": f"{thread_id} 助手回合 {index}"})
    depths = [2 * (index + 1) for index in range(len(request_ids))]
    return capture_factory(
        messages=messages,
        terminal_prefix_depths=depths,
        request_ids=request_ids,
        thread_id=thread_id,
        account_id=account_id,
    )


@pytest.fixture
def cross_group_run(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> tuple[Path, Path]:
    """两个分属不同候选组、却共享同一 source_request_id 的 capture：SHARED 边必须跨组建立。

    这是门②的唯一实证形态：关系由已发布的可见事实（srid）重算得出，而 (thread_id, account_id)
    只是上游元数据；若以候选组裁剪 Grade-A，这条真实关系会被漏掉。
    """

    m1b_run = compile_dataset(
        [
            _group_capture(capture_factory, ["cgx"], thread_id="thread-a", account_id="acct-a"),
            _group_capture(capture_factory, ["cgx"], thread_id="thread-b", account_id="acct-b"),
        ],
        label="crossgroup",
    )
    lineage_run = build_lineage(m1b_run_dir=m1b_run, output_root=tmp_path / "lineage_cross")
    return lineage_run, m1b_run


@pytest.fixture
def complete_duplicate_run(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> tuple[Path, Path]:
    """两条逐字节相同的 capture 记录（仅 line_number 不同）→ 占据不同 occurrence 但指纹链与 srid
    序列全等 → 应建 COMPLETE_DUPLICATE 边（capture_id 受「须等于末个 srid」约束，不能自由改名）。"""

    m1b_run = compile_dataset(
        [
            _capture(capture_factory, ["dup1", "dup2"]),
            _capture(capture_factory, ["dup1", "dup2"]),
        ],
        label="completedup",
    )
    lineage_run = build_lineage(m1b_run_dir=m1b_run, output_root=tmp_path / "lineage_dup")
    return lineage_run, m1b_run


# --- 正常路径 --------------------------------------------------------------


def test_validator_accepts_freshly_built_run(valid_run: tuple[Path, Path]) -> None:
    """新建 lineage run 通过全部核验：ok、有 checked_file_count、observed_counts 守恒。"""

    lineage_run, m1b_run = valid_run
    result = validate_lineage_run(lineage_run, m1b_run)

    assert result.ok, result.errors
    assert result.checked_file_count >= 1
    counts = result.observed_counts
    assert counts["capture_count"] == 2
    assert counts["request_node_count"] == 3
    assert counts["candidate_group_count"] == 1
    assert counts["shared_source_request_edge_count"] == 1
    assert counts["explicit_request_successor_edge_count"] == 2
    assert counts["complete_duplicate_capture_edge_count"] == 0


# --- 对抗 fail-closed -------------------------------------------------------


def test_deleting_edge_and_resigning_is_caught(valid_run: tuple[Path, Path]) -> None:
    """删一条 Grade-A 边并重签 → 双向 bijection 抓「删边洗白」（§8 首要威胁）。"""

    lineage_run, m1b_run = valid_run
    _rewrite(lineage_run / _EDGES, lambda records: records[1:])
    _resign(lineage_run, _EDGES)

    assert "LINEAGE_EDGE_MISSING" in _codes(lineage_run, m1b_run)


def test_phantom_edge_and_resigning_is_caught(valid_run: tuple[Path, Path]) -> None:
    """注入独立复算不产出的幻影边并重签 → bijection 抓幻影。"""

    lineage_run, m1b_run = valid_run
    m1b_run_id = json.loads((lineage_run / "lineage_manifest.json").read_text(encoding="utf-8"))[
        "m1b_run_id"
    ]

    def inject(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        endpoints = sorted(
            records[_index_of(records, LineageRelation.SHARED_SOURCE_REQUEST)][
                "endpoint_capture_ids"
            ]
        )
        evidence_key = ["phantom-shared-request"]
        phantom = {
            "schema_version": CAPTURE_RELATION_EDGE_SCHEMA,
            "edge_id": capture_relation_edge_id(
                m1b_run_id=m1b_run_id,
                relation=LineageRelation.SHARED_SOURCE_REQUEST,
                endpoint_capture_ids=endpoints,
                evidence_key=evidence_key,
            ),
            "relation": LineageRelation.SHARED_SOURCE_REQUEST.value,
            "endpoint_capture_ids": endpoints,
            "evidence": {"shared_source_request_ids": evidence_key},
        }
        return [*records, phantom]

    _rewrite(lineage_run / _EDGES, inject)
    _resign(lineage_run, _EDGES)

    assert "LINEAGE_EDGE_PHANTOM" in _codes(lineage_run, m1b_run)


def test_tampered_edge_id_and_resigning_is_caught(valid_run: tuple[Path, Path]) -> None:
    """篡改 edge_id 并重签 → 稳定 ID 独立重算不符。"""

    lineage_run, m1b_run = valid_run

    def tamper(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records[0]["edge_id"] = "f" * 64
        return records

    _rewrite(lineage_run / _EDGES, tamper)
    _resign(lineage_run, _EDGES)

    assert "STABLE_ID_MISMATCH" in _codes(lineage_run, m1b_run)


def test_candidate_group_in_evidence_is_caught(valid_run: tuple[Path, Path]) -> None:
    """把候选组 ID 塞进 evidence 并重签 → 门① 结构性拦截（Grade-A 全局组盲）。"""

    lineage_run, m1b_run = valid_run
    captures = [
        json.loads(line)
        for line in (m1b_run / "private" / "captures.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    candidate_group_id = captures[0]["candidate_group_id"]

    def inject(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        index = _index_of(records, LineageRelation.SHARED_SOURCE_REQUEST)
        records[index]["evidence"] = {"shared_source_request_ids": [candidate_group_id]}
        return records

    _rewrite(lineage_run / _EDGES, inject)
    _resign(lineage_run, _EDGES)

    assert "LINEAGE_EVIDENCE_CANDIDATE_GROUP" in _codes(lineage_run, m1b_run)


def test_data_url_in_evidence_is_caught(valid_run: tuple[Path, Path]) -> None:
    """evidence 注入 Data URL 并重签 → 隐私兜底抓 BASE64_DATA_URL_OBSERVED。"""

    lineage_run, m1b_run = valid_run

    def inject(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        index = _index_of(records, LineageRelation.SHARED_SOURCE_REQUEST)
        records[index]["evidence"] = {"shared_source_request_ids": [_DATA_URL]}
        return records

    _rewrite(lineage_run / _EDGES, inject)
    _resign(lineage_run, _EDGES)

    assert "BASE64_DATA_URL_OBSERVED" in _codes(lineage_run, m1b_run)


def test_unsigned_sha_tamper_is_caught(valid_run: tuple[Path, Path]) -> None:
    """篡改私有表但不重签 → 逐文件重哈希抓 ARTIFACT_SHA256_MISMATCH。"""

    lineage_run, m1b_run = valid_run
    path = lineage_run / _EDGES
    path.write_bytes(path.read_bytes() + b'{"unsigned":1}\n')  # 不调用 _resign

    assert "ARTIFACT_SHA256_MISMATCH" in _codes(lineage_run, m1b_run)


# --- 门②/门③：跨候选组 Grade-A 与候选组分区（组盲设计的实证）--------------------


def test_shared_edge_spans_candidate_groups(cross_group_run: tuple[Path, Path]) -> None:
    """headline：两个不同候选组、同一 srid 的 capture → 1 条跨组 SHARED 边（门②）。"""

    lineage_run, m1b_run = cross_group_run
    result = validate_lineage_run(lineage_run, m1b_run)

    assert result.ok, result.errors
    counts = result.observed_counts
    assert counts["candidate_group_count"] == 2
    assert counts["shared_source_request_edge_count"] == 1
    assert counts["explicit_request_successor_edge_count"] == 0
    assert counts["complete_duplicate_capture_edge_count"] == 0
    # 门③正例：多组 run 通过分区核验，不误报 1:1 双射不符。
    assert "LINEAGE_CANDIDATE_GROUP_PARTITION_MISMATCH" not in _codes(lineage_run, m1b_run)

    group_by_capture = {
        record["capture_occurrence_id"]: record["candidate_group_id"]
        for record in _m1b_captures(m1b_run)
    }
    edges = _lineage_records(lineage_run, _EDGES)
    shared = edges[_index_of(edges, LineageRelation.SHARED_SOURCE_REQUEST)]
    left, right = shared["endpoint_capture_ids"]
    assert group_by_capture[left] != group_by_capture[right]


def test_gate3_partition_mismatch_is_caught(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """门③负例：两个不同 (thread,account) 的 capture 被篡改为共用同一候选组 → 分区非 1:1。"""

    m1b_run = compile_dataset(
        [
            _group_capture(capture_factory, ["gm1"], thread_id="thread-x", account_id="acct-x"),
            _group_capture(capture_factory, ["gm2"], thread_id="thread-y", account_id="acct-y"),
        ],
        label="gate3neg",
    )
    forced_group = _m1b_captures(m1b_run)[0]["candidate_group_id"]

    def force_same_group(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for record in records:
            record["candidate_group_id"] = forced_group
        return records

    _rewrite(m1b_run / _CAPTURES, force_same_group)
    _resign_m1b(m1b_run, _CAPTURES)
    lineage_run = build_lineage(m1b_run_dir=m1b_run, output_root=tmp_path / "lineage_gate3")

    assert "LINEAGE_CANDIDATE_GROUP_PARTITION_MISMATCH" in _codes(lineage_run, m1b_run)


# --- COMPLETE_DUPLICATE_CAPTURE 端到端 --------------------------------------


def test_complete_duplicate_capture_edge_end_to_end(
    complete_duplicate_run: tuple[Path, Path],
) -> None:
    """指纹链与 srid 序列双双相等的两 capture → COMPLETE_DUP 首次端到端产出并过 validator 重算。"""

    lineage_run, m1b_run = complete_duplicate_run
    result = validate_lineage_run(lineage_run, m1b_run)

    assert result.ok, result.errors
    counts = result.observed_counts
    assert counts["capture_count"] == 2
    assert counts["complete_duplicate_capture_edge_count"] >= 1
    assert counts["shared_source_request_edge_count"] >= 1


# --- 图完整性守卫（node / successor 的 bijection 家族，此前零断言）-------------


def test_missing_node_is_caught(valid_run: tuple[Path, Path]) -> None:
    """删一个 RequestNode 并重签 → node bijection 抓 LINEAGE_NODE_MISSING。"""

    lineage_run, m1b_run = valid_run
    _rewrite(lineage_run / _NODES, lambda records: records[1:])
    _resign(lineage_run, _NODES)

    assert "LINEAGE_NODE_MISSING" in _codes(lineage_run, m1b_run)


def test_phantom_node_is_caught(valid_run: tuple[Path, Path]) -> None:
    """注入独立复算不产出的幻影 RequestNode 并重签 → LINEAGE_NODE_PHANTOM。"""

    lineage_run, m1b_run = valid_run
    m1b_run_id = _m1b_run_id(lineage_run)
    real_capture = _m1b_captures(m1b_run)[0]["capture_occurrence_id"]

    def inject(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        source_request_id = "phantom-node-srid"
        phantom = {
            "schema_version": REQUEST_NODE_SCHEMA,
            "request_node_id": request_node_id(
                m1b_run_id=m1b_run_id, source_request_id=source_request_id
            ),
            "source_request_id": source_request_id,
            "boundary_occurrences": [
                {"boundary_ordinal": 0, "capture_occurrence_id": real_capture}
            ],
        }
        return [*records, phantom]

    _rewrite(lineage_run / _NODES, inject)
    _resign(lineage_run, _NODES)

    assert "LINEAGE_NODE_PHANTOM" in _codes(lineage_run, m1b_run)


def test_tampered_node_boundary_is_caught(valid_run: tuple[Path, Path]) -> None:
    """丢一条 boundary_occurrence 但保留 request_node_id 并重签 → LINEAGE_NODE_MISMATCH。"""

    lineage_run, m1b_run = valid_run

    def drop_one_occurrence(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        target = next(record for record in records if len(record["boundary_occurrences"]) > 1)
        target["boundary_occurrences"] = target["boundary_occurrences"][:1]
        return records

    _rewrite(lineage_run / _NODES, drop_one_occurrence)
    _resign(lineage_run, _NODES)

    assert "LINEAGE_NODE_MISMATCH" in _codes(lineage_run, m1b_run)


def test_node_endpoint_not_in_m1b_is_caught(valid_run: tuple[Path, Path]) -> None:
    """boundary_occurrences 含未知 capture_occurrence_id 并重签 → LINEAGE_ENDPOINT_NOT_IN_M1B。"""

    lineage_run, m1b_run = valid_run

    def inject(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records[0]["boundary_occurrences"] = [
            {"boundary_ordinal": 0, "capture_occurrence_id": "unknown-capture-id"},
        ]
        return records

    _rewrite(lineage_run / _NODES, inject)
    _resign(lineage_run, _NODES)

    assert "LINEAGE_ENDPOINT_NOT_IN_M1B" in _codes(lineage_run, m1b_run)


def test_missing_successor_edge_is_caught(valid_run: tuple[Path, Path]) -> None:
    """删一条后继边并重签 → successor bijection 抓 LINEAGE_EDGE_MISSING。"""

    lineage_run, m1b_run = valid_run
    _rewrite(lineage_run / _SUCCESSORS, lambda records: records[1:])
    _resign(lineage_run, _SUCCESSORS)

    assert "LINEAGE_EDGE_MISSING" in _codes(lineage_run, m1b_run)


def test_phantom_successor_edge_is_caught(valid_run: tuple[Path, Path]) -> None:
    """注入一条独立复算不产出（r1→r3 非相邻）的后继边并重签 → LINEAGE_EDGE_PHANTOM。"""

    lineage_run, m1b_run = valid_run
    m1b_run_id = _m1b_run_id(lineage_run)
    real_capture = _m1b_captures(m1b_run)[0]["capture_occurrence_id"]
    parent = request_node_id(m1b_run_id=m1b_run_id, source_request_id="r1")
    child = request_node_id(m1b_run_id=m1b_run_id, source_request_id="r3")

    def inject(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        phantom = {
            "schema_version": REQUEST_SUCCESSOR_EDGE_SCHEMA,
            "edge_id": request_successor_edge_id(
                m1b_run_id=m1b_run_id,
                parent_request_node_id=parent,
                child_request_node_id=child,
            ),
            "relation": LineageRelation.EXPLICIT_REQUEST_SUCCESSOR.value,
            "parent_request_node_id": parent,
            "child_request_node_id": child,
            "evidence": [
                {
                    "capture_occurrence_id": real_capture,
                    "parent_boundary_ordinal": 0,
                    "child_boundary_ordinal": 1,
                }
            ],
        }
        return [*records, phantom]

    _rewrite(lineage_run / _SUCCESSORS, inject)
    _resign(lineage_run, _SUCCESSORS)

    assert "LINEAGE_EDGE_PHANTOM" in _codes(lineage_run, m1b_run)


# --- 关系级别 / evidence 禁用标识 / 报告 / 绑定（此前零断言）-------------------


def test_relation_level_mismatch_is_caught(valid_run: tuple[Path, Path]) -> None:
    """把 capture 级边的 relation 改成请求级值并重签 → LINEAGE_RELATION_LEVEL_MISMATCH。"""

    lineage_run, m1b_run = valid_run

    def tamper(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records[0]["relation"] = LineageRelation.EXPLICIT_REQUEST_SUCCESSOR.value
        return records

    _rewrite(lineage_run / _EDGES, tamper)
    _resign(lineage_run, _EDGES)

    assert "LINEAGE_RELATION_LEVEL_MISMATCH" in _codes(lineage_run, m1b_run)


def test_target_hash_in_evidence_is_caught(valid_run: tuple[Path, Path]) -> None:
    """target_hash 混入 SHARED 边 evidence 并重签 → LINEAGE_EVIDENCE_TARGET_HASH（§5.5）。"""

    lineage_run, m1b_run = valid_run
    target_hash = _m1b_captures(m1b_run)[0]["target_hash"]

    def inject(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        index = _index_of(records, LineageRelation.SHARED_SOURCE_REQUEST)
        records[index]["evidence"] = {"shared_source_request_ids": [target_hash]}
        return records

    _rewrite(lineage_run / _EDGES, inject)
    _resign(lineage_run, _EDGES)

    assert "LINEAGE_EVIDENCE_TARGET_HASH" in _codes(lineage_run, m1b_run)


def test_report_count_mismatch_is_caught(valid_run: tuple[Path, Path]) -> None:
    """篡改公共报告某聚合计数并重签 → LINEAGE_REPORT_COUNT_MISMATCH。"""

    lineage_run, m1b_run = valid_run
    report_path = lineage_run / _REPORT
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["counts"]["capture_count"] = report["counts"]["capture_count"] + 100
    report_path.write_bytes(canonical_json_line(report))
    _resign(lineage_run, _REPORT)

    assert "LINEAGE_REPORT_COUNT_MISMATCH" in _codes(lineage_run, m1b_run)


def test_report_allowlist_mismatch_is_caught(valid_run: tuple[Path, Path]) -> None:
    """给公共报告 counts 增设一个白名单外的键并重签 → LINEAGE_REPORT_ALLOWLIST_MISMATCH。"""

    lineage_run, m1b_run = valid_run
    report_path = lineage_run / _REPORT
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["counts"]["unexpected_extra_count"] = 0
    report_path.write_bytes(canonical_json_line(report))
    _resign(lineage_run, _REPORT)

    assert "LINEAGE_REPORT_ALLOWLIST_MISMATCH" in _codes(lineage_run, m1b_run)


def test_binding_wrong_m1b_oracle_is_caught(
    valid_run: tuple[Path, Path],
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    """用另一套 M1B run 作 oracle 校验（无篡改）→ 身份绑定不符（run_id + manifest sha）。"""

    lineage_run, _m1b_a = valid_run
    m1b_b = compile_dataset(
        [
            _capture(capture_factory, ["z1", "z2"]),
            _capture(capture_factory, ["z3", "z4"]),
        ],
        label="oracle-b",
    )
    codes = _codes(lineage_run, m1b_b)

    assert "M1B_RUN_ID_BINDING_MISMATCH" in codes
    assert "M1B_MANIFEST_SHA_BINDING_MISMATCH" in codes


# --- reader 透传字段类型守卫（把潜在裸 TypeError 前移为 fail-closed）-----------


def test_reader_rejects_non_str_candidate_group(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    """M1B validator 不逐值校验透传类型（放行 list 值），reader 守卫据此抛 LineageInputError。"""

    m1b_run = compile_dataset([_capture(capture_factory, ["r1", "r2"])], label="readerguard")

    def to_list(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for record in records:
            record["candidate_group_id"] = [record["candidate_group_id"]]
        return records

    _rewrite(m1b_run / _CAPTURES, to_list)
    _resign_m1b(m1b_run, _CAPTURES)

    # 上游权威 validator 仍放行（字段集合不变、不逐值校验透传标量类型）。
    assert validate_compiled_run(m1b_run).ok
    # 若无守卫，list 值会在 pipeline 里进 set(...) 触发裸 TypeError；守卫将其前移为 fail-closed。
    with pytest.raises(LineageInputError):
        load_m1b_run_view(m1b_run)
