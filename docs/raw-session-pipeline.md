# 当前原始会话重建主链

本页只说明当前代码的真实调用关系。reconstruct run 仍用于已有 screening record 的兼容路径；R04/R05 当前实验使用 reconstruct raw-run，不读取筛选结论，也不把 intake 伪装成 ELIGIBLE。

## 输入与批处理

scripts/prepare_session_batch.py 调用 session_inventory.prepare_session_batch，只读扫描上游 R04/R05，按物理 JSONL 行建立冻结副本和 sessions.jsonl。它校验 distribution 中的记录数、字节数和 SHA256；校验不通过就不发布可批处理清单。当前清单位于 artifacts/r04-r05-all-sessions-v1/：

- R04 6535 行，R05 1694 行，共 8229 行；
- 无非法 JSON 行、无重复物理行，字节数和 SHA256 与 distribution 一致；
- source_manifest.json 为 READY 且 coverage_complete=true；
- 冻结输入在 return_data/four_batch/frozen_r04_r05/，不会覆盖旧的 by-rubric。

scripts/run_session_batch.py 固定单进程顺序执行。每行只允许出现一个最终 reconstruction_manifest.json；只有分段收据而没有最终重建清单时，批处理记为 PROCESS_ERROR，不会把分段成功冒充为端到端完成。

## 单条 raw-run

调用关系是：

raw JSONL 行 → Session Task Agent → RAW_SESSION source → Intent → Replay/Route → Completion → Sufficiency → Environment Contract/TaskFit → Verifier/RED → Hermes Rollout

### 1. Session Task Agent

raw_session.build_raw_session_source 先用 build_spans 建立 user span，再让只读的 SESSION_TASK_ROLE 将每个 span 精确分到一个 task 或 context。门禁要求：

- label_status=COMPLETE；
- 所有 span 恰好覆盖一次，无未知 span、重叠或遗漏；
- evidence_refs.message_indices 必须是任务内真实 user message 的子集；
- task 的 message_indices 保留该任务所有 user message，避免把续写/约束丢掉。

失败写入 session_task_segmentation.json 并返回 SESSION_TASK_REVIEW 或 INPUT_INVALID。成功只表示边界收据完整，不表示任务可重建。

### 2. Intent

intent_recovery.run_intent_recovery 只恢复用户原始目标、验收义务和 environment_bindings。每条义务必须有真实 user:<message_index> 证据。raw 路径只使用 intake_selected=true，不会写 reconstruction_eligible 或 screening decision。

Intent 通过后才会生成可执行 q；Intent 失败只保留审计契约，不进入 Completion。

### 3. Replay 与支持路由

env_replay.replay_from_timeline 按工具时间线恢复首次可信文件内容，区分完整文件、部分证据、未知 mutation 和 withheld changes；它不执行原始 shell，也不伪造未观察文件。

当前编排保留完整 session tool timeline（session_timeline_scope=FULL_SESSION），因此每个 task 的 replay 是 session 级初始证据，不是用户在该 task 时刻的独立快照。这保证跨 turn 证据不被截断，但后续 task 依赖前 task 产物时可能包含不适合作为独立起点的状态，属于当前已知限制。

execution_support_route 在 Intent 后重新计算：有文件回放走 TERMINAL_FILE；没有可观测文件但有 FILE 义务走 DEFAULT_EMPTY；只有 NON_FILE 义务或 retrieval 没有可验证文件时提前 REVIEW，不进入假的文件 Verifier。

### 4. Completion

complete_from_replayed 或 complete_from_default_empty 只补全缺失上下文、部分文件和依赖，禁止实现用户目标、写 solution 或写隐藏测试。候选通过结构、证据引用、受保护文件和泄漏门禁并物化 workspace 后，候选自身可标为 READY。

这个 READY 只表示候选产物符合 Completion 契约，不表示程序能加载、依赖齐全或任务可解。候选可以因模型决策、证据不足或沙盒初始化失败停在 REVIEW；后续候选会继续检查。

### 5. Sufficiency、环境和 TaskFit

workspace_sufficiency.run_workspace_sufficiency 先做 workspace hash、绑定路径和完整性诊断，再在只读沙盒中要求模型检查源码并产生 load/reset/dependency 探针。现在探针以真实 task workspace 作为 cwd，临时写入只能进入 TRACEFORGE_PROBE_SCRATCH；环境契约还会核对 workspace 前后快照、退出码、超时和 reset 执行次数。

- 确认缺失必要资产、绑定路径或必要路径受未知 mutation 影响：SKIPPED_UNRECONSTRUCTABLE；
- 模型/AGS/网络故障：INFRA_ERROR；
- 收据、完整性分类或契约错误：PIPELINE_ERROR；
- 证据不足、UNKNOWN 或 INSUFFICIENT：REVIEW；
- 无这些问题且探针完整：环境契约才可能 READY。

TaskFit 衡量补全环境是否支持未来 agent 实现并验证原任务；目标功能尚未实现是正常的初态，不应误判为冲突。只有经过探针且有可复现 task_conflict 证据的真实环境冲突，才允许生成环境约束变体。缺失资产不能生成变体；变体必须保留核心意图并重新走后续验证。

### 6. Verifier、RED 与 Rollout

只有存在明确 FILE 验收义务才进入 verification.py。Verifier 在初始 workspace 上生成隐藏 pytest、oracle 和 mutation；RED 校准必须同时满足：missing-capability 测试失败、protective 测试通过、oracle 全部通过、mutation 全部失败。校准成功时 verification.status=READY、calibration=PASS，但 rollout 默认仍是 NOT_RUN。

真实 Hermes rollout 还要求 execution 完成、quality gate 通过、trial 数量准确、每次 trial PASS/reward=1、cleanup 和轨迹证据完整，并且没有未验证义务，才会设置 sft_eligible=true 和 certification_closed=true。Rollout 失败不会把已经完成的 RED 校准伪装成成功。

Harbor/AGS 的计划、执行和读取是三步：计划目录在 verification/plans/，实际 job 在 verification/jobs/，Hermes 交付 bundle 在 verification/deliverables/hermes-replay/harbor_bundle/。真实轨迹应在 job trial 下的 agent/trajectory.full.json，并与 artifacts manifest、reconstruction certification 和 cleanup ledger 一起由 read_rollout_results 校验。没有这些真实文件，不能声称已有 rollout 轨迹交付。

## 当前证据

- 离线全量回归：通过；
- R04/R05 原始输入清单：8229 行已完成覆盖校验；
- raw 单条真实烟测：已证明分段、Intent、Replay 和 Completion 可以产生真实中间产物；
- 当前没有通过 Verifier/RED 的真实 raw-run，也没有真实 Hermes trajectory.full.json 或 harbor_bundle。因此当前项目处于“重建主链可执行、最终交付闭环仍未完成”，不能把 Completion 或环境 READY 当成端到端完成。
