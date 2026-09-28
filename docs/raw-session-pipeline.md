# 原始会话重建流程

本页说明截至 2026-09-28 的调用关系与阶段职责，源码版本以当前 Git 提交为准。运行结论和未解决问题统一维护在 [当前状态](current-status.md)，模块文件见 [阅读地图](rebuild-live-map.md)。

R04/R05 按条重建使用 `reconstruct raw-run`，不读取筛选结论。已有筛选记录使用 `reconstruct run --records`，两者共享后续主链；原始 intake 不会被改写为筛选 ELIGIBLE。

## 输入与批处理

`scripts/prepare_session_batch.py` 调用 `session_inventory.prepare_session_batch`，按物理 JSONL 行创建冻结输入和 `sessions.jsonl`，核对 distribution 的记录数、字节数和 SHA256。现有 R04/R05 清单共 8229 个 session（R04 6535、R05 1694），证据为 `artifacts/r04-r05-all-sessions-v1/source_manifest.json`。冻结清单通过只说明输入覆盖完整。

`scripts/run_session_batch.py` 顺序处理冻结清单。每条需要最终 `reconstruction_manifest.json`；只有阶段收据而没有最终清单时记录 PROCESS_ERROR，不把局部结果当成完成。

## 单条处理

```text
原始 JSONL 行
→ Session Task Agent → RAW_SESSION source
→ Replay / 初始路由 → Intent → 重算路由
→ Completion 候选 ↔ Sufficiency（最多追加两轮定向修复）
→ Environment Contract / TaskFit / 可选变体
→ Verifier 生成与独立语义审查 / RED
→ 冻结 Harbor bundle（task、workspace、verifier）

单独读取冻结 harbor_bundle/task
→ prepare-rollout → execute-rollout → read-results
→ 真实 solver trajectory 与评分（独立最终响应验收尚待接通）
```

### Session Task Agent

`raw_session.build_raw_session_source` 将 user span 分为 task 或 context，并校验覆盖、重叠和用户消息引用。失败保留分段收据；成功只证明任务边界协议完整。

### Replay 与初始路由

`env_replay.replay_from_timeline` 在 Intent 前恢复首次可信文件内容，区分完整文件、片段、未知修改和 withheld changes；不执行原始 shell。read 输出的 hash 行号包装被解包，显式 raw 内容保持原样，缺失文件错误不作为源码。当前任务使用完整 session 工具时间线，提供跨 turn 上下文，但不等价于每个任务起点的独立快照。Replay 后先计算初始支持路由，供后续 Intent 使用。

可核验的源码片段另存为公开的 `.traceforge/source-excerpts.json` 和带行号片段文件，供解题者引用实际观测行；索引不补造未观察正文。明确的初态读取缺失记录为 ABSENT，修改或未知执行屏障后的读取不能倒灌为初态证据。

### Intent 与重算路由

`intent_recovery.run_intent_recovery` 恢复原始目标、验收义务和环境绑定，每项义务引用真实用户消息。初始必要路径与最终输出路径必须区分：用户要求新增的文件不应被当成必须预先存在的输入。提取时区分真实用户要求、示例与说明文字，并依据回放根及用户绝对路径统一 workspace 坐标；公开任务与响应验收使用同一映射。原始用户文本、指定输出格式及结构化响应合同保留来源。Intent 产物仍需通过实际环境和验收核对是否符合原意。

Intent 后重新计算 `execution_support_route`：有回放文件走 TERMINAL_FILE；无回放文件而有 FILE 义务可以走 DEFAULT_EMPTY。没有受支持的文件验收时保留 REVIEW，而不生成虚假的文件验收结论。

v33 的实际失败发生在 Intent 输出解析，尚未进入 Completion。`fd41fdc` 已修复损坏根 JSON 被内层对象冒充的问题，并允许原角色最多一次格式纠正；相关回归通过不等于真实重跑通过。

### Completion

`complete_from_replayed` 或 `complete_from_default_empty` 生成 task-start 环境候选，补全相关上下文与依赖，记录事实来源和不确定性。不得提前解题或把参考答案、隐藏验证测试交给 agent。

首次补全和后续修复都只接收允许公开的原始证据：隐藏写入、修改屏障后的事件、匿名或重复事件编号不能进入可读取证据索引。Completion 沙盒上传后修正工作区目录所有权，使普通用户能写入子目录；完整原始正文仍由工具和候选校验共同保护，写入失败保留退出码和错误详情。

必需初始输入被原始读取明确证实不存在时，首次与修复 Completion 都直接 REVIEW 并说明路径，不调用模型。可选的已知缺失路径也不能被工具写入或最终 JSON 补造；屏障前后续可信正文仍可按原规则恢复。

候选通过结构、引用、写入边界和泄漏检查后物化 workspace。工具调用缺少证据编号时拒绝该次写入，允许 agent 补正参数；不得因此允许无证据写入。Completion READY 不代表依赖可用、源码正确或任务可解。

编排层对有效候选运行 Sufficiency，最多追加两轮反馈：具体上下文缺口或真实探针失败返回 Completion 增量修复；仅缺探针收据时只重评。每轮独立物化、复核和记录，无进展、基础设施故障、模型拒绝或轮次耗尽均保留真实原因。原 Replay 的完整文件与部分片段保护不变，不通过修改任务或预解任务使环境过关。见 [环境反馈闭环](environment-repair.md)。

运行声明省略时继承，显式新数组替换旧值；它们目前没有统一自动安装机制。缺包仍须由真实探针揭示，不能把声明写入 manifest 当作已完成安装。

### Sufficiency、环境合同与 TaskFit

`workspace_sufficiency.run_workspace_sufficiency` 检查绑定路径、workspace hash 与源码完整性，并在只读沙盒中评估任务所需上下文。执行探针通过 `run_environment_probe` 产生真实收据，核对退出码、超时、输入快照与 reset 重复执行；临时写入使用探针 scratch。

上下文充分性与执行证据分开：`environment_contract.status/context_status` 表示上下文；`execution_readiness` 表示探针状态。三种探针收据满足要求时可标为 PROBED，不是任意任务可解性的证明。探针能力必须与任务相关，只读审查不自动要求编译项目。

TaskFit 衡量环境能否支持完成和验证原任务；目标功能未实现属于正常 task-start 状态。只有明确且可复现的任务冲突才允许从补全环境与原任务共同生成变体；仅有执行故障或缺少上下文不应冒充任务冲突。变体保留来源和变更义务，再交给后续验证；当前主编排没有通用的变体后重新运行 Sufficiency 循环。

### 重建验收：Verifier 与 RED

有 FILE 验收义务才调用现有文件 Verifier。真实 rollout 请求还会在 Verifier 之前检查环境执行收据；不满足时写 `verification/execution_gate.json` 并停止。RED-only 路径与此不同。

Verifier 生成隐藏 pytest、oracle 和 mutation。独立审查会读取实际 workspace，检查错误结果能否蒙混过关、合理结果是否被额外要求拒绝，以及响应格式检查是否被误当成语义义务覆盖；具体反例返回现有的有限修复轮。RED 要求初始缺失能力检查失败、保护性检查通过、oracle 通过、mutation 失败。独立审查与 RED 数值均不单独证明任务语义正确。校准成功可发布 Harbor bundle，`verification.status=READY` 不证明真实 agent 已解题。

### 独立 solver rollout 与尚未闭合的验收

重建负责 task、workspace、verifier 与 RED；RED 是验证器校准，不是 solver rollout。独立重建可以仅启用 `--execute-red`，不带 `--execute-rollout`，先产出独立 Harbor bundle；这不是已有运行通过的结论。

已有独立 `prepare-rollout → execute-rollout → read-results` 路径，可以读取冻结的 `harbor_bundle/task`，在沙盒中执行真实 solver 并产生 trajectory 与评分。但独立结果读取尚未接入最终 response contract 验收，相关逻辑仍在 `reconstruction.verification` 的私有函数；状态分离与验收规则持久化仍在收尾。以下描述的是现有重建内联 rollout 的验收行为，不能据此认定独立路径已经闭合。

真实 Hermes rollout 在 task-start 环境执行，验收读取 trial、reward、质量门禁、轨迹、输入绑定与 cleanup。待验证的最终响应不再一概阻止采集真实轨迹；它们仍留在未验证集合，不能提前获得交付资格。执行完成后，从真实最终 assistant 消息生成绑定收据，按来自用户要求的显式响应合同校验字段、数组元素、实际报告路径及支持的摘要一致性。只绑定 JSON 不等于合同通过，不支持的语义或结构继续未验证。

当前修复闭环覆盖环境充分性与 Verifier/RED；真实 rollout 位于校准循环之外。最终响应 receipt 拒收会返回 REVIEW 并保存失败证据，不会自动重新解题或修改验收规则。

公开 instruction 保留用户规定的验收格式，不向其追加未声明的旧报告字段；路径转换保持非路径转义语义。只有真实执行、义务覆盖和各项验收证据完整才可能关闭认证。新代码的实际通过情况以 [当前状态](current-status.md) 所列运行记录为准，不能由流程描述推断已经端到端通过。

## 产物边界

- 重建：用户任务、task-start workspace、环境声明、verifier、参考校准材料及 RED 结果，交付独立 Harbor bundle。
- solver rollout：读取冻结 bundle，真实执行并保留 `agent/trajectory.full.json`、最终输出与评分。
- 最终验收：关联 bundle、trial、reward、response receipt、manifest 和 cleanup；独立结果读取的响应合同验收仍待接通。

Harbor 计划在 `verification/plans/`，实际 job 在 `verification/jobs/`，交付包在 `verification/deliverables/hermes-replay/harbor_bundle/`。完整格式与核查顺序见 [当前状态](current-status.md)。日志和历史诊断不是交付资格证明，缺少实际产物不得补写成功标志。
