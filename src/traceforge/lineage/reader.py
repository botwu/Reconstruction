"""读取并先行校验一个已发布 M1B run，产出内存有界的 ``M1bRunView``。

职责边界（[`AGENTS.md`] §4）：本模块只做 IO 与**已发布字段**抽取，不派生任何关系边（派生在
`builder.py`）。它先调用 M1B 自己的权威 validator `validate_compiled_run` 完成规格 §2.1「先校
验再消费」的全部完整性核验（逐文件重哈希、manifest 自摘要、inventory、source⇄artifact 交叉一
致、内容寻址 run_id 与目录名），因此**不复刻也不 import M1B 私有派生公式**——重算 run_id 由
M1B validator 内部完成，M1C 侧不触碰该私有约定。

抽取的字段全部来自冻结公开契约 `NormalizedCaptureV2` / `RequestBoundaryV1` /
`EventOccurrenceV2`（规格 §2.2）。`candidate_group_id` 与 `(thread_id, account_id)` 仅供
validator 做门③分区一致性与报告聚合计数取用，**绝不传入 builder**（结构性坐实门①/②）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceforge.trajectory.json_codec import (
    StrictJsonError,
    sha256_bytes,
    strict_json_loads,
)
from traceforge.trajectory.validation import validate_compiled_run


class LineageInputError(RuntimeError):
    """输入 M1B run 不可安全消费（不通过完整性核验或身份绑定不符）。"""


@dataclass(frozen=True, slots=True)
class M1bRunView:
    """一个已校验 M1B run 的只读投影，只含 M1C 建图所需的结构与指纹字段。"""

    m1b_run_id: str
    m1b_artifact_manifest_sha256: str
    source_schema: str
    capture_ids: tuple[str, ...]
    raw_request_hash_by_capture: dict[str, Any]
    # 每 capture 的 target_hash，仅供 validator 断言「无边以 target_hash 为 evidence」（§5.5）。
    target_hash_by_capture: dict[str, Any]
    # 每 capture 的 (boundary_ordinal, source_request_id)，按 ordinal 升序。
    boundaries_by_capture: dict[str, tuple[tuple[int, str], ...]]
    # 每 capture 的可见指纹链 (event_kind, visible_payload_sha256)，按 sequence_number 升序。
    fingerprint_chain_by_capture: dict[str, tuple[tuple[str, str], ...]]
    # 仅供 validator 门③与报告聚合，不进 builder。
    candidate_group_by_capture: dict[str, str]
    thread_account_by_capture: dict[str, tuple[str, str]]


def _iter_published_jsonl(root: Path, relative: str) -> Iterator[dict[str, Any]]:
    """逐行读取已校验 M1B private 表；``validate_compiled_run`` 已确认其 canonical 与 schema。"""

    path = root / relative
    with path.open("rb") as file:
        for raw in file:
            try:
                value = strict_json_loads(raw)
            except StrictJsonError as exc:  # 已校验后仍失败属环境异常，fail-closed
                raise LineageInputError(f"输入 M1B 记录无法解析：{relative}") from exc
            if not isinstance(value, dict):
                raise LineageInputError(f"输入 M1B 记录不是对象：{relative}")
            yield value


def load_m1b_run_view(m1b_run_dir: str | Path) -> M1bRunView:
    """校验并读取一个已发布 M1B run；任一核验不符即整批失败，不产半份视图。"""

    root = Path(m1b_run_dir)
    if not root.is_dir():
        raise LineageInputError("输入 M1B run 不是可读目录")

    # 规格 §2.1「先校验再消费」：完整性与身份核验全权委托 M1B 权威 validator（含重算内容寻址
    # run_id、逐文件重哈希、inventory、source⇄artifact 交叉一致）。只上报错误码、不回显任何值。
    result = validate_compiled_run(root)
    if not result.ok:
        codes = ",".join(sorted({issue.code for issue in result.issues}))
        raise LineageInputError(f"输入 M1B run 未通过独立完整性校验：{codes}")

    manifest_path = root / "artifact_manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = strict_json_loads(manifest_bytes)
    except (OSError, StrictJsonError) as exc:
        raise LineageInputError("无法读取输入 M1B artifact_manifest.json") from exc
    if not isinstance(manifest, dict):
        raise LineageInputError("输入 M1B artifact_manifest.json 顶层不是对象")

    m1b_run_id = manifest.get("run_id")
    source_schema = manifest.get("source_schema")
    if not isinstance(m1b_run_id, str) or not isinstance(source_schema, str):
        raise LineageInputError("输入 M1B artifact_manifest.json 缺少 run_id/source_schema")
    # 内容寻址绑定：M1C 以已发布 run_id 作为 m1b_run_id（规格 §2.1），并断言其为目录名。
    if root.name != m1b_run_id:
        raise LineageInputError("输入 M1B run 目录名与已发布 run_id 不一致")
    m1b_artifact_manifest_sha256 = sha256_bytes(manifest_bytes)

    raw_request_hash_by_capture: dict[str, Any] = {}
    target_hash_by_capture: dict[str, Any] = {}
    candidate_group_by_capture: dict[str, str] = {}
    thread_account_by_capture: dict[str, tuple[str, str]] = {}
    for record in _iter_published_jsonl(root, "private/captures.jsonl"):
        capture_id = record["capture_occurrence_id"]
        raw_request_hash_by_capture[capture_id] = record["raw_request_hash"]
        target_hash_by_capture[capture_id] = record["target_hash"]
        candidate_group_by_capture[capture_id] = record["candidate_group_id"]
        thread_account_by_capture[capture_id] = (record["thread_id"], record["account_id"])

    boundaries_accumulator: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for record in _iter_published_jsonl(root, "private/request_boundaries.jsonl"):
        capture_id = record["capture_occurrence_id"]
        boundaries_accumulator[capture_id].append(
            (record["boundary_ordinal"], record["source_request_id"])
        )

    fingerprint_accumulator: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for record in _iter_published_jsonl(root, "private/event_occurrences.jsonl"):
        capture_id = record["capture_occurrence_id"]
        fingerprint_accumulator[capture_id].append(
            (record["sequence_number"], record["event_kind"], record["visible_payload_sha256"])
        )

    boundaries_by_capture = {
        capture_id: tuple(sorted(items)) for capture_id, items in boundaries_accumulator.items()
    }
    fingerprint_chain_by_capture = {
        capture_id: tuple((kind, digest) for _, kind, digest in sorted(items))
        for capture_id, items in fingerprint_accumulator.items()
    }

    return M1bRunView(
        m1b_run_id=m1b_run_id,
        m1b_artifact_manifest_sha256=m1b_artifact_manifest_sha256,
        source_schema=source_schema,
        capture_ids=tuple(sorted(raw_request_hash_by_capture)),
        raw_request_hash_by_capture=raw_request_hash_by_capture,
        target_hash_by_capture=target_hash_by_capture,
        boundaries_by_capture=boundaries_by_capture,
        fingerprint_chain_by_capture=fingerprint_chain_by_capture,
        candidate_group_by_capture=candidate_group_by_capture,
        thread_account_by_capture=thread_account_by_capture,
    )
