# 完整 session 重建输入

reconstruct prepare 以 M1B 的 candidate_group_id 作为 session 身份，将同组全部
normalized capture 聚合为一个 evidence.json。--capture-id 仅用于选择失败分析报告对应的
目标 capture，不再选择某个 USER 回合；request boundary 和 capture 元数据保留在
provenance 中，供审计与任务边界解释。

共享前缀事件按 (sequence_number, event_kind, payload) 稳定去重；每个证据行的
source_occurrence_ids 保留所有原始 occurrence。分叉事件不会因同名工具或相同正文被配对
覆盖。工具调用与结果沿原始 tool_pairings.jsonl 传递，未观测结果继续标记
pending_tool_call_ids 和 REVIEW。

语义恢复的证据投影完整保留事件、工具参数、工具结果和正文，不按回合或字符预算静默裁剪。
若模型端点无法容纳请求，调用层必须返回显式阻断状态；不得将截断后的证据当成完整 session。


## 模块契约与验收

- `trajectory.evidence_join.build_session_input` 是唯一生产入口：输入 M1B 的 `events.jsonl`、`captures.jsonl`、边界和 pairing，输出 `task-reconstruction-input.v2`。
- `reconstruction.prepare` 只消费完整 session；旧的 `build_query_task_input` 保留为兼容 API，但不被 CLI 或 workflow 调用，`query_ordinal` 不再参与切分。
- `semantic_recovery` 和 `environment_completion` 都保留完整 evidence。投影只做字段结构化，不做 token、字符或事件预算截断。
- `environment_completion` 的候选仍必须经过 `_materialize` 的路径、hash、来源引用和受保护文件校验；缺少上下文或模型上下文溢出时返回 REVIEW/FAILED，不能降级为部分证据继续生成。
- 完整 session 的回归测试位于 `tests/test_full_session_input.py`；远程环境的 Python 虚拟环境损坏时，可将 `src/` 与 `tests/` 打包到本地 Python 3.12 环境执行：
  `PYTHONPATH=src python -m pytest -q tests`。
