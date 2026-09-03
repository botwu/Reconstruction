"""M1D 独立 validator：接受合法 run；对重签后的语义损坏 fail-closed（删产物/改派生洗白为首要威胁）。

两层信任边界（规格 §8）：①篡改检测——从 M1B 已发布字段重建输入、调用与 pipeline **同一个**纯 fold
重建期望，与 M1D private/ 双向 bijection；②正交不变量——不经 fold、只用 M1B 视图 + M1D 已发布表
断言分区守恒/记账对账/报告重算，用于检出 fold 自身缺陷。本文件对②专设「关掉 bijection 仍能抓到」
的用例，证明其检出力不依赖 fold。所有篡改只作用于**新建测试** run（M1B 或 M1D 侧）；冻结 R01 绝不
触碰。内容寻址 run_id 只绑输入、不绑输出字节，故定向篡改私有表并重签物理摘要后仍过完整性层——
这正是「删边/改派生洗白」得以 e2e 验证的前提。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from traceforge.query_turns import build_query_turns
from traceforge.query_turns import validation as m1d_validation
from traceforge.query_turns.validation import validate_query_turn_run
from traceforge.trajectory.json_codec import canonical_json_line

_USER_BLOCKS = "private/user_blocks.jsonl"
_AGENT_STEPS = "private/agent_steps.jsonl"
_OUTCOMES = "private/assistant_outcomes.jsonl"
_QUERY_TURNS = "private/query_turns.jsonl"
_ACCOUNTING = "private/capture_turn_accounting.jsonl"
_EDGES = "private/thread_turn_edges.jsonl"
_REPORT = "reports/m1d_report.json"
_DATA_URL = "data:image/png;base64,U0VDUkVUX0JBU0U2NA=="


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


def _resign(run: Path, relative: str) -> None:
    """重签物理 manifest 与 receipt，使语义损坏能越过纯摘要层（对抗「重签洗白」）。

    M1D run_id 只绑 M1B 输入身份、不绑输出字节，故对 M1D 私有/报告表的定向篡改重签后仍过完整性层，
    命中的是 validator 的独立复算/双向 bijection 或报告重算/隐私兜底。
    """

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


def _codes(m1d_run: Path, m1b_run: Path) -> set[str]:
    return {issue.code for issue in validate_query_turn_run(m1d_run, m1b_run).issues}


def _records(run: Path, relative: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (run / relative).read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def valid_run(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    two_boundary_capture: dict[str, Any],
    tmp_path: Path,
) -> tuple[Path, Path]:
    """合法 M1D run：含 PREFIX_ROOTED 与 OBSERVED_ROOTED 回合、TEXT/INCOMPLETE 终态、结构边。"""

    m1b_run = compile_dataset(
        [_capture(capture_factory, ["r1", "r2"]), two_boundary_capture], label="m1d-valid"
    )
    m1d_run = build_query_turns(m1b_run_dir=m1b_run, output_root=tmp_path / "m1d_out")
    return m1d_run, m1b_run


# --- 正常路径 --------------------------------------------------------------


def test_validator_accepts_freshly_built_run(valid_run: tuple[Path, Path]) -> None:
    """新建 M1D run 通过全部核验：ok、有 checked_file_count、重算聚合计数守恒。"""

    m1d_run, m1b_run = valid_run
    result = validate_query_turn_run(m1d_run, m1b_run)

    assert result.ok, result.errors
    assert result.checked_file_count >= 1
    counts = result.observed_counts
    assert counts["capture_count"] == 2
    assert counts["query_turn_count"] == 4
    assert counts["prefix_rooted_turn_count"] == 2
    assert counts["observed_rooted_turn_count"] == 2
    assert counts["complete_turn_count"] == 3
    assert counts["incomplete_turn_count"] == 1
    assert counts["agent_step_count"] == 5
    assert counts["user_block_count"] == 2
    assert counts["assistant_outcome_count"] == 3
    assert counts["thread_turn_edge_count"] == 2
    assert counts["orphan_tool_observation_count"] == 0
    assert counts["captures_with_unlocalizable_prefix_count"] == 2
    # 跨 oracle 终态 tripwire 在合法 run 上保持沉默（不可由黑盒篡改强制触发，见模块说明）。
    assert "QUERY_TURN_TERMINAL_STATUS_MISMATCH" not in _codes(m1d_run, m1b_run)


# --- 产物完整性（重签前的纯摘要层）------------------------------------------


def test_unsigned_sha_tamper_is_caught(valid_run: tuple[Path, Path]) -> None:
    """篡改私有表但不重签 → 逐文件重哈希抓 ARTIFACT_SHA256_MISMATCH。"""

    m1d_run, m1b_run = valid_run
    path = m1d_run / _QUERY_TURNS
    path.write_bytes(path.read_bytes() + b'{"unsigned":1}\n')  # 不调用 _resign

    assert "ARTIFACT_SHA256_MISMATCH" in _codes(m1d_run, m1b_run)


# --- 双向 bijection：删产物 / 幻影 / 篡改（重签洗白为首要威胁）-----------------


def test_missing_query_turn_is_caught(valid_run: tuple[Path, Path]) -> None:
    """删一个 QueryTurn 并重签 → bijection 抓 M1D_QUERY_TURN_MISSING。"""

    m1d_run, m1b_run = valid_run
    _rewrite(m1d_run / _QUERY_TURNS, lambda records: records[1:])
    _resign(m1d_run, _QUERY_TURNS)

    assert "M1D_QUERY_TURN_MISSING" in _codes(m1d_run, m1b_run)


def test_missing_user_block_is_caught(valid_run: tuple[Path, Path]) -> None:
    """删一个 UserBlock 并重签 → M1D_USER_BLOCK_MISSING。"""

    m1d_run, m1b_run = valid_run
    _rewrite(m1d_run / _USER_BLOCKS, lambda records: records[1:])
    _resign(m1d_run, _USER_BLOCKS)

    assert "M1D_USER_BLOCK_MISSING" in _codes(m1d_run, m1b_run)


def test_missing_thread_edge_is_caught(valid_run: tuple[Path, Path]) -> None:
    """删一条结构边并重签 → M1D_THREAD_EDGE_MISSING。"""

    m1d_run, m1b_run = valid_run
    _rewrite(m1d_run / _EDGES, lambda records: records[1:])
    _resign(m1d_run, _EDGES)

    assert "M1D_THREAD_EDGE_MISSING" in _codes(m1d_run, m1b_run)


def test_phantom_agent_step_is_caught(valid_run: tuple[Path, Path]) -> None:
    """注入独立复算不产出（assistant 事件不存在）的幻影 AgentStep 并重签 → PHANTOM。"""

    m1d_run, m1b_run = valid_run

    def inject(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        template = dict(records[0])
        template["agent_step_id"] = "f" * 64  # 独立复算绝不产出此 ID
        return [*records, template]

    _rewrite(m1d_run / _AGENT_STEPS, inject)
    _resign(m1d_run, _AGENT_STEPS)

    assert "M1D_AGENT_STEP_PHANTOM" in _codes(m1d_run, m1b_run)


def test_tampered_turn_status_is_caught(valid_run: tuple[Path, Path]) -> None:
    """把某完成回合的 turn_status 翻成 INCOMPLETE（保留 ID）并重签 → M1D_QUERY_TURN_MISMATCH。"""

    m1d_run, m1b_run = valid_run

    def tamper(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        target = next(record for record in records if record["turn_status"] == "COMPLETE")
        target["turn_status"] = "INCOMPLETE"
        return records

    _rewrite(m1d_run / _QUERY_TURNS, tamper)
    _resign(m1d_run, _QUERY_TURNS)

    assert "M1D_QUERY_TURN_MISMATCH" in _codes(m1d_run, m1b_run)


def test_tampered_outcome_kind_is_caught(valid_run: tuple[Path, Path]) -> None:
    """翻转 AssistantOutcome 的 outcome_kind（保留 ID）并重签 → M1D_ASSISTANT_OUTCOME_MISMATCH。"""

    m1d_run, m1b_run = valid_run

    def tamper(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records[0]["outcome_kind"] = "EMPTY_OUTCOME"
        return records

    _rewrite(m1d_run / _OUTCOMES, tamper)
    _resign(m1d_run, _OUTCOMES)

    assert "M1D_ASSISTANT_OUTCOME_MISMATCH" in _codes(m1d_run, m1b_run)


def test_tampered_agent_step_attribution_is_caught(valid_run: tuple[Path, Path]) -> None:
    """给某 AgentStep 注入伪造 unresolved 调用（保留 ID）并重签 → M1D_AGENT_STEP_MISMATCH。"""

    m1d_run, m1b_run = valid_run

    def tamper(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records[0]["unresolved_tool_call_event_ids"] = ["injected-call"]
        return records

    _rewrite(m1d_run / _AGENT_STEPS, tamper)
    _resign(m1d_run, _AGENT_STEPS)

    assert "M1D_AGENT_STEP_MISMATCH" in _codes(m1d_run, m1b_run)


def test_tampered_accounting_is_caught(valid_run: tuple[Path, Path]) -> None:
    """篡改 capture 记账的孤儿观测列表（保留 capture id）并重签 → CAPTURE_ACCOUNTING_MISMATCH。"""

    m1d_run, m1b_run = valid_run

    def tamper(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records[0]["orphan_tool_observation_event_ids"] = ["injected-orphan"]
        return records

    _rewrite(m1d_run / _ACCOUNTING, tamper)
    _resign(m1d_run, _ACCOUNTING)

    assert "M1D_CAPTURE_ACCOUNTING_MISMATCH" in _codes(m1d_run, m1b_run)


def test_duplicate_id_is_caught(valid_run: tuple[Path, Path]) -> None:
    """同表重复主键 ID 并重签 → QUERY_TURN_DUPLICATE_ID（bijection 因字节相同不会察觉重复行）。"""

    m1d_run, m1b_run = valid_run
    _rewrite(m1d_run / _QUERY_TURNS, lambda records: [records[0], *records])
    _resign(m1d_run, _QUERY_TURNS)

    assert "QUERY_TURN_DUPLICATE_ID" in _codes(m1d_run, m1b_run)


# --- 隐私兜底（派生产物不得回灌 Data URL）-----------------------------------


def test_data_url_in_edge_evidence_is_caught(valid_run: tuple[Path, Path]) -> None:
    """结构边 evidence 注入 Data URL 并重签 → 隐私兜底抓 BASE64_DATA_URL_OBSERVED。"""

    m1d_run, m1b_run = valid_run

    def inject(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records[0]["evidence"] = {**records[0]["evidence"], "leak": _DATA_URL}
        return records

    _rewrite(m1d_run / _EDGES, inject)
    _resign(m1d_run, _EDGES)

    assert "BASE64_DATA_URL_OBSERVED" in _codes(m1d_run, m1b_run)


# --- 公共报告聚合计数 / 白名单 ----------------------------------------------


def test_report_count_mismatch_is_caught(valid_run: tuple[Path, Path]) -> None:
    """篡改公共报告某聚合计数并重签 → QUERY_TURN_REPORT_COUNT_MISMATCH。"""

    m1d_run, m1b_run = valid_run
    report_path = m1d_run / _REPORT
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["counts"]["query_turn_count"] = report["counts"]["query_turn_count"] + 100
    report_path.write_bytes(canonical_json_line(report))
    _resign(m1d_run, _REPORT)

    assert "QUERY_TURN_REPORT_COUNT_MISMATCH" in _codes(m1d_run, m1b_run)


def test_report_allowlist_mismatch_is_caught(valid_run: tuple[Path, Path]) -> None:
    """给公共报告 counts 增设白名单外的键并重签 → QUERY_TURN_REPORT_ALLOWLIST_MISMATCH。"""

    m1d_run, m1b_run = valid_run
    report_path = m1d_run / _REPORT
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["counts"]["unexpected_extra_count"] = 0
    report_path.write_bytes(canonical_json_line(report))
    _resign(m1d_run, _REPORT)

    assert "QUERY_TURN_REPORT_ALLOWLIST_MISMATCH" in _codes(m1d_run, m1b_run)


# --- M1B 身份绑定 -----------------------------------------------------------


def test_binding_wrong_m1b_oracle_is_caught(
    valid_run: tuple[Path, Path],
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    """用另一套 M1B run 作 oracle 校验（无篡改）→ 内容寻址身份绑定不符（run_id + manifest sha）。"""

    m1d_run, _m1b_a = valid_run
    m1b_b = compile_dataset(
        [_capture(capture_factory, ["z1", "z2"]), _capture(capture_factory, ["z3", "z4"])],
        label="m1d-oracle-b",
    )
    codes = _codes(m1d_run, m1b_b)

    assert "M1B_RUN_ID_BINDING_MISMATCH" in codes
    assert "M1B_MANIFEST_SHA_BINDING_MISMATCH" in codes


def test_invalid_m1b_oracle_fails_closed(valid_run: tuple[Path, Path], tmp_path: Path) -> None:
    """所给 M1B oracle 不是合法 run（空目录）→ M1B_INPUT_INVALID，且不冒充通过。"""

    m1d_run, _m1b_run = valid_run
    bogus = tmp_path / "not-an-m1b-run"
    bogus.mkdir()
    result = validate_query_turn_run(m1d_run, bogus)

    assert not result.ok
    assert "M1B_INPUT_INVALID" in {issue.code for issue in result.issues}
    assert result.observed_counts == {}


# --- 记账/终态覆盖：孤儿观测、量化终态 --------------------------------------


def test_text_only_multi_turn_run_counts(
    stable_git_provenance: None,
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    """纯文本多回合 run：无孤儿、无 compaction，重算计数与报告一致且 ok。"""

    m1b_run = compile_dataset(
        [_capture(capture_factory, ["a1", "a2", "a3"]), _capture(capture_factory, ["b1"])],
        label="m1d-counts",
    )
    m1d_run = build_query_turns(m1b_run_dir=m1b_run, output_root=tmp_path / "m1d_counts_out")
    result = validate_query_turn_run(m1d_run, m1b_run)

    assert result.ok, result.errors
    assert result.observed_counts["orphan_tool_observation_count"] == 0
    assert result.observed_counts["captures_with_compaction_count"] == 0


# --- 正交不变量：关掉 bijection 仍能抓到（检出力不依赖 fold，规格 §8 ②）-------

_BIJECTION_CODE_PREFIXES = (
    "M1D_USER_BLOCK_",
    "M1D_AGENT_STEP_",
    "M1D_ASSISTANT_OUTCOME_",
    "M1D_QUERY_TURN_",
    "M1D_CAPTURE_ACCOUNTING_",
    "M1D_THREAD_EDGE_",
)


def _codes_without_bijection(
    monkeypatch: pytest.MonkeyPatch, m1d_run: Path, m1b_run: Path
) -> set[str]:
    """把篡改检测层置空，只留不变量层；返回的 code 集合中不应再有任何 bijection 产物。"""

    monkeypatch.setattr(m1d_validation, "bijection", lambda *args, **kwargs: None)
    codes = _codes(m1d_run, m1b_run)
    assert not {code for code in codes if code.startswith(_BIJECTION_CODE_PREFIXES)}, codes
    return codes


def _step_with_observation(m1d_run: Path) -> dict[str, Any]:
    return next(
        record for record in _records(m1d_run, _AGENT_STEPS) if record["tool_observation_event_ids"]
    )


def _tamper_accounting(m1d_run: Path, cid: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    def transform(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        target = next(record for record in records if record["capture_occurrence_id"] == cid)
        mutate(target)
        return records

    _rewrite(m1d_run / _ACCOUNTING, transform)
    _resign(m1d_run, _ACCOUNTING)


def _uncovered(m1d_run: Path) -> None:
    """UserBlock 丢一个 USER 事件 → 该观测事件无人覆盖。"""

    def transform(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records[0]["event_ids"] = records[0]["event_ids"][1:]
        return records

    _rewrite(m1d_run / _USER_BLOCKS, transform)
    _resign(m1d_run, _USER_BLOCKS)


def _overlap(m1d_run: Path) -> None:
    """把某 AgentStep 已归属的观测结果同时列为孤儿 → 同 kind、被覆盖两次。"""

    step = _step_with_observation(m1d_run)
    observation = step["tool_observation_event_ids"][0]
    _tamper_accounting(
        m1d_run,
        step["capture_occurrence_id"],
        lambda row: row.__setitem__("orphan_tool_observation_event_ids", [observation]),
    )


def _foreign(m1d_run: Path) -> None:
    """孤儿列表引用不存在于本 capture 观测窗口的事件。"""

    step = _step_with_observation(m1d_run)
    _tamper_accounting(
        m1d_run,
        step["capture_occurrence_id"],
        lambda row: row.__setitem__("orphan_tool_observation_event_ids", ["not-an-observed-event"]),
    )


def _kind_mismatch(m1d_run: Path) -> None:
    """把 assistant 事件列为孤儿观测 → 划分 kind 与事件 kind 不符。"""

    step = _step_with_observation(m1d_run)
    _tamper_accounting(
        m1d_run,
        step["capture_occurrence_id"],
        lambda row: row.__setitem__(
            "orphan_tool_observation_event_ids", [step["assistant_event_id"]]
        ),
    )


def _observed_count(m1d_run: Path) -> None:
    cid = _records(m1d_run, _ACCOUNTING)[0]["capture_occurrence_id"]

    def mutate(row: dict[str, Any]) -> None:
        row["observed_event_counts_by_kind"]["ASSISTANT_MESSAGE"] += 1

    _tamper_accounting(m1d_run, cid, mutate)


def _prefix_count(m1d_run: Path) -> None:
    cid = _records(m1d_run, _ACCOUNTING)[0]["capture_occurrence_id"]

    def mutate(row: dict[str, Any]) -> None:
        counts = row["prefix_event_counts_by_kind"]
        counts["USER"] = counts.get("USER", 0) + 1

    _tamper_accounting(m1d_run, cid, mutate)


def _drop_turn(m1d_run: Path) -> None:
    """删一个 QueryTurn：报告计数须由**已发布表**重算，故不靠 fold 也能对不上。"""

    _rewrite(m1d_run / _QUERY_TURNS, lambda records: records[1:])
    _resign(m1d_run, _QUERY_TURNS)


@pytest.mark.parametrize(
    ("tamper", "expected_code"),
    [
        pytest.param(_uncovered, "M1D_PARTITION_UNCOVERED", id="uncovered"),
        pytest.param(_overlap, "M1D_PARTITION_OVERLAP", id="overlap"),
        pytest.param(_foreign, "M1D_PARTITION_FOREIGN_EVENT", id="foreign"),
        pytest.param(_kind_mismatch, "M1D_PARTITION_KIND_MISMATCH", id="kind"),
        pytest.param(
            _observed_count, "M1D_ACCOUNTING_OBSERVED_COUNT_MISMATCH", id="observed-count"
        ),
        pytest.param(_prefix_count, "M1D_ACCOUNTING_PREFIX_COUNT_MISMATCH", id="prefix-count"),
        pytest.param(_drop_turn, "QUERY_TURN_REPORT_COUNT_MISMATCH", id="report-from-tables"),
    ],
)
def test_invariants_catch_corruption_without_fold(
    valid_run: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tamper: Callable[[Path], None],
    expected_code: str,
) -> None:
    """不变量层在 bijection 被置空后仍 fail-closed；同一篡改下完整 validator 也报同一 code。"""

    m1d_run, m1b_run = valid_run
    tamper(m1d_run)

    assert expected_code in _codes(m1d_run, m1b_run)
    assert expected_code in _codes_without_bijection(monkeypatch, m1d_run, m1b_run)


def test_invariants_silent_on_valid_run_without_fold(
    valid_run: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """合法 run 上关掉 bijection 后不变量层零误报（不变量本身不制造噪音）。"""

    m1d_run, m1b_run = valid_run
    assert _codes_without_bijection(monkeypatch, m1d_run, m1b_run) == set()
