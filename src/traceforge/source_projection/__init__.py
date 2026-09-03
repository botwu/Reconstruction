"""M2 前置 `UserTextProjection`（[`docs/m2-source-projection-spec.md`]）。

把已发布 M1B run 的**全部 USER 事件**按结构（开头标签的冻结白名单）注解为普通用户文本 /
Harness 注入 / 控制信号等类别，并以显式 ``locality`` 暴露不可定位前缀；可选绑定 M1D run 以回指
UserBlock。零模型调用、零语义推断；产物不含正文。

对外只导出编排入口（镜像 `query_turns` 只导出 `build_query_turns`）。
"""

from __future__ import annotations

from traceforge.source_projection.pipeline import build_user_text_projection

__all__ = ["build_user_text_projection"]
