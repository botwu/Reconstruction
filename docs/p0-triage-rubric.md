# P0：轨迹接入、TaskEpisode 切分与重建候选筛选

## 目标

P0 不只是读取 JSONL，而是从真实 session 中筛出适合进入下游重建的 TaskEpisode/失败 attempt。输出必须能解释为什么入选、为什么拒绝，以及后续应该使用哪一种 environment backend。

## 总体流程

R01 sessions.jsonl
  → 原始结构扫描与 source adapter
  → M1 TraceForge 规范化
  → Session → TaskEpisode → TargetAttempt
  → 规则硬门槛
  → 模型 triage + 独立 critic
  → rubric 评分与分流
  → SelectionManifest
  → Task Recovery / Initial State Replay / Environment Completion

任何 source quarantine 的记录不能进入模型重建。

## 重建单位

原始 session 不是重建单位。使用：

Session → TaskEpisode → Attempts → TargetFailedAttempt

TaskEpisode 合并同一任务的用户澄清和连续执行，排除无关话题。TargetAttempt 是被选中的失败或未完成执行。必须记录 episode 边界、target attempt 边界、reconstruction cutoff、后续用户反馈是否允许作为任务澄清，以及被排除的其他 span。

## P0-A：原始结构扫描

确定性代码只输出元数据和计数：

- JSONL 是否完整；
- top-level schema；
- messages/tools/meta 是否存在；
- representation/protocol/adapter；
- message/tool 数量；
- 是否有错误、超时、未完成标记；
- 文件签名和 source hash；
- domain/rubric metadata。

不读取、不执行历史命令。R01 当前缺少 canonical domain_meta，需要 source adapter 将 task_domain_session_meta 映射为 canonical metadata，同时保留原始扩展字段。

## P0-B：候选特征

规则特征：

- parse_ok
- has_user_task_like_turn
- has_agent_attempt
- has_tool_activity
- has_failure_or_unfinished_signal
- has_file_evidence
- has_recoverable_cutoff
- has_verifier_candidate
- unsupported_action_count
- privacy_or_integrity_issue
- estimated_cost
- domain_route：code_file / retrieval / other

硬拒绝：

- source schema 不可解析；
- 没有用户任务意图；
- 没有 Agent attempt；
- 只有闲聊或纯信息回答；
- source integrity/privacy 失败；
- 关键事件全部位于 unknown action barrier 之后；
- 当前阶段不允许的不可逆外部操作；
- 无法确定 TaskEpisode 边界且没有人工复核。

硬入选候选至少满足：

- 有用户任务型 turn；
- 有 Agent attempt；
- 有失败、未完成、用户纠正或无交付物信号之一；
- 能定位 target attempt 或失败边界；
- 至少存在一种可分析证据：文件、工具结果、artifact、错误或用户反馈。

## 模型 triage

T1 Episode Segmenter：输出 episode spans、target attempt、same-task clarification、unrelated spans、segmentation confidence 和 source references。

T2 Failure Classifier：输出 primary failure、critical events、causal hypotheses、failure boundary 和 recoverability。

T3 Reconstructability Judge：输出 task clarity、initial-state evidence、environment completion opportunity、verifierability 和 recommended backend。

T4 独立 Critic：只审查 T1–T3 的 evidence binding、冲突和过度推断，不重新生成 task，不看 teacher solution。

所有输出必须是结构化 JSON，每个结论绑定 event/turn id。模型不能覆盖规则硬拒绝。

## Rubric

每个维度 0–3 分。

R1 任务可识别性：0 无任务；1 只能猜测；2 目标基本明确；3 目标、交付物和关键约束都有用户证据。

R2 失败/未完成证据：0 无失败；1 弱推断；2 有错误、终止或纠正；3 失败边界和表现均可定位。

R3 初始环境可见性：0 无环境证据；1 零散字符串或最终状态；2 部分初始文件/输入/依赖；3 首次修改前有完整文件或环境快照。

R4 环境补全价值：0 补全等于猜题；1 只能补外围；2 可补少量上下文；3 明显暴露缺失依赖、配置、输入或文件结构。

R5 Verifier 可构造性：0 无可验证交付物；1 只能主观 judge；2 部分 deterministic checks；3 用户要求可转为独立测试/invariant。

R6 TaskEpisode 边界置信度：0 无法分开；1 高度不确定；2 大致可分；3 同一任务和无关话题边界清晰。

R7 安全和隐私可处理性：0 无法安全处理；1 需人工脱敏；2 可在远端隔离环境处理；3 风险和数据边界清晰。

R8 预计成本：0 超长/不可控；1 需要大量人工/沙盒；2 成本可接受；3 适合作为 pilot。

## 分流规则

eligible_code_file：R1、R2、R3、R5、R7 均至少 2，且 domain_route=code_file。

eligible_retrieval：R1、R2、R5、R7 均至少 2，且 domain_route=retrieval。

needs_manual_review：任意关键维度为 1，或 T1/T2/T3 与 critic 冲突。

deferred：任务有价值，但 source、环境或依赖暂时无法闭合。

rejected：硬拒绝条件成立，或 R1/R2/R5 中任意一个为 0。

P0 不使用 teacher rollout reward 作为选择依据，避免选择器偏向容易或被环境作弊的任务。

## SelectionManifest

SelectionManifest 至少包含：

source_ref、episode_span、target_attempt、route、rule_features、model_assessments、rubric、eligibility、reason_codes、evidence_refs、input_hash、prompt_versions、model_provenance。

下游只接受 eligible。manual_review 进入人工处理；deferred 等 adapter/backend 完成后重评。

## P0 评估

抽样人工标注至少 100 条，评估 episode segmentation F1、bad attempt precision/recall、eligible candidate precision、backend route accuracy、rubric 一致性、evidence attribution、误纳入率、误拒绝率和成本。

第一条 pilot 先取 1 个 eligible_code_file，之后扩展到 20–50 条。R01 retrieval 不应在 code/file adapter 和 verifier 稳定前混入主实验。
