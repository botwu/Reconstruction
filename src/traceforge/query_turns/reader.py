"""读取并先行校验一个已发布 M1B run，产出 builder 消费的 ``M1bTurnView``（规格 §2.1/§4）。

职责边界（[`AGENTS.md`] §4，镜像 `lineage/reader.py`）：本模块只做 IO 与**已发布字段**抽取，
不派生任何回合结构（派生全在 `builder.py`）。它先调用 M1B 权威 validator `validate_compiled_run`
完成「先校验再消费」的全部完整性核验（逐文件重哈希、manifest 自摘要、inventory、source⇄artifact
交叉一致、内容寻址 run_id 与目录名），因此**不复刻也不 import M1B 私有派生公式**——上游 ID 一律
当不透明外键。

content 空/非空判定所需的**冻结标量**（``TextContent.utf8_byte_length`` /
``ContentBlocks.block_count`` / ``CONTENT_BLOCKS`` 种类位）由 `event_payload.py` typed reader
在此预抽取到 ``ObservedEvent``——builder 因此完全不触碰 payload（规格 §1 勘误、§4 步 4）。

注：M1B validator 校验字段集合与 canonical，但不逐值校验透传标量的 Python 类型。为把下游对错类型
值的裸 `TypeError` 前移为 fail-closed 的 `QueryTurnInputError`，本模块对入图所需标量（事件/批次/
配对的 ID、序数、kind、scope、状态、has_compaction）另做类型守卫。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from traceforge.trajectory.contracts import EventKind, EventScope
from traceforge.trajectory.event_payload import (
    ContentBlocks,
    EventPayloadReadError,
    read_event_payload,
)
from traceforge.trajectory.json_codec import (
    StrictJsonError,
    sha256_bytes,
    strict_json_loads,
)
from traceforge.trajectory.validation import validate_compiled_run

from .view import ActionBatchInfo, CaptureInput, M1bTurnView, ObservedEvent, ToolPairingLink


class QueryTurnInputError(RuntimeError):
    """输入 M1B run 不可安全消费（完整性核验不过、身份绑定不符、或透传字段类型非法）。"""


def _iter_published_jsonl(root: Path, relative: str) -> Iterator[dict[str, Any]]:
    """逐行读取已校验 M1B private 表；``validate_compiled_run`` 已确认其 canonical 与 schema。"""

    path = root / relative
    if not path.is_file():
        raise QueryTurnInputError(f"输入 M1B 缺少必需表：{relative}")
    with path.open("rb") as file:
        for raw in file:
            try:
                value = strict_json_loads(raw)
            except StrictJsonError as exc:  # 已校验后仍失败属环境异常，fail-closed
                raise QueryTurnInputError(f"输入 M1B 记录无法解析：{relative}") from exc
            if not isinstance(value, dict):
                raise QueryTurnInputError(f"输入 M1B 记录不是对象：{relative}")
            yield value


def _require_str(value: Any, field: str, relative: str) -> str:
    if not isinstance(value, str):
        raise QueryTurnInputError(f"输入 M1B 记录字段类型非法：{relative}/{field}")
    return value


def _optional_str(value: Any, field: str, relative: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise QueryTurnInputError(f"输入 M1B 记录字段类型非法：{relative}/{field}")
    return value


def _require_ordinal(value: Any, field: str, relative: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise QueryTurnInputError(f"输入 M1B 记录字段类型非法：{relative}/{field}")
    return value


def _optional_ordinal(value: Any, field: str, relative: str) -> int | None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise QueryTurnInputError(f"输入 M1B 记录字段类型非法：{relative}/{field}")
    return value


def _require_bool(value: Any, field: str, relative: str) -> bool:
    if not isinstance(value, bool):
        raise QueryTurnInputError(f"输入 M1B 记录字段类型非法：{relative}/{field}")
    return value


def _require_str_tuple(value: Any, field: str, relative: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise QueryTurnInputError(f"输入 M1B 记录字段类型非法：{relative}/{field}")
    return tuple(_require_str(item, field, relative) for item in value)


def _content_scalars(
    event_kind: str, payload: Any, relative: str
) -> tuple[int | None, int | None, bool]:
    """从冻结契约 payload 抽 content 空/非空标量（规格 §1 勘误、§4 步 4）。

    仅 USER/ASSISTANT_MESSAGE 的 content 参与回合派生；其余 kind 取 ``(None, None, False)``。
    TEXT→(utf8_byte_length, None, False)；CONTENT_BLOCKS→(None, block_count, True)。
    """

    if event_kind not in (EventKind.USER, EventKind.ASSISTANT_MESSAGE):
        return None, None, False
    try:
        typed = read_event_payload(event_kind, payload)
    except EventPayloadReadError as exc:  # payload 不合冻结契约 → fail-closed，不回显值
        raise QueryTurnInputError(
            f"输入 M1B 事件 payload 不合冻结契约：{relative}/{exc.reason_code}"
        ) from exc
    content = typed.content  # UserPayload / AssistantMessagePayload 均有 content
    if isinstance(content, ContentBlocks):
        return None, content.block_count, True
    return content.utf8_byte_length, None, False


def load_m1b_turn_view(m1b_run_dir: str | Path) -> M1bTurnView:
    """校验并读取一个已发布 M1B run；任一核验不符即整批失败，不产半份视图。"""

    root = Path(m1b_run_dir)
    if not root.is_dir():
        raise QueryTurnInputError("输入 M1B run 不是可读目录")

    # 规格 §2.1「先校验再消费」：完整性与身份核验全权委托 M1B 权威 validator（含重算内容寻址
    # run_id、逐文件重哈希、inventory、source⇄artifact 交叉一致）。只上报错误码、不回显任何值。
    result = validate_compiled_run(root)
    if not result.ok:
        codes = ",".join(sorted({issue.code for issue in result.issues}))
        raise QueryTurnInputError(f"输入 M1B run 未通过独立完整性校验：{codes}")

    manifest_path = root / "artifact_manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = strict_json_loads(manifest_bytes)
    except (OSError, StrictJsonError) as exc:
        raise QueryTurnInputError("无法读取输入 M1B artifact_manifest.json") from exc
    if not isinstance(manifest, dict):
        raise QueryTurnInputError("输入 M1B artifact_manifest.json 顶层不是对象")

    m1b_run_id = manifest.get("run_id")
    source_schema = manifest.get("source_schema")
    if not isinstance(m1b_run_id, str) or not isinstance(source_schema, str):
        raise QueryTurnInputError("输入 M1B artifact_manifest.json 缺少 run_id/source_schema")
    # 内容寻址绑定：M1D 以已发布 run_id 作为 m1b_run_id（规格 §2.1），并断言其为目录名。
    if root.name != m1b_run_id:
        raise QueryTurnInputError("输入 M1B run 目录名与已发布 run_id 不一致")
    m1b_artifact_manifest_sha256 = sha256_bytes(manifest_bytes)

    # capture_quality：terminal_status 的权威来源（NormalizedCaptureV3 不含）。QUARANTINED 行
    # capture_occurrence_id=None（未产 NormalizedCaptureV3/事件），join 时天然跳过；已编译 capture
    # 的 processing_status 已由 validate_compiled_run 断言恒 COMPLETE，故不读取。
    quality_rel = "private/capture_quality.jsonl"
    terminal_status_by_capture: dict[str, str] = {}
    for record in _iter_published_jsonl(root, quality_rel):
        cid = _optional_str(record["capture_occurrence_id"], "capture_occurrence_id", quality_rel)
        if cid is None:
            continue
        terminal_status_by_capture[cid] = _require_str(
            record["terminal_status"], "terminal_status", quality_rel
        )

    # captures：M1D 处理的结构 capture 全集（NormalizedCaptureV3；has_compaction 在此）。
    captures_rel = "private/captures.jsonl"
    has_compaction_by_capture: dict[str, bool] = {}
    for record in _iter_published_jsonl(root, captures_rel):
        cid = _require_str(record["capture_occurrence_id"], "capture_occurrence_id", captures_rel)
        has_compaction_by_capture[cid] = _require_bool(
            record["has_compaction"], "has_compaction", captures_rel
        )

    # event_occurrences：按 scope 分流——观测窗口进 ObservedEvent，前缀仅按 kind 记账（诚实分母）。
    events_rel = "private/event_occurrences.jsonl"
    observed_by_capture: dict[str, list[ObservedEvent]] = defaultdict(list)
    prefix_counts_by_capture: dict[str, dict[str, int]] = defaultdict(dict)
    for record in _iter_published_jsonl(root, events_rel):
        cid = _require_str(record["capture_occurrence_id"], "capture_occurrence_id", events_rel)
        event_kind = _require_str(record["event_kind"], "event_kind", events_rel)
        scope = _require_str(record["event_scope"], "event_scope", events_rel)
        if scope == EventScope.PRE_FIRST_OBSERVED_TERMINAL:
            counts = prefix_counts_by_capture[cid]
            counts[event_kind] = counts.get(event_kind, 0) + 1
            continue
        if scope != EventScope.OBSERVED_REQUEST_WINDOW:  # 闭合枚举外的 scope → fail-closed
            raise QueryTurnInputError(f"输入 M1B 事件 scope 非法：{events_rel}/event_scope")
        text_len, block_count, has_blocks = _content_scalars(
            event_kind, record["payload"], events_rel
        )
        observed_by_capture[cid].append(
            ObservedEvent(
                event_id=_require_str(
                    record["event_occurrence_id"], "event_occurrence_id", events_rel
                ),
                event_kind=event_kind,
                sequence_number=_require_ordinal(
                    record["sequence_number"], "sequence_number", events_rel
                ),
                request_boundary_id=_optional_str(
                    record["request_boundary_id"], "request_boundary_id", events_rel
                ),
                message_index=_require_ordinal(
                    record["message_index"], "message_index", events_rel
                ),
                sub_index=_optional_ordinal(record["sub_index"], "sub_index", events_rel),
                text_utf8_byte_length=text_len,
                block_count=block_count,
                has_non_text_blocks=has_blocks,
            )
        )

    # action_batches：键=assistant 事件；builder 只按观测 assistant 查用，前缀批次天然不被引用。
    batches_rel = "private/action_batches.jsonl"
    batches_by_capture: dict[str, dict[str, ActionBatchInfo]] = defaultdict(dict)
    for record in _iter_published_jsonl(root, batches_rel):
        cid = _require_str(record["capture_occurrence_id"], "capture_occurrence_id", batches_rel)
        assistant_event_id = _require_str(
            record["assistant_event_id"], "assistant_event_id", batches_rel
        )
        batches_by_capture[cid][assistant_event_id] = ActionBatchInfo(
            action_batch_id=_require_str(record["action_batch_id"], "action_batch_id", batches_rel),
            assistant_event_id=assistant_event_id,
            tool_call_event_ids=_require_str_tuple(
                record["tool_call_event_ids"], "tool_call_event_ids", batches_rel
            ),
            execution_semantics=_require_str(
                record["execution_semantics"], "execution_semantics", batches_rel
            ),
        )

    # tool_pairings：只透传严格 1:1 的 matched 对（未匹配两者为 None，builder 据此判 orphan）。
    pairings_rel = "private/tool_pairings.jsonl"
    pairings_by_capture: dict[str, list[ToolPairingLink]] = defaultdict(list)
    for record in _iter_published_jsonl(root, pairings_rel):
        cid = _require_str(record["capture_occurrence_id"], "capture_occurrence_id", pairings_rel)
        pairings_by_capture[cid].append(
            ToolPairingLink(
                matched_call_event_id=_optional_str(
                    record["matched_call_event_id"], "matched_call_event_id", pairings_rel
                ),
                matched_result_event_id=_optional_str(
                    record["matched_result_event_id"], "matched_result_event_id", pairings_rel
                ),
            )
        )

    captures: list[CaptureInput] = []
    for cid in sorted(has_compaction_by_capture):
        terminal_status = terminal_status_by_capture.get(cid)
        if terminal_status is None:  # 编译 capture 必有匹配 quality 行；缺失 → fail-closed
            raise QueryTurnInputError(f"输入 M1B capture 无匹配 capture_quality 行：{quality_rel}")
        captures.append(
            CaptureInput(
                capture_occurrence_id=cid,
                terminal_status=terminal_status,
                has_compaction=has_compaction_by_capture[cid],
                observed_events=tuple(observed_by_capture.get(cid, ())),
                prefix_event_counts_by_kind=dict(prefix_counts_by_capture.get(cid, {})),
                action_batch_by_assistant=dict(batches_by_capture.get(cid, {})),
                tool_pairings=tuple(pairings_by_capture.get(cid, ())),
            )
        )

    return M1bTurnView(
        m1b_run_id=m1b_run_id,
        m1b_artifact_manifest_sha256=m1b_artifact_manifest_sha256,
        source_schema=source_schema,
        captures=tuple(captures),
    )
