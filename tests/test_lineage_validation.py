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
    LineageRelation,
    capture_relation_edge_id,
)
from traceforge.lineage.validation import validate_lineage_run
from traceforge.trajectory.json_codec import canonical_json_line

_EDGES = "private/capture_relation_edges.jsonl"
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
    """建一个含 SHARED + IDENTICAL + successor 边的合法 lineage run，返回 (lineage, m1b)。"""

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
    assert counts["identical_raw_request_hash_edge_count"] == 1
    assert counts["explicit_request_successor_edge_count"] == 2
    assert counts["complete_duplicate_capture_edge_count"] == 0
    assert counts["raw_request_hash_qualified_count"] == 2
    assert counts["raw_request_hash_unknown_count"] == 0


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


def test_unknown_hash_in_identical_edge_is_caught(valid_run: tuple[Path, Path]) -> None:
    """把 IDENTICAL 边证据改成非 QUALIFIED 哈希并重签 → 门④拦截。"""

    lineage_run, m1b_run = valid_run

    def tamper(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        index = _index_of(records, LineageRelation.IDENTICAL_RAW_REQUEST_HASH)
        records[index]["evidence"] = {"raw_request_hash": "B" * 64}  # 大写 → UNKNOWN
        return records

    _rewrite(lineage_run / _EDGES, tamper)
    _resign(lineage_run, _EDGES)

    assert "LINEAGE_IDENTICAL_UNKNOWN_ENDPOINT" in _codes(lineage_run, m1b_run)


def test_unsigned_sha_tamper_is_caught(valid_run: tuple[Path, Path]) -> None:
    """篡改私有表但不重签 → 逐文件重哈希抓 ARTIFACT_SHA256_MISMATCH。"""

    lineage_run, m1b_run = valid_run
    path = lineage_run / _EDGES
    path.write_bytes(path.read_bytes() + b'{"unsigned":1}\n')  # 不调用 _resign

    assert "ARTIFACT_SHA256_MISMATCH" in _codes(lineage_run, m1b_run)
