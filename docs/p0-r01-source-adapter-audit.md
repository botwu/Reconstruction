# P0 R01 source adapter audit

日期：2026-09-11

## 观察

- 输入：R01 opus-4.8 sessions.jsonl。
- 只读统计：103 条 JSONL 记录，文件正文未导出。
- 真实记录顶层字段包含：messages、meta、record_id、rubric_partition、task_domain_annotations、task_domain_session_meta、tools、turn_quality_meta。
- meta.representation = restored_long，meta.adapter = claude_relay_replay，meta.protocol = anthropic_messages。
- 现有 TraceForge source adapter 的 restored-long envelope 要求顶层字段：messages、meta、tools、domain_meta。
- 用 source-schema=traceforge.restored-long-capture.v1 编译时，103 条记录均进入 quarantine，normalized_capture/event/tool pairing 数为 0。失败不是模型重建问题，而是 source envelope 字段契约未对齐。

## 决策候选

新增 R01 专用 source adapter 或 adapter wrapper，不修改 restored-long canonical contract：

1. 将 task_domain_session_meta 映射到 canonical domain_meta；
2. 原始 R01 元数据完整保留在 adapter extension/reference 中，不把 domain annotations 当用户任务文本；
3. 保留 record_id、source hash、lineage metadata 和 trailing/unassigned span metadata；
4. 不执行历史 tool action；
5. 先用脱敏结构 fixture 做 adapter 单测，再对远端真实全集运行 schema/计数验证；
6. adapter 通过后，才进入 QueryTurn/TurnEvidence/TaskEpisode。

## P0 验收

- 103/103 records parsed；
- quarantine 原因均可归类；
- normalized capture 数、event 数、pairing 数和 source record 数守恒；
- adapter 不新增消息正文、不改写用户文本；
- 单条候选 seed 可由 source record id 和 line ordinal 定位；
- 输入文件 signature 在读取期间不变化。

## 当前状态

已修改 TraceForge 代码完成 R01 adapter 和 M4 CLI 接入；尚未调用 teacher 或启动 AGS。
## 已完成验证（2026-09-11）

- 输入记录：103/103 JSONL 记录解析成功。
- 规范化 capture：102；明确隔离 1 条，原因是 MESSAGE_CONTENT_INVALID（第 25 行消息 content 既不是字符串也不是 content block 数组）。
- 事件：26,681；工具调用/结果配对：8,238；action batch：7,310。
- validate_compiled_run：通过；source schema 为 traceforge.r01-sessions.v1。
- 在干净 provenance 的 M1B 产物上运行 M4 确定性 Failure Analysis：102 条报告、无结构 FAIL，说明后续必须由语义模型分析任务/环境/solver 归因，不能把 M1B 结构完整性当作失败标签。
