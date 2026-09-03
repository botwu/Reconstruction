"""`UserTextProjection` 发布编排：全类别/全形态覆盖、报告向量、确定性、M1D 回指、内容安全。

夹具四个 capture 一起覆盖 7 个类别 × 2 个 locality 中会出现的组合与 4 种 content_form
（规格 §6 列表④⑤⑥）。所有正文均为虚构；断言产物**不含**任何正文与未知标签名。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import USER_TEXT_UNKNOWN_TAG, user_text_dialogue

from traceforge.query_turns import build_query_turns
from traceforge.source_projection import build_user_text_projection
from traceforge.source_projection.contracts import (
    ANNOTATIONS_RELATIVE_PATH,
    LEADING_TAG_WHITELIST,
    PROJECTION_COUNT_KEYS,
    PROJECTION_MANIFEST_RELATIVE_PATH,
    PROJECTION_REPORT_RELATIVE_PATH,
    PROJECTION_RUN_RECEIPT_SCHEMA,
    USER_TEXT_PROJECTION_CONTRACT_VERSION,
    UserTextProjectionInputError,
    projection_run_id,
)
from traceforge.trajectory.artifacts import ArtifactPublishError

# 每条 USER 事件的期望 (locality, content_form, text_class, leading_tag)，按消息顺序。
_MIXED_EXPECTED = (
    ("PREFIX_UNLOCALIZED", "TEXT_STRING", "PLAIN_USER_TEXT", None),
    ("OBSERVED", "TEXT_STRING", "HARNESS_CONTEXT", "environment_context"),
    ("OBSERVED", "TEXT_STRING", "PLAIN_USER_TEXT", None),
)
_EDGE_EXPECTED = (
    ("PREFIX_UNLOCALIZED", "TEXT_STRING", "EMPTY_TEXT", None),
    ("OBSERVED", "TEXT_STRING", "HARNESS_CONTEXT", "in-app-browser-context"),
    ("OBSERVED", "TEXT_STRING", "CONTROL_SIGNAL", "turn_aborted"),
    ("OBSERVED", "TEXT_STRING", "UNKNOWN_TAGGED", None),
    ("OBSERVED", "TEXT_STRING", "HARNESS_CAPABILITY", "recommended_plugins"),
)
_PRIVACY_EXPECTED = (
    ("PREFIX_UNLOCALIZED", "DATA_URL_SUMMARY", "NO_LEADING_TEXT", None),
    ("OBSERVED", "TEXT_WITH_DATA_URL_SEGMENTS", "PLAIN_USER_TEXT", None),
    ("OBSERVED", "TEXT_WITH_DATA_URL_SEGMENTS", "NO_LEADING_TEXT", None),
    ("OBSERVED", "CONTENT_BLOCKS", "NO_LEADING_TEXT", None),
)
_PREFIX_ONLY_EXPECTED = (("PREFIX_UNLOCALIZED", "TEXT_STRING", "PLAIN_USER_TEXT", None),)

# 规格 §2.4 门④：由上面四个 capture 手算的报告向量。
_EXPECTED_COUNTS = {
    "capture_count": 4,
    "user_event_count": 13,
    "captures_with_plain_user_text": 3,
    "captures_with_plain_user_text_only_in_prefix": 1,
    "captures_with_observed_plain_user_text": 2,
    "content_form_text_string_count": 9,
    "content_form_text_with_data_url_segments_count": 2,
    "content_form_data_url_summary_count": 1,
    "content_form_content_blocks_count": 1,
    "prefix_unlocalized_plain_user_text_count": 2,
    "prefix_unlocalized_empty_text_count": 1,
    "prefix_unlocalized_no_leading_text_count": 1,
    "observed_plain_user_text_count": 2,
    "observed_harness_context_count": 2,
    "observed_harness_capability_count": 1,
    "observed_control_signal_count": 1,
    "observed_unknown_tagged_count": 1,
    "observed_no_leading_text_count": 2,
}


def _rows(run: Path, relative: str = ANNOTATIONS_RELATIVE_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run / relative).read_text(encoding="utf-8").splitlines()]


def _shape_by_capture(rows: list[dict[str, Any]]) -> set[tuple[tuple[Any, ...], ...]]:
    """按 capture 聚合（文件顺序即 sequence 顺序）→ 每 capture 一个期望元组序列。"""

    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        grouped.setdefault(row["capture_occurrence_id"], []).append(
            (row["locality"], row["content_form"], row["text_class"], row["leading_tag"])
        )
    return {tuple(shape) for shape in grouped.values()}


def _dirs(output_root: Path) -> list[Path]:
    return [c for c in output_root.iterdir() if c.is_dir()] if output_root.exists() else []


@pytest.fixture
def m1b_run(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    user_text_captures: list[dict[str, Any]],
) -> Path:
    return compile_dataset(user_text_captures, label="utp")


# --- 覆盖与报告向量 ---------------------------------------------------------------


def test_every_user_event_gets_expected_structural_annotation(
    m1b_run: Path, tmp_path: Path
) -> None:
    """13 条 USER 事件恰各一条注解；每 capture 的 (locality, form, class, tag) 序列与手算一致。"""

    run = build_user_text_projection(m1b_run_dir=m1b_run, output_root=tmp_path / "utp_out")
    rows = _rows(run)

    m1b_events = _rows(m1b_run, "private/event_occurrences.jsonl")
    user_events = [event for event in m1b_events if event["event_kind"] == "USER"]
    assert len(rows) == len(user_events) == 13
    assert {row["event_occurrence_id"] for row in rows} == {
        event["event_occurrence_id"] for event in user_events
    }
    assert _shape_by_capture(rows) == {
        _MIXED_EXPECTED,
        _EDGE_EXPECTED,
        _PRIVACY_EXPECTED,
        _PREFIX_ONLY_EXPECTED,
    }
    # 观测窗口事件恒带 boundary、前缀事件恒不带；未绑定 M1D 时全表不回指。
    for row in rows:
        assert (row["request_boundary_id"] is not None) == (row["locality"] == "OBSERVED")
        assert row["user_block_id"] is None
        assert row["schema_version"] == "traceforge.user-text-annotation.v1"


def test_source_facts_pass_through_from_m1b(m1b_run: Path, tmp_path: Path) -> None:
    """capture/boundary/utf8_byte_length 逐条等于 M1B 已发布事实；内容块无字节长度。"""

    run = build_user_text_projection(m1b_run_dir=m1b_run, output_root=tmp_path / "utp_out")
    by_id = {row["event_occurrence_id"]: row for row in _rows(run)}
    for event in _rows(m1b_run, "private/event_occurrences.jsonl"):
        if event["event_kind"] != "USER":
            continue
        row = by_id[event["event_occurrence_id"]]
        assert row["capture_occurrence_id"] == event["capture_occurrence_id"]
        assert row["request_boundary_id"] == event["request_boundary_id"]
        content = event["payload"]["content"]
        if row["content_form"] == "CONTENT_BLOCKS":
            assert row["utf8_byte_length"] is None
            assert isinstance(content["blocks"], list)
        else:
            assert row["utf8_byte_length"] == content["utf8_byte_length"]


def test_report_counts_match_hand_computed_vector(m1b_run: Path, tmp_path: Path) -> None:
    """公共报告 = 固定 allowlist；显式给出的键逐项等于手算，其余键为 0；两组计数各自守恒。"""

    run = build_user_text_projection(m1b_run_dir=m1b_run, output_root=tmp_path / "utp_out")
    report = json.loads((run / PROJECTION_REPORT_RELATIVE_PATH).read_text(encoding="utf-8"))
    counts = report["counts"]

    assert frozenset(counts) == PROJECTION_COUNT_KEYS
    for key in PROJECTION_COUNT_KEYS:
        assert counts[key] == _EXPECTED_COUNTS.get(key, 0), key
    form_total = sum(value for key, value in counts.items() if key.startswith("content_form_"))
    class_total = sum(
        value
        for key, value in counts.items()
        if key.startswith(("observed_", "prefix_unlocalized_"))
    )
    assert form_total == class_total == counts["user_event_count"]
    assert report["m1d_run_id"] is None
    assert report["projection_run_id"] == run.name


# --- 确定性与内容寻址 -----------------------------------------------------------


def test_double_build_is_byte_identical_and_content_addressed(
    m1b_run: Path, tmp_path: Path
) -> None:
    first = build_user_text_projection(m1b_run_dir=m1b_run, output_root=tmp_path / "out_a")
    second = build_user_text_projection(m1b_run_dir=m1b_run, output_root=tmp_path / "out_b")

    assert first.name == second.name
    for relative in (
        PROJECTION_MANIFEST_RELATIVE_PATH,
        PROJECTION_REPORT_RELATIVE_PATH,
        ANNOTATIONS_RELATIVE_PATH,
        "artifact_manifest.json",
    ):
        assert (first / relative).read_bytes() == (second / relative).read_bytes(), relative

    manifest = json.loads((first / PROJECTION_MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8"))
    m1b_manifest_bytes = (m1b_run / "artifact_manifest.json").read_bytes()
    assert manifest["m1b_run_id"] == m1b_run.name
    assert (
        manifest["m1b_artifact_manifest_sha256"] == hashlib.sha256(m1b_manifest_bytes).hexdigest()
    )
    assert manifest["projection_contract_version"] == USER_TEXT_PROJECTION_CONTRACT_VERSION
    assert first.name == projection_run_id(
        m1b_run_id=manifest["m1b_run_id"],
        m1b_artifact_manifest_sha256=manifest["m1b_artifact_manifest_sha256"],
        m1d_run_id=None,
        m1d_artifact_manifest_sha256=None,
    )


def test_receipt_fields_and_no_path_leak(m1b_run: Path, tmp_path: Path) -> None:
    output_root = tmp_path / "utp_out"
    run = build_user_text_projection(m1b_run_dir=m1b_run, output_root=output_root)
    receipt_text = (run / "run_receipt.json").read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert receipt["schema_version"] == PROJECTION_RUN_RECEIPT_SCHEMA
    assert receipt["run_id"] == run.name
    assert receipt["git_provenance_verified_at_completion"] is True
    assert str(m1b_run) not in receipt_text
    assert str(output_root) not in receipt_text
    artifact_manifest = json.loads((run / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert {entry["relative_path"] for entry in artifact_manifest["files"]} == {
        PROJECTION_MANIFEST_RELATIVE_PATH,
        PROJECTION_REPORT_RELATIVE_PATH,
        ANNOTATIONS_RELATIVE_PATH,
    }


# --- 内容安全 -------------------------------------------------------------------


def test_artifacts_carry_no_text_and_no_unknown_tag_names(m1b_run: Path, tmp_path: Path) -> None:
    """产物任何文件都不含用户正文、不含未知标签名；leading_tag 取值闭合于白名单。"""

    run = build_user_text_projection(m1b_run_dir=m1b_run, output_root=tmp_path / "utp_out")
    published = b"".join(path.read_bytes() for path in run.rglob("*") if path.is_file())
    for forbidden in ("虚构", USER_TEXT_UNKNOWN_TAG, "U0VDUkVUX0JBU0U2NA", "about:blank"):
        assert forbidden.encode("utf-8") not in published, forbidden

    whitelist = {tag for names in LEADING_TAG_WHITELIST.values() for tag in names}
    for row in _rows(run):
        tag = row["leading_tag"]
        assert tag is None or tag in whitelist
        if row["text_class"] == "UNKNOWN_TAGGED":
            assert tag is None


# --- M1D 可选绑定 ----------------------------------------------------------------


def test_m1d_binding_back_references_user_blocks(m1b_run: Path, tmp_path: Path) -> None:
    """绑定 M1D：观测事件回指其 UserBlock、前缀恒空；run_id 与未绑定不同且绑定身份写入 manifest。"""

    m1d_run = build_query_turns(m1b_run_dir=m1b_run, output_root=tmp_path / "m1d_out")
    unbound = build_user_text_projection(m1b_run_dir=m1b_run, output_root=tmp_path / "out_u")
    bound = build_user_text_projection(
        m1b_run_dir=m1b_run, output_root=tmp_path / "out_b", m1d_run_dir=m1d_run
    )
    assert bound.name != unbound.name

    block_by_event = {
        event_id: block["user_block_id"]
        for block in _rows(m1d_run, "private/user_blocks.jsonl")
        for event_id in block["event_ids"]
    }
    rows = _rows(bound)
    assert rows
    for row in rows:
        if row["locality"] == "OBSERVED":
            assert row["user_block_id"] == block_by_event[row["event_occurrence_id"]]
        else:
            assert row["user_block_id"] is None
    assert sum(row["user_block_id"] is not None for row in rows) == 9

    manifest = json.loads((bound / PROJECTION_MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert manifest["m1d_run_id"] == m1d_run.name
    assert manifest["m1d_artifact_manifest_sha256"] is not None
    report = json.loads((bound / PROJECTION_REPORT_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert report["m1d_run_id"] == m1d_run.name
    # 绑定只影响回指与身份，不改变任何分类计数。
    unbound_report = json.loads(
        (unbound / PROJECTION_REPORT_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert report["counts"] == unbound_report["counts"]


def test_m1d_bound_to_other_m1b_run_is_refused(
    m1b_run: Path,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """M1D 绑定的 M1B 身份与所给 M1B 不一致 → 上游绑定校验拦下，不产半份产物。"""

    other_m1b = compile_dataset(
        [user_text_dialogue(capture_factory, ["另一份虚构输入。"], request_ids=["other-1"])],
        label="utp-other",
    )
    foreign_m1d = build_query_turns(m1b_run_dir=other_m1b, output_root=tmp_path / "m1d_other")
    output_root = tmp_path / "utp_out"
    with pytest.raises(UserTextProjectionInputError):
        build_user_text_projection(
            m1b_run_dir=m1b_run, output_root=output_root, m1d_run_dir=foreign_m1d
        )
    assert _dirs(output_root) == []


# --- fail-closed ------------------------------------------------------------------


def test_tampered_m1b_run_leaves_no_partial_output(m1b_run: Path, tmp_path: Path) -> None:
    tampered = m1b_run / "private" / "event_occurrences.jsonl"
    tampered.write_bytes(tampered.read_bytes() + b'{"injected":1}\n')
    output_root = tmp_path / "utp_out"
    with pytest.raises(UserTextProjectionInputError):
        build_user_text_projection(m1b_run_dir=m1b_run, output_root=output_root)
    assert _dirs(output_root) == []


def test_missing_m1b_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UserTextProjectionInputError):
        build_user_text_projection(m1b_run_dir=tmp_path / "nope", output_root=tmp_path / "utp_out")


def test_content_address_refuses_overwrite(m1b_run: Path, tmp_path: Path) -> None:
    output_root = tmp_path / "utp_out"
    first = build_user_text_projection(m1b_run_dir=m1b_run, output_root=output_root)
    with pytest.raises(ArtifactPublishError):
        build_user_text_projection(m1b_run_dir=m1b_run, output_root=output_root)
    assert _dirs(output_root) == [first]
