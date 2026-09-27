# 原始会话重建流程

本页说明调用关系与阶段职责。运行结论和未解决问题统一维护在 [当前状态](current-status.md)，模块文件见 [阅读地图](rebuild-live-map.md)。

R04/R05 按条重建使用 `reconstruct raw-run`，不读取筛选结论。已有筛选记录使用 `reconstruct run --records`，两者共享后续主链；原始 intake 不会被改写为筛选 ELIGIBLE。

## 输入与批处理

`scripts/prepare_session_batch.py` 调用 `session_inventory.prepare_session_batch`，按物理 JSONL 行创建冻结输入和 `sessions.jsonl`，核对 distribution 的记录数、字节数和 SHA256。现有 R04/R05 清单共 8229 个 session（R04 6535、R05 1694），证据为 `artifacts/r04-r05-all-sessions-v1/source_manifest.json`。冻结清单通过只说明输入覆盖完整。

`scripts/run_session_batch.py` 顺序处理冻结清单。每条需要最终 `reconstruction_manifest.json`；只有阶段收据而没有最终清单时记录 PROCESS_ERROR，不把局部结果当成完成。

## 单条处理

```text
原始 JSONL 行
→ Session Task Agent → RAW_SESSION source
→ Intent → Replay / Route
→ Completion 候选 → Sufficiency
→ Environment Contract / TaskFit / 可选变体
→ 执行门禁 → Verifier / RED
→ Harbor bundle → 可选真实 Hermes rollout → 完整验收
```

### Session Task Agent 与 Intent

`raw_session.build_raw_session_source` 将 user span 分为 task 或 context，并校验覆盖、重叠和用户消息引用。失败保留分段收据；成功只证明任务边界协议完整。

`intent_recovery.run_intent_recovery` 恢复原始目标、验收义务和环境绑定，每项义务引用真实用户消息。初始必要路径与最终输出路径必须区分：用户要求新增的文件不应被当成必须预先存在的输入。Intent 产物是拟合任务，仍需审查路径与义务是否符合原意。

### Replay 与路由

`env_replay.replay_from_timeline` 恢复首次可信文件内容，区分完整文件、片段、未知修改和 withheld changes；不执行原始 shell。当前任务使用完整 session 工具时间线，提供跨 turn 上下文，但不等价于每个任务起点的独立快照。

Intent 后重新计算 `execution_support_route`：有回放文件走 TERMINAL_FILE；无回放文件而有 FILE 义务可以走 DEFAULT_EMPTY。没有受支持的文件验收时保留 REVIEW，而不生成虚假的文件验收结论。

### Completion

`complete_from_replayed` 或 `complete_from_default_empty` 生成 task-start 环境候选，补全相关上下文与依赖，记录事实来源和不确定性。不得提前解题或把参考答案、隐藏验证测试交给 agent。

候选通过结构、引用、写入边界和泄漏检查后物化 workspace。Completion READY 不代表依赖可用、源码正确或任务可解。当前是一次生成后逐个候选判断，没有基于后续结果自动返回 Completion 修复的跨阶段循环。

### Sufficiency、环境合同与 TaskFit

`workspace_sufficiency.run_workspace_sufficiency` 检查绑定路径、workspace hash 与源码完整性，并在只读沙盒中评估任务所需上下文。执行探针通过 `run_environment_probe` 产生真实收据，核对退出码、超时、输入快照与 reset 重复执行；临时写入使用探针 scratch。

上下文充分性与执行证据分开：`environment_contract.status/context_status` 表示上下文；`execution_readiness` 表示探针状态。三种探针收据满足要求时可标为 PROBED，不是任意任务可解性的证明。探针能力必须与任务相关，只读审查不自动要求编译项目。

TaskFit 衡量环境能否支持完成和验证原任务；目标功能未实现属于正常 task-start 状态。只有明确且可复现的任务冲突才允许从补全环境与原任务共同生成变体；仅有执行故障或缺少上下文不应冒充任务冲突。变体保留来源和变更义务，再交给后续验证；当前主编排没有通用的变体后重新运行 Sufficiency 循环。

### Verifier、RED 与 rollout

有 FILE 验收义务才调用现有文件 Verifier。真实 rollout 请求还会在 Verifier 之前检查环境执行收据；不满足时写 `verification/execution_gate.json` 并停止。RED-only 路径与此不同。

Verifier 生成隐藏 pytest、oracle 和 mutation；RED 要求初始缺失能力检查失败、保护性检查通过、oracle 通过、mutation 失败。Verifier 内有有限轮反馈修复。校准成功可发布 Harbor bundle，`verification.status=READY` 不证明真实 agent 已解题。

真实 Hermes rollout 在 task-start 环境执行，验收读取 trial、reward、质量门禁、轨迹、输入绑定与 cleanup。只有这些结果和义务覆盖都完整才可能关闭认证。显式请求的诊断 rollout 允许 NON_FILE 响应义务暂未验证，但未覆盖的 FILE 义务仍会前置阻断。rollout 后的 response receipt 只证明响应格式和来源绑定；结论、数量、证据等内容义务未验时仍为 REVIEW，不能宣称最终响应验收完整。

## 产物边界

- 重建：用户任务、task-start workspace、环境声明、verifier 与参考校准材料。
- rollout：真实 agent 执行及最终输出，保留 `agent/trajectory.full.json`。
- 验收：RED、trial、reward、response receipt、manifest 和 cleanup 的关联证据。

Harbor 计划在 `verification/plans/`，实际 job 在 `verification/jobs/`，交付包在 `verification/deliverables/hermes-replay/harbor_bundle/`。完整格式与核查顺序见 [当前状态](current-status.md)。日志和历史诊断不是交付资格证明，缺少实际产物不得补写成功标志。
