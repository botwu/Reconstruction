"""`UserTextProjection` builder 的冻结输入投影（reader 产出 / builder 消费的边界契约）。

纯 dataclass、无 IO。把已发布 M1B run 的全部 USER 事件抽成 builder 需要的最小结构：来源外键、
可定位性所需的 scope/boundary、``content_form`` 与**开头判定窗口**（规格 §2.3：去左侧空白后的前
256 个码点；``None`` 表示没有可判定的开头文本）。窗口只存在于内存、只供分类，绝不落盘。
M1D 绑定时另附 "USER 事件 → UserBlock" 的回指索引。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserEventInput:
    """一条 USER 事件的最小投影；``sequence_number`` 只用于确定性排序，不落盘。"""

    event_id: str
    capture_occurrence_id: str
    sequence_number: int
    event_scope: str
    request_boundary_id: str | None
    content_form: str
    head_window: str | None
    utf8_byte_length: int | None


@dataclass(frozen=True, slots=True)
class M1bUserTextView:
    """reader 产出的整 run 只读投影（内容寻址身份 + 全部 USER 事件 + capture 全集）。"""

    m1b_run_id: str
    m1b_artifact_manifest_sha256: str
    source_schema: str
    capture_occurrence_ids: frozenset[str]
    user_events: tuple[UserEventInput, ...]


@dataclass(frozen=True, slots=True)
class M1dBlockView:
    """可选 M1D 绑定：已校验 M1D run 的身份 + ``event_id → user_block_id`` 回指索引。

    ``capture_id_by_user_block_id`` 是每个 UserBlock 自报的 capture（规格 §3 要求回指时与注解的
    capture 一致），其值集合即"有 UserBlock 的 capture"（跨模块不变量的右侧）。
    """

    m1d_run_id: str
    m1d_artifact_manifest_sha256: str
    user_block_id_by_event_id: dict[str, str]
    capture_id_by_user_block_id: dict[str, str]

    @property
    def capture_ids_with_user_blocks(self) -> frozenset[str]:
        return frozenset(self.capture_id_by_user_block_id.values())
