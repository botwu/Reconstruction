"""M1D 结构型 QueryTurn 编译（[`docs/m1d-processing-spec.md`]）。

把已发布 M1B run 的**可观测事件流**确定性折叠成 `AgentStep`／`UserBlock`／
`AssistantOutcome`／`QueryTurn` 与结构性 `ThreadTurnGraph`，并对不可定位前缀与孤儿观测
做显式记账。零模型调用；语义关系与 `TaskEpisode` 全部留 M2。

对外只导出编排入口（镜像 `trajectory` 只导出 `compile_trajectory`、`lineage` 只导出
`build_lineage`）。
"""

from __future__ import annotations

from traceforge.query_turns.pipeline import build_query_turns

__all__ = ["build_query_turns"]
