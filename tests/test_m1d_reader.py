"""M1D reader IO 层测试（规格 §2.1「先校验后消费」+ §4 输入投影）。

在 `compile_dataset` 造的**新建**测试 M1B run 上验证：scope 分流守恒、content 冻结标量抽取、
capture_quality join、batches/pairings 透传、内容寻址身份绑定，以及 reader→builder 直连。
失败面：非目录 / 非法 run / 透传标量错类型（篡改+重签新建 run）一律 fail-closed 为
`QueryTurnInputError`（绝不裸 TypeError）。冻结 R01 run 绝不触碰。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from traceforge.query_turns.builder import build_query_turn_graph
from traceforge.query_turns.reader import QueryTurnInputError, load_m1b_turn_view
from traceforge.trajectory.contracts import EventKind
from traceforge.trajectory.json_codec import canonical_json_line

_CAPTURES = "private/captures.jsonl"
_EVENTS = "private/event_occurrences.jsonl"
_TERMINAL_STATUSES = {"TEXT_OUTCOME", "TOOL_CALL_PENDING", "EMPTY_OUTCOME", "INVALID"}


def _capture(
    capture_factory: Callable[..., dict[str, Any]], request_ids: list[str], **kwargs: Any
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for index in range(len(request_ids)):
        messages.append({"role": "user", "content": f"用户回合 {index}"})
        messages.append({"role": "assistant", "content": f"助手回合 {index}"})
    depths = [2 * (index + 1) for index in range(len(request_ids))]
    return capture_factory(
        messages=messages, terminal_prefix_depths=depths, request_ids=request_ids, **kwargs
    )


def _rewrite(path: Path, transform: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> None:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    path.write_bytes(b"".join(canonical_json_line(record) for record in transform(records)))


def _resign_m1b(m1b_run: Path, relative: str) -> None:
    """重签新建测试 M1B run 的 manifest+receipt。

    内容寻址 run_id 只绑输入、不绑输出字节，故对输出私有表定向篡改后重签物理摘要仍过完整性校验。
    """

    raw = (m1b_run / relative).read_bytes()
    manifest_path = m1b_run / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    [entry] = [item for item in manifest["files"] if item["relative_path"] == relative]
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    entry["byte_length"] = len(raw)
    if relative.endswith(".jsonl"):
        entry["record_count"] = raw.count(b"\n")
    manifest_path.write_bytes(canonical_json_line(manifest))
    receipt_path = m1b_run / "run_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["artifact_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    receipt_path.write_bytes(canonical_json_line(receipt))


@pytest.fixture
def m1b_run(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    two_boundary_capture: dict[str, Any],
) -> Path:
    """含多回合文本 capture、含 compaction capture 与含工具事件 capture 的合法 M1B run。"""

    return compile_dataset(
        [
            _capture(capture_factory, ["r1", "r2"]),
            _capture(capture_factory, ["r3"], has_compaction=True),
            two_boundary_capture,
        ],
        label="m1d-reader",
    )


def test_identity_binding_and_capture_set(m1b_run: Path) -> None:
    view = load_m1b_turn_view(m1b_run)
    assert view.m1b_run_id == m1b_run.name
    recomputed = hashlib.sha256((m1b_run / "artifact_manifest.json").read_bytes()).hexdigest()
    assert view.m1b_artifact_manifest_sha256 == recomputed
    published = {row["capture_occurrence_id"] for row in _read(m1b_run, _CAPTURES)}
    assert {c.capture_occurrence_id for c in view.captures} == published
    # 按 capture_occurrence_id 升序（确定性）。
    ids = [c.capture_occurrence_id for c in view.captures]
    assert ids == sorted(ids)


def test_scope_split_is_total_and_disjoint(m1b_run: Path) -> None:
    view = load_m1b_turn_view(m1b_run)
    events_per_capture: dict[str, int] = {}
    for row in _read(m1b_run, _EVENTS):
        cid = row["capture_occurrence_id"]
        events_per_capture[cid] = events_per_capture.get(cid, 0) + 1
    for cap in view.captures:
        prefix = sum(cap.prefix_event_counts_by_kind.values())
        observed = len(cap.observed_events)
        # 观测窗口 + 前缀 = 该 capture 全部事件（分流既无重叠也无遗漏）。
        assert prefix + observed == events_per_capture[cap.capture_occurrence_id]


def test_content_scalars_extracted_from_frozen_reader(m1b_run: Path) -> None:
    view = load_m1b_turn_view(m1b_run)
    for cap in view.captures:
        for event in cap.observed_events:
            if event.event_kind in (EventKind.USER, EventKind.ASSISTANT_MESSAGE):
                # TEXT→仅 utf8_byte_length；CONTENT_BLOCKS→仅 block_count（恰一者非 None）。
                assert (event.text_utf8_byte_length is None) != (event.block_count is None)
                assert event.has_non_text_blocks == (event.block_count is not None)
            else:
                assert event.text_utf8_byte_length is None
                assert event.block_count is None
                assert event.has_non_text_blocks is False


def test_quality_join_and_compaction(m1b_run: Path) -> None:
    view = load_m1b_turn_view(m1b_run)
    quality = {
        row["capture_occurrence_id"]: row
        for row in _read(m1b_run, "private/capture_quality.jsonl")
        if row["capture_occurrence_id"] is not None
    }
    for cap in view.captures:
        assert cap.terminal_status in _TERMINAL_STATUSES
        assert cap.terminal_status == quality[cap.capture_occurrence_id]["terminal_status"]
    # 含 compaction 的 capture 如实透传 has_compaction。
    assert any(cap.has_compaction for cap in view.captures)


def test_action_batches_and_pairings_passthrough(m1b_run: Path) -> None:
    view = load_m1b_turn_view(m1b_run)
    # two_boundary_capture 含 call-matched(1:1)、call-missing(有调用无结果)、call-terminal。
    with_batches = [cap for cap in view.captures if cap.action_batch_by_assistant]
    assert with_batches
    cap = with_batches[0]
    batch = next(iter(cap.action_batch_by_assistant.values()))
    assert batch.tool_call_event_ids  # 非空
    assert isinstance(batch.execution_semantics, str)
    matched = [
        p for p in cap.tool_pairings if p.matched_call_event_id and p.matched_result_event_id
    ]
    unmatched = [p for p in cap.tool_pairings if p.matched_result_event_id is None]
    assert matched  # call-matched：严格 1:1
    assert unmatched  # call-missing：有调用无结果


def test_reader_feeds_builder_end_to_end(m1b_run: Path) -> None:
    view = load_m1b_turn_view(m1b_run)
    graph = build_query_turn_graph(m1b_run_id=view.m1b_run_id, captures=view.captures)
    assert graph.capture_count == len(view.captures)
    # 每个 capture 至少产出一个 AgentStep 或一个 UserBlock（观测窗口非空）。
    assert graph.query_turn_count >= 1


def test_missing_directory_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(QueryTurnInputError):
        load_m1b_turn_view(tmp_path / "does-not-exist")


def test_non_m1b_directory_fails_closed(tmp_path: Path) -> None:
    empty = tmp_path / "empty-run"
    empty.mkdir()
    with pytest.raises(QueryTurnInputError):
        load_m1b_turn_view(empty)


def test_malformed_passthrough_scalar_fails_closed(m1b_run: Path) -> None:
    """把透传标量 has_compaction 篡改成非 bool、重签——须 fail-closed 为 QueryTurnInputError。

    M1B validator 只校验字段集合与 canonical、不逐值校验透传标量类型，故重签后仍过完整性校验，
    命中的是 reader 自身的类型守卫（把下游裸 TypeError 前移为显式错误）。
    """

    def corrupt(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records[0]["has_compaction"] = "yes"  # 应为 bool
        return records

    _rewrite(m1b_run / _CAPTURES, corrupt)
    _resign_m1b(m1b_run, _CAPTURES)
    with pytest.raises(QueryTurnInputError):
        load_m1b_turn_view(m1b_run)


def _read(run: Path, relative: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run / relative).read_text(encoding="utf-8").splitlines()]
