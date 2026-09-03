"""`UserTextProjection` 的纯派生 fold：USER 事件视图 → 注解行 + 报告计数（规格 §2.2–§2.4）。

无 IO、无随机、无模型调用：同一 ``M1bUserTextView``（与可选 ``M1dBlockView``）恒产出逐字节相同的
注解序列（按 ``(capture_occurrence_id, sequence_number, event_id)`` 排序）。pipeline 与独立
validator 的篡改检测层调用**同一个** `build_user_text_projection_graph`（规格 §3 第一层）；分类
只经 `contracts.classify_leading_text`，本模块不复刻文法。

M1D 绑定语义（规格 §2.1/§3）：绑定即**每个** ``OBSERVED`` USER 事件都必须能回指到一个 UserBlock，
否则属上游契约违约、整批失败；``PREFIX_UNLOCALIZED`` 事件的 ``user_block_id`` 恒空。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from traceforge.source_projection.contracts import (
    USER_TEXT_ANNOTATION_SCHEMA,
    ContentForm,
    Locality,
    TextClass,
    UserTextAnnotationV1,
    UserTextProjectionInputError,
    build_report_counts,
    classify_leading_text,
    locality_for_scope,
)
from traceforge.source_projection.view import M1bUserTextView, M1dBlockView, UserEventInput


@dataclass(frozen=True, slots=True)
class UserTextProjectionGraph:
    """fold 的完整输出：注解行 + 由同批注解聚合的报告计数。"""

    annotations: tuple[UserTextAnnotationV1, ...]
    report_counts: dict[str, int]


def _annotate(event: UserEventInput, block_view: M1dBlockView | None) -> UserTextAnnotationV1:
    locality = locality_for_scope(event.event_scope)
    if locality is Locality.OBSERVED and event.request_boundary_id is None:
        raise UserTextProjectionInputError("输入 M1B 观测窗口 USER 事件缺少 request_boundary_id")
    if locality is Locality.PREFIX_UNLOCALIZED and event.request_boundary_id is not None:
        raise UserTextProjectionInputError("输入 M1B 前缀 USER 事件不应携带 request_boundary_id")

    user_block_id: str | None = None
    if block_view is not None and locality is Locality.OBSERVED:
        user_block_id = block_view.user_block_id_by_event_id.get(event.event_id)
        if user_block_id is None:
            raise UserTextProjectionInputError(
                "绑定 M1D 时观测窗口 USER 事件必须能回指到 UserBlock，但存在无归属事件"
            )

    text_class, leading_tag = classify_leading_text(event.head_window)
    return UserTextAnnotationV1(
        schema_version=USER_TEXT_ANNOTATION_SCHEMA,
        event_occurrence_id=event.event_id,
        capture_occurrence_id=event.capture_occurrence_id,
        locality=locality.value,
        request_boundary_id=event.request_boundary_id,
        user_block_id=user_block_id,
        content_form=event.content_form,
        text_class=text_class.value,
        leading_tag=leading_tag,
        utf8_byte_length=event.utf8_byte_length,
    )


def build_user_text_projection_graph(
    *, view: M1bUserTextView, block_view: M1dBlockView | None
) -> UserTextProjectionGraph:
    """对视图内全部 USER 事件逐条注解，并按规格 §2.4 门④ 聚合三个 capture 级分母。"""

    ordered = sorted(
        view.user_events,
        key=lambda event: (event.capture_occurrence_id, event.sequence_number, event.event_id),
    )
    annotations = tuple(_annotate(event, block_view) for event in ordered)

    content_form_counts: Counter[ContentForm] = Counter()
    class_counts: Counter[tuple[Locality, TextClass]] = Counter()
    plain_localities: defaultdict[str, set[Locality]] = defaultdict(set)
    for annotation in annotations:
        locality = Locality(annotation.locality)
        text_class = TextClass(annotation.text_class)
        content_form_counts[ContentForm(annotation.content_form)] += 1
        class_counts[(locality, text_class)] += 1
        if text_class is TextClass.PLAIN_USER_TEXT:
            plain_localities[annotation.capture_occurrence_id].add(locality)

    return UserTextProjectionGraph(
        annotations=annotations,
        report_counts=build_report_counts(
            capture_count=len(view.capture_occurrence_ids),
            user_event_count=len(annotations),
            content_form_counts=content_form_counts,
            class_counts=class_counts,
            captures_with_plain_user_text=len(plain_localities),
            captures_with_plain_user_text_only_in_prefix=sum(
                1
                for localities in plain_localities.values()
                if localities == {Locality.PREFIX_UNLOCALIZED}
            ),
            captures_with_observed_plain_user_text=sum(
                1 for localities in plain_localities.values() if Locality.OBSERVED in localities
            ),
        ),
    )
