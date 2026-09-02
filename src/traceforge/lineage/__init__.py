"""M1C 跨 capture 关系图（RequestLineageForest + CaptureRelationGraph）。

对外只暴露编排入口 `build_lineage`，镜像 `trajectory` 只导出 `compile_trajectory` 的收口方式。
"""

from __future__ import annotations

from traceforge.lineage.pipeline import build_lineage

__all__ = ["build_lineage"]
