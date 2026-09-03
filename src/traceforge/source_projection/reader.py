"""读取并先行校验已发布的 M1B run（与可选 M1D run），产出 builder 消费的只读视图（规格 §2.1/§4）。

职责边界（[`AGENTS.md`] §4，镜像 `query_turns/reader.py`）：只做 IO 与**已发布字段**抽取，不做任何
分类（分类全在 `builder.py` 经 `contracts.classify_leading_text`）。「先校验再消费」全权委托上游权威
validator：M1B 用 `validate_compiled_run`，M1D 用 `validate_query_turn_run`（后者内部再次核验同一
M1B run 并断言二者绑定一致）。任一核验不符即整批失败、不产半份视图。上游 ID 一律当不透明外键，
不复刻也不 import 上游私有派生公式。

开头判定窗口由 typed reader 的 ``ContentPayload`` 经 `contracts.content_form_and_head` 与
`leading_head_window` 在此预抽取：builder 因此完全不触碰 payload，且内存里只保留每条 USER 事件
至多 256 个码点的开头。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from traceforge.query_turns.validation import validate_query_turn_run
from traceforge.source_projection.contracts import (
    UserTextProjectionInputError,
    content_form_and_head,
    leading_head_window,
)
from traceforge.source_projection.view import M1bUserTextView, M1dBlockView, UserEventInput
from traceforge.trajectory.contracts import EventKind
from traceforge.trajectory.event_payload import (
    EventPayloadReadError,
    TextContent,
    UserPayload,
    read_event_payload,
)
from traceforge.trajectory.json_codec import StrictJsonError, sha256_bytes, strict_json_loads
from traceforge.trajectory.validation import validate_compiled_run

_M1B_CAPTURES = "private/captures.jsonl"
_M1B_EVENTS = "private/event_occurrences.jsonl"
_M1D_USER_BLOCKS = "private/user_blocks.jsonl"


def _iter_published_jsonl(root: Path, relative: str) -> Iterator[dict[str, Any]]:
    """逐行读取已校验的 private 表；上游 validator 已确认其 canonical 与 schema。"""

    path = root / relative
    if not path.is_file():
        raise UserTextProjectionInputError(f"输入 run 缺少必需表：{relative}")
    with path.open("rb") as file:
        for raw in file:
            try:
                value = strict_json_loads(raw)
            except StrictJsonError as exc:  # 已校验后仍失败属环境异常，fail-closed
                raise UserTextProjectionInputError(f"输入记录无法解析：{relative}") from exc
            if not isinstance(value, dict):
                raise UserTextProjectionInputError(f"输入记录不是对象：{relative}")
            yield value


def _require_str(value: Any, field: str, relative: str) -> str:
    if not isinstance(value, str):
        raise UserTextProjectionInputError(f"输入记录字段类型非法：{relative}/{field}")
    return value


def _optional_str(value: Any, field: str, relative: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise UserTextProjectionInputError(f"输入记录字段类型非法：{relative}/{field}")
    return value


def _require_ordinal(value: Any, field: str, relative: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise UserTextProjectionInputError(f"输入记录字段类型非法：{relative}/{field}")
    return value


def _read_manifest_identity(root: Path, run_id_field: str) -> tuple[str, str, dict[str, Any]]:
    """读 artifact_manifest.json → ``(run_id, manifest_sha256, manifest)``；断言目录名即 run_id。"""

    manifest_path = root / "artifact_manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = strict_json_loads(manifest_bytes)
    except (OSError, StrictJsonError) as exc:
        raise UserTextProjectionInputError("无法读取输入 run 的 artifact_manifest.json") from exc
    if not isinstance(manifest, dict):
        raise UserTextProjectionInputError("输入 run 的 artifact_manifest.json 顶层不是对象")
    run_id = manifest.get(run_id_field)
    if not isinstance(run_id, str):
        raise UserTextProjectionInputError(f"输入 run 的 artifact_manifest 缺少 {run_id_field}")
    if root.name != run_id:  # 内容寻址绑定：目录名必须就是已发布 run_id（规格 §4）
        raise UserTextProjectionInputError("输入 run 目录名与已发布 run_id 不一致")
    return run_id, sha256_bytes(manifest_bytes), manifest


def load_m1b_user_text_view(m1b_run_dir: str | Path) -> M1bUserTextView:
    """校验并读取一个已发布 M1B run 的全部 USER 事件；任一核验不符即整批失败。"""

    root = Path(m1b_run_dir)
    if not root.is_dir():
        raise UserTextProjectionInputError("输入 M1B run 不是可读目录")

    result = validate_compiled_run(root)
    if not result.ok:
        codes = ",".join(sorted({issue.code for issue in result.issues}))
        raise UserTextProjectionInputError(f"输入 M1B run 未通过独立完整性校验：{codes}")

    m1b_run_id, manifest_sha256, manifest = _read_manifest_identity(root, "run_id")
    source_schema = manifest.get("source_schema")
    if not isinstance(source_schema, str):
        raise UserTextProjectionInputError("输入 M1B artifact_manifest.json 缺少 source_schema")

    capture_ids = frozenset(
        _require_str(record["capture_occurrence_id"], "capture_occurrence_id", _M1B_CAPTURES)
        for record in _iter_published_jsonl(root, _M1B_CAPTURES)
    )

    user_events: list[UserEventInput] = []
    for record in _iter_published_jsonl(root, _M1B_EVENTS):
        if record["event_kind"] != EventKind.USER:
            continue
        capture_id = _require_str(
            record["capture_occurrence_id"], "capture_occurrence_id", _M1B_EVENTS
        )
        if capture_id not in capture_ids:  # 事件必属已编译 capture；违约即上游损坏
            raise UserTextProjectionInputError("输入 M1B USER 事件引用了不存在的 capture")
        try:
            payload = read_event_payload(EventKind.USER, record["payload"])
        except EventPayloadReadError as exc:  # 不回显值，只报错误码
            raise UserTextProjectionInputError(
                f"输入 M1B USER 事件 payload 不合冻结契约：{exc.reason_code}"
            ) from exc
        if not isinstance(payload, UserPayload):  # read_event_payload 按 kind 分派，防御性断言
            raise UserTextProjectionInputError("输入 M1B USER 事件 payload 类型非法")
        content = payload.content
        content_form, head = content_form_and_head(content)
        utf8_byte_length = content.utf8_byte_length if isinstance(content, TextContent) else None
        user_events.append(
            UserEventInput(
                event_id=_require_str(
                    record["event_occurrence_id"], "event_occurrence_id", _M1B_EVENTS
                ),
                capture_occurrence_id=capture_id,
                sequence_number=_require_ordinal(
                    record["sequence_number"], "sequence_number", _M1B_EVENTS
                ),
                event_scope=_require_str(record["event_scope"], "event_scope", _M1B_EVENTS),
                request_boundary_id=_optional_str(
                    record["request_boundary_id"], "request_boundary_id", _M1B_EVENTS
                ),
                content_form=content_form.value,
                head_window=None if head is None else leading_head_window(head),
                utf8_byte_length=utf8_byte_length,
            )
        )

    return M1bUserTextView(
        m1b_run_id=m1b_run_id,
        m1b_artifact_manifest_sha256=manifest_sha256,
        source_schema=source_schema,
        capture_occurrence_ids=capture_ids,
        user_events=tuple(user_events),
    )


def load_m1d_block_view(m1d_run_dir: str | Path, m1b_run_dir: str | Path) -> M1dBlockView:
    """校验并读取一个已发布 M1D run 的 UserBlock 回指索引；M1D 须与所给 M1B run 绑定一致。"""

    root = Path(m1d_run_dir)
    if not root.is_dir():
        raise UserTextProjectionInputError("输入 M1D run 不是可读目录")

    result = validate_query_turn_run(root, m1b_run_dir)
    if not result.ok:
        codes = ",".join(sorted({issue.code for issue in result.issues}))
        raise UserTextProjectionInputError(f"输入 M1D run 未通过独立完整性/绑定校验：{codes}")

    m1d_run_id, manifest_sha256, _manifest = _read_manifest_identity(root, "query_turn_run_id")

    user_block_id_by_event_id: dict[str, str] = {}
    capture_id_by_user_block_id: dict[str, str] = {}
    for record in _iter_published_jsonl(root, _M1D_USER_BLOCKS):
        block_id = _require_str(record["user_block_id"], "user_block_id", _M1D_USER_BLOCKS)
        if block_id in capture_id_by_user_block_id:  # M1D validator 已断言主键唯一，防御性
            raise UserTextProjectionInputError("输入 M1D UserBlock 主键重复")
        capture_id_by_user_block_id[block_id] = _require_str(
            record["capture_occurrence_id"], "capture_occurrence_id", _M1D_USER_BLOCKS
        )
        event_ids = record["event_ids"]
        if not isinstance(event_ids, list):
            raise UserTextProjectionInputError(
                f"输入记录字段类型非法：{_M1D_USER_BLOCKS}/event_ids"
            )
        for event_id in event_ids:
            event_id = _require_str(event_id, "event_ids", _M1D_USER_BLOCKS)
            if event_id in user_block_id_by_event_id:  # M1D validator 已断言划分不交，防御性
                raise UserTextProjectionInputError("输入 M1D UserBlock 重复覆盖同一 USER 事件")
            user_block_id_by_event_id[event_id] = block_id

    return M1dBlockView(
        m1d_run_id=m1d_run_id,
        m1d_artifact_manifest_sha256=manifest_sha256,
        user_block_id_by_event_id=user_block_id_by_event_id,
        capture_id_by_user_block_id=capture_id_by_user_block_id,
    )
