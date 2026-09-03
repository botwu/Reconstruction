"""`UserTextProjection` 独立 validator：接受合法 run；对重签后的语义损坏 fail-closed（规格 §3）。

两层信任边界：①篡改检测——从 M1B（+M1D）已发布字段重建视图、调用与 pipeline **同一个**纯 fold
重建期望，与已发布注解表双向 bijection；②正交不变量——不经 fold/分类函数，只用上游视图 + 已发布表
断言来源事实、类别结构、M1D 回指、报告重算与跨模块不变量。本文件对②专设「关掉 bijection 仍能抓到」
的用例。所有篡改只作用于**新建测试** run；投影 run_id 只绑输入身份、不绑输出字节，故定向篡改私有表并
重签物理摘要后仍过完整性层——这正是「删行/改类别洗白」得以 e2e 验证的前提。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import user_text_dialogue

from traceforge.query_turns import build_query_turns
from traceforge.source_projection import build_user_text_projection
from traceforge.source_projection import validation as utp_validation
from traceforge.source_projection.contracts import (
    ANNOTATIONS_RELATIVE_PATH,
    PROJECTION_MANIFEST_RELATIVE_PATH,
    PROJECTION_REPORT_RELATIVE_PATH,
)
from traceforge.source_projection.validation import validate_user_text_projection_run
from traceforge.trajectory.json_codec import canonical_json_line

_ANNOTATIONS = ANNOTATIONS_RELATIVE_PATH
_REPORT = PROJECTION_REPORT_RELATIVE_PATH
_MANIFEST = PROJECTION_MANIFEST_RELATIVE_PATH

Transform = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


def _rewrite(path: Path, transform: Transform) -> None:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    path.write_bytes(b"".join(canonical_json_line(record) for record in transform(records)))


def _rewrite_json(path: Path, transform: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    path.write_bytes(canonical_json_line(transform(json.loads(path.read_text(encoding="utf-8")))))


def _resign(run: Path, relative: str) -> None:
    """重签物理 manifest 与 receipt，使语义损坏能越过纯摘要层（对抗「重签洗白」）。"""

    raw = (run / relative).read_bytes()
    manifest_path = run / "artifact_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    [entry] = [item for item in manifest["files"] if item["relative_path"] == relative]
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    entry["byte_length"] = len(raw)
    if relative.endswith(".jsonl"):
        entry["record_count"] = raw.count(b"\n")
    manifest_path.write_bytes(canonical_json_line(manifest))
    receipt_path = run / "run_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["artifact_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    receipt_path.write_bytes(canonical_json_line(receipt))


def _codes(run: Path, m1b_run: Path, m1d_run: Path | None = None) -> set[str]:
    return {issue.code for issue in validate_user_text_projection_run(run, m1b_run, m1d_run).issues}


def _rows(run: Path, relative: str = _ANNOTATIONS) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run / relative).read_text(encoding="utf-8").splitlines()]


def _first(rows: list[dict[str, Any]], **where: Any) -> dict[str, Any]:
    for row in rows:
        if all(row[key] == value for key, value in where.items()):
            return row
    raise AssertionError(f"没有满足条件的注解行：{where}")


@pytest.fixture
def m1b_run(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    user_text_captures: list[dict[str, Any]],
) -> Path:
    return compile_dataset(user_text_captures, label="utp-val")


@pytest.fixture
def unbound(m1b_run: Path, tmp_path: Path) -> tuple[Path, Path]:
    run = build_user_text_projection(m1b_run_dir=m1b_run, output_root=tmp_path / "utp_out")
    return run, m1b_run


@pytest.fixture
def bound(m1b_run: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    m1d_run = build_query_turns(m1b_run_dir=m1b_run, output_root=tmp_path / "m1d_out")
    run = build_user_text_projection(
        m1b_run_dir=m1b_run, output_root=tmp_path / "utp_bound", m1d_run_dir=m1d_run
    )
    return run, m1b_run, m1d_run


@pytest.fixture
def no_bijection(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉篡改检测层，证明正交不变量层的检出力不依赖 fold。"""

    monkeypatch.setattr(utp_validation, "bijection", lambda *args, **kwargs: None)


# --- 正常路径 --------------------------------------------------------------


def test_validator_accepts_fresh_unbound_run(unbound: tuple[Path, Path]) -> None:
    run, m1b_run = unbound
    result = validate_user_text_projection_run(run, m1b_run)
    assert result.ok, result.errors
    assert result.checked_file_count == 3
    assert result.observed_counts["capture_count"] == 4
    assert result.observed_counts["user_event_count"] == 13
    assert result.observed_counts["captures_with_observed_plain_user_text"] == 2


def test_validator_accepts_fresh_bound_run(bound: tuple[Path, Path, Path]) -> None:
    run, m1b_run, m1d_run = bound
    result = validate_user_text_projection_run(run, m1b_run, m1d_run)
    assert result.ok, result.errors
    assert result.observed_counts["user_event_count"] == 13


def test_report_is_recomputed_from_published_table(unbound: tuple[Path, Path]) -> None:
    run, m1b_run = unbound
    report = json.loads((run / _REPORT).read_text(encoding="utf-8"))
    assert validate_user_text_projection_run(run, m1b_run).observed_counts == report["counts"]


# --- 身份与 oracle 绑定 -----------------------------------------------------------


def test_bound_run_requires_m1d_oracle(bound: tuple[Path, Path, Path]) -> None:
    run, m1b_run, _ = bound
    assert "M1D_RUN_REQUIRED" in _codes(run, m1b_run)


def test_unbound_run_rejects_unexpected_m1d_oracle(
    unbound: tuple[Path, Path], tmp_path: Path
) -> None:
    run, m1b_run = unbound
    m1d_run = build_query_turns(m1b_run_dir=m1b_run, output_root=tmp_path / "m1d_extra")
    assert "M1D_BINDING_MISMATCH" in _codes(run, m1b_run, m1d_run)


def test_wrong_m1b_oracle_is_detected(
    unbound: tuple[Path, Path],
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    run, _ = unbound
    other = compile_dataset(
        [user_text_dialogue(capture_factory, ["另一份虚构输入。"], request_ids=["o-1"])],
        label="utp-val-other",
    )
    codes = _codes(run, other)
    assert {"M1B_RUN_ID_BINDING_MISMATCH", "PROJECTION_RUN_DIRECTORY_ID_MISMATCH"} <= codes


def test_wrong_m1d_oracle_is_detected(
    bound: tuple[Path, Path, Path],
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """所给 M1D 绑定到另一个 M1B → M1D 自身的绑定校验先拦下（M1D_INPUT_INVALID）。"""

    run, m1b_run, _ = bound
    other_m1b = compile_dataset(
        [user_text_dialogue(capture_factory, ["另一份虚构输入。"], request_ids=["o-1"])],
        label="utp-val-other",
    )
    foreign_m1d = build_query_turns(m1b_run_dir=other_m1b, output_root=tmp_path / "m1d_foreign")
    assert "M1D_INPUT_INVALID" in _codes(run, m1b_run, foreign_m1d)


def test_tampered_upstream_m1b_fails_closed(unbound: tuple[Path, Path]) -> None:
    run, m1b_run = unbound
    events = m1b_run / "private" / "event_occurrences.jsonl"
    events.write_bytes(events.read_bytes() + b'{"injected":1}\n')
    codes = _codes(run, m1b_run)
    assert "M1B_INPUT_INVALID" in codes


def test_missing_or_not_directory_run(unbound: tuple[Path, Path], tmp_path: Path) -> None:
    _, m1b_run = unbound
    result = validate_user_text_projection_run(tmp_path / "nope", m1b_run)
    assert not result.ok
    assert {issue.code for issue in result.issues} == {"PROJECTION_RUN_NOT_DIRECTORY"}


# --- 物理层 -----------------------------------------------------------------------


def test_unsigned_byte_change_is_caught_by_digests(unbound: tuple[Path, Path]) -> None:
    run, m1b_run = unbound
    path = run / _ANNOTATIONS
    path.write_bytes(path.read_bytes().replace(b"PLAIN_USER_TEXT", b"PLAIN_USER_TEXX", 1))
    codes = _codes(run, m1b_run)
    assert "ARTIFACT_SHA256_MISMATCH" in codes


def test_missing_report_file_is_caught(unbound: tuple[Path, Path]) -> None:
    run, m1b_run = unbound
    (run / _REPORT).unlink()
    codes = _codes(run, m1b_run)
    assert {"ARTIFACT_NOT_REGULAR_FILE", "RUN_FILE_SET_MISMATCH", "JSON_FILE_MISSING"} <= codes


# --- 篡改检测层（同一纯 fold 的双向 bijection）------------------------------------------


def test_deleted_annotation_row_after_resign_is_caught(unbound: tuple[Path, Path]) -> None:
    run, m1b_run = unbound
    _rewrite(run / _ANNOTATIONS, lambda rows: rows[1:])
    _resign(run, _ANNOTATIONS)
    codes = _codes(run, m1b_run)
    assert "USER_TEXT_ANNOTATION_MISSING" in codes
    assert "USER_TEXT_EVENT_UNCOVERED" in codes
    assert "PROJECTION_REPORT_COUNT_MISMATCH" in codes


def test_relabelled_class_with_consistent_report_is_caught_only_by_fold(
    unbound: tuple[Path, Path],
) -> None:
    """观测普通文本改成合法白名单类别并同步改报告：结构不变量无法区分，只有 fold 复算能推翻。"""

    run, m1b_run = unbound

    def relabel(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        row = _first(rows, locality="OBSERVED", text_class="PLAIN_USER_TEXT")
        row["text_class"] = "HARNESS_CONTEXT"
        row["leading_tag"] = "environment_context"
        return rows

    _rewrite(run / _ANNOTATIONS, relabel)
    _resign(run, _ANNOTATIONS)

    # 把报告调到与篡改后的表完全自洽（validator 的 observed_counts 正是由已发布表重算的）。
    recomputed = validate_user_text_projection_run(run, m1b_run).observed_counts

    def align(report: dict[str, Any]) -> dict[str, Any]:
        report["counts"] = recomputed
        return report

    _rewrite_json(run / _REPORT, align)
    _resign(run, _REPORT)
    codes = _codes(run, m1b_run)
    assert "USER_TEXT_ANNOTATION_MISMATCH" in codes
    assert "PROJECTION_REPORT_COUNT_MISMATCH" not in codes
    assert not codes & {"USER_TEXT_LEADING_TAG_INVALID", "USER_TEXT_CLASS_HEAD_MISMATCH"}


def test_phantom_row_is_caught(unbound: tuple[Path, Path]) -> None:
    run, m1b_run = unbound

    def add_phantom(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        phantom = dict(rows[0])
        phantom["event_occurrence_id"] = "f" * 64
        return [*rows, phantom]

    _rewrite(run / _ANNOTATIONS, add_phantom)
    _resign(run, _ANNOTATIONS)
    codes = _codes(run, m1b_run)
    assert {"USER_TEXT_ANNOTATION_PHANTOM", "USER_TEXT_EVENT_FOREIGN"} <= codes


# --- 正交不变量层（关掉 bijection 仍能抓到）--------------------------------------------


def test_invariants_catch_unknown_tagged_row_carrying_tag(
    no_bijection: None, unbound: tuple[Path, Path]
) -> None:
    run, m1b_run = unbound

    def leak_tag(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        _first(rows, text_class="UNKNOWN_TAGGED")["leading_tag"] = "mystery_tag"
        return rows

    _rewrite(run / _ANNOTATIONS, leak_tag)
    _resign(run, _ANNOTATIONS)
    assert "USER_TEXT_LEADING_TAG_INVALID" in _codes(run, m1b_run)


def test_invariants_catch_tag_outside_class_whitelist(
    no_bijection: None, unbound: tuple[Path, Path]
) -> None:
    run, m1b_run = unbound

    def cross_tag(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        _first(rows, text_class="CONTROL_SIGNAL")["leading_tag"] = "environment_context"
        return rows

    _rewrite(run / _ANNOTATIONS, cross_tag)
    _resign(run, _ANNOTATIONS)
    assert "USER_TEXT_LEADING_TAG_INVALID" in _codes(run, m1b_run)


def test_invariants_catch_class_incompatible_with_head_structure(
    no_bijection: None, unbound: tuple[Path, Path]
) -> None:
    """无开头文本却标普通文本 / 有非空开头文本却标空文本，均与结构事实不相容。"""

    run, m1b_run = unbound

    def relabel(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        _first(rows, content_form="DATA_URL_SUMMARY")["text_class"] = "PLAIN_USER_TEXT"
        capability = _first(rows, text_class="HARNESS_CAPABILITY")
        capability["text_class"] = "EMPTY_TEXT"
        capability["leading_tag"] = None
        return rows

    _rewrite(run / _ANNOTATIONS, relabel)
    _resign(run, _ANNOTATIONS)
    assert "USER_TEXT_CLASS_HEAD_MISMATCH" in _codes(run, m1b_run)


def test_invariants_catch_source_fact_drift(no_bijection: None, unbound: tuple[Path, Path]) -> None:
    run, m1b_run = unbound

    def drift(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        row = _first(rows, locality="PREFIX_UNLOCALIZED", text_class="EMPTY_TEXT")
        row["locality"] = "OBSERVED"
        other = _first(rows, content_form="CONTENT_BLOCKS")
        other["utf8_byte_length"] = 1
        return rows

    _rewrite(run / _ANNOTATIONS, drift)
    _resign(run, _ANNOTATIONS)
    assert "USER_TEXT_SOURCE_FACT_MISMATCH" in _codes(run, m1b_run)


def test_invariants_catch_block_binding_drift(
    no_bijection: None, bound: tuple[Path, Path, Path]
) -> None:
    run, m1b_run, m1d_run = bound

    def swap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        observed = [row for row in rows if row["locality"] == "OBSERVED"]
        first, second = observed[0], observed[-1]
        first["user_block_id"], second["user_block_id"] = (
            second["user_block_id"],
            first["user_block_id"],
        )
        prefix = _first(rows, locality="PREFIX_UNLOCALIZED", text_class="EMPTY_TEXT")
        prefix["user_block_id"] = first["user_block_id"]
        return rows

    _rewrite(run / _ANNOTATIONS, swap)
    _resign(run, _ANNOTATIONS)
    assert "USER_TEXT_BLOCK_BINDING_INVALID" in _codes(run, m1b_run, m1d_run)


def test_unbound_table_must_not_back_reference(
    no_bijection: None, unbound: tuple[Path, Path]
) -> None:
    run, m1b_run = unbound

    def bind(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        _first(rows, locality="OBSERVED", text_class="CONTROL_SIGNAL")["user_block_id"] = "x" * 64
        return rows

    _rewrite(run / _ANNOTATIONS, bind)
    _resign(run, _ANNOTATIONS)
    assert "USER_TEXT_BLOCK_BINDING_INVALID" in _codes(run, m1b_run)


def test_invariants_recompute_report_from_table(
    no_bijection: None, unbound: tuple[Path, Path]
) -> None:
    run, m1b_run = unbound

    def inflate(report: dict[str, Any]) -> dict[str, Any]:
        report["counts"]["captures_with_observed_plain_user_text"] += 1
        return report

    _rewrite_json(run / _REPORT, inflate)
    _resign(run, _REPORT)
    assert "PROJECTION_REPORT_COUNT_MISMATCH" in _codes(run, m1b_run)


def test_report_allowlist_is_closed(unbound: tuple[Path, Path]) -> None:
    run, m1b_run = unbound

    def leak(report: dict[str, Any]) -> dict[str, Any]:
        report["counts"]["leading_tag_mystery_count"] = 1
        return report

    _rewrite_json(run / _REPORT, leak)
    _resign(run, _REPORT)
    assert "PROJECTION_REPORT_ALLOWLIST_MISMATCH" in _codes(run, m1b_run)


def test_cross_module_invariant_detects_m1d_block_loss(
    no_bijection: None, bound: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """模拟 M1D 丢失 UserBlock 的派生缺陷：观测普通文本 capture 数 > 有 UserBlock 的 capture 数。"""

    run, m1b_run, m1d_run = bound
    real_loader = utp_validation.load_m1d_block_view

    def lossy_loader(m1d_dir: Path, m1b_dir: Path) -> Any:
        view = real_loader(m1d_dir, m1b_dir)
        # 所有 UserBlock 都记到同一个 capture：回指索引不变，但"有 UserBlock 的 capture"只剩 1 个。
        [only_capture] = sorted(view.capture_ids_with_user_blocks)[:1]
        return replace(
            view,
            capture_id_by_user_block_id=dict.fromkeys(
                view.capture_id_by_user_block_id, only_capture
            ),
        )

    monkeypatch.setattr(utp_validation, "load_m1d_block_view", lossy_loader)
    codes = _codes(run, m1b_run, m1d_run)
    assert "USER_TEXT_CROSS_MODULE_INVARIANT_VIOLATED" in codes
    # 同时，回指 UserBlock 与注解 capture 不一致也被逐条抓到（规格 §3）。
    assert "USER_TEXT_BLOCK_BINDING_INVALID" in codes


def test_manifest_binding_drift_is_caught(unbound: tuple[Path, Path]) -> None:
    run, m1b_run = unbound

    def drift(manifest: dict[str, Any]) -> dict[str, Any]:
        manifest["m1b_artifact_manifest_sha256"] = "0" * 64
        return manifest

    _rewrite_json(run / _MANIFEST, drift)
    _resign(run, _MANIFEST)
    assert "M1B_MANIFEST_SHA_BINDING_MISMATCH" in _codes(run, m1b_run)
