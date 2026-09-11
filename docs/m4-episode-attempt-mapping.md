# M4 映射边界

M1D 目前只提供结构性的 `QueryTurn`、`AgentStep` 和 `UserBlock`，不提供语义任务边界。因此 M4 使用 `mapping_reader.load_mapping_index` 将每个 `QueryTurn` 映射为一个候选 `EpisodeRefV1`，将该回合的 `agent_step_ids` 映射为 `AttemptRefV1`。

该映射有三个约束：

- `session_ref` 来源于真实 `capture_occurrence_id`，只表示观测 session，不把 capture 当作 episode。
- `episode_ref` 是由 `(m1d_run_id, query_turn_id)` 内容寻址生成的稳定 ID。
- `attempt_ref` 是由 `(m1d_run_id, query_turn_id, agent_step_id, step_ordinal)` 内容寻址生成的稳定 ID。

所有记录的 `semantic_status` 当前为 `STRUCTURAL_ONLY`。这意味着它们可以被 Failure Analysis 用作真实外键，但还不能声称已经完成语义任务切分。后续 TaskEpisode Segmenter 应在独立产物中合并连续 QueryTurn，并生成新的语义版本 ID；不得覆写本索引。

当回合没有 AgentStep 时，读取器生成一个 `TURN_FALLBACK` attempt，仅用于保留未完成/空回合的引用完整性。该 fallback 不能被当作真实执行步骤。

当前 `failure_analysis.pipeline.build_failure_analysis` 仍是 M1B-only 确定性分析，尚未把该映射接入主流水线。接入前必须将 M1D run 作为显式输入，并在 manifest 中绑定 M1D 身份；任何用 `capture_occurrence_id` 同时填充 episode/attempt 的旧逻辑均应删除。
