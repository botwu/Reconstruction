# 参考仓库处理逻辑与采用边界

版本：v0.1

日期：2026-08-31

状态：基于当前本地快照完成只读审计；作为选择性重写依据

本文回答三个问题：

1. `../seed2traj/refer_repo` 中每个参考项目实际做了什么；
2. 它的输入前提与 TraceForge 有什么不同；
3. 哪些逻辑可以映射到 TraceForge，哪些不能照搬。

当前本地参考根目录是：

```text
/Users/wujian1/Downloads/seed2traj/refer_repo
```

该路径只用于开发期审阅，不是 TraceForge 的运行时依赖。参考仓库、论文、数据样本和运行指南均不得复制进 TraceForge Git 仓库。

## 1. 总体采用规则

### 1.1 采用状态

本文使用四种状态：

| 状态 | 含义 |
| --- | --- |
| `SELECTIVE_REWRITE` | 重写少量确定性逻辑，使用 TraceForge 自身契约和测试 |
| `PRINCIPLE_ONLY` | 只吸收方法不变量，不复制实现 |
| `POST_MVP` | 当前不实现，等前置模块闭合后再评估 |
| `BLOCKED` | 快照、许可证或输入前提不足，不能进入实施依据 |

不存在“直接 import 参考仓库”的采用方式。

### 1.2 外部实现进入 TraceForge 的条件

任何参考逻辑只有同时满足以下条件才可实施：

- 当前模块确实需要；
- 输入假设与 TraceForge 当前数据契约一致；
- 输出不会越权生成 Truth、难度或成功标签；
- 失败不被静默当成通过；
- 有正常、失败和边界测试；
- 记录来源仓库、提交、许可证、原文件和本地修改；
- 重新实现后不再依赖参考仓库路径。

若只是复用概念，应重新设计最小契约，不复制 Prompt、动态代码执行器和历史兼容层。

## 2. 快照与优先级

| 资源 | 当前快照 | 许可证/身份 | 采用状态 | 首要映射 |
| --- | --- | --- | --- | --- |
| AgentRx | `f228165bfec60a801fd5fedd9d8ffe0f9de0c69d` | MIT | `SELECTIVE_REWRITE` | trajectory、certification |
| TRACE 仓库 | `d2db23085409555b3f13ea426f42d62cf0bbc43d` | MIT | `POST_MVP` + 少量重写 | certification、calibration |
| ASTRA | `bdf6a46a130118e039aadcaeb46724d278383589` | Apache-2.0 | `PRINCIPLE_ONLY` | synthesis |
| EnvHarness | `fab7d57441f06b75c73a900e04561d4d7600f361` | Apache-2.0 | `POST_MVP` | calibration |
| 旧 seed2traj | `6880b7c7323d3235be4ff4a9022a91cbd18cbc20` | 本地未见明确 LICENSE | `PRINCIPLE_ONLY` | trajectory、synthesis、export |
| QC_postprocess | 未发现独立 Git 快照 | 本地未见明确 LICENSE | `PRINCIPLE_ONLY` | trajectory、profiling |
| cardgame golden | 固定 artifact 目录 | 本地未见明确 LICENSE | `PRINCIPLE_ONLY` | certification、export |
| `agent-world.pdf` | 48 页论文 | Agent-World 论文 | `PRINCIPLE_ONLY` | synthesis、calibration |
| `轨迹诊断.pdf` | 21 页论文 | Trace context attribution 论文 | `PRINCIPLE_ONLY` | profiling、repair |
| `13781_LLMs_Get_Lost_In_Multi_T.pdf` | ICLR 2026 论文 | Multi-turn reliability 论文 | `POST_MVP` | episode、context difficulty |
| 腾讯 AGS/Hermes/TokenHub 指南 | 2026-08-31 本地指南 | 已验证运行手册 | `PRINCIPLE_ONLY` | export/rollout 边界 |

同名提醒：

- `refer_repo/TRACE` 是 **Capability-Targeted Agentic Training**；
- `refer_repo/轨迹诊断.pdf` 是 **TRajectory Attribution for Automated Context Engineering**。

它们不是同一个项目，输入、目标和输出完全不同。

## 3. 按 TraceForge 阶段的参考路由

| TraceForge 阶段 | 优先阅读 | 只吸收什么 |
| --- | --- | --- |
| M1 Trajectory | QC data_filter、AgentRx IR、旧 seed2traj 证据提取 | 流式接入、来源指纹、事件事实、pairing、部分状态 |
| M2 Profiling | QC LLMChecker、AgentRx taxonomy、轨迹诊断论文 | evidence view、封闭标签、假设与事实分离 |
| M3 Synthesis | Agent-World、ASTRA、旧 seed2traj materialize | world-first、工具/证据图、确定性物化 |
| M4 Certification | TRACE self-test、AgentRx invariant、cardgame artifact | Witness 自测、显式状态、mutation、attestation |
| M5 Calibration | EnvHarness、TRACE calibration、Multi-turn 论文 | 目标区间、多 rollout、单算子 mutation、matched variant |
| M6 Export | cardgame artifact、腾讯运行指南 | hash manifest、attempt、artifact、rollout contract |

开发某一模块时只阅读该行相关文件，不能因为参考仓库存在就提前实现后续系统。

## 4. AgentRx：失败轨迹诊断器

位置：`refer_repo/AgentRx`

### 4.1 真实处理链

```text
Raw JSON / JSONL
→ domain-specific Trajectory IR
→ LLM 生成 Static Invariants
→ LLM 按轨迹前缀生成 Dynamic Invariants
→ Python/NL Checker
→ LLM Judge 定位 failure step 并强制十分类
→ 失败频率与人工标注准确率报告
```

关键文件：

- `run.py`：当前真实主链；
- `agentrx/ir/trajectory_ir.py`：多格式读取和极简 IR；
- `agentrx/invariants/domain_registry.py`：domain、policy、tool 和 converter 注册；
- `agentrx/invariants/static_invariant_generator.py`；
- `agentrx/invariants/dynamic_invariant_generator.py`；
- `agentrx/invariants/checker.py`；
- `agentrx/judge/judge.py`；
- `agentrx/reports/`。

### 4.2 输入前提

AgentRx 假设：

- 一条对象就是一条独立 trajectory；
- domain 已知为 Tau、Flash 或 Magentic；
- 外部已有 policy、tool schema、部分 Gold 或人工失败标注；
- 轨迹通常已被认为失败；
- 可以使用完整轨迹和 LLM 做事后归因。

它没有 capture lineage、公共前缀、QueryTurn、TaskEpisode、Public/Control 或可解性认证。

### 4.3 对 R01 的已验证失配

当前代码直接读取 R01 时会产生：

```json
{
  "raw_count": 1683,
  "ir_count": 1683,
  "empty_instruction": 1683,
  "one_step": 1683,
  "all_substeps_unknown": 1683
}
```

原因包括：

- 不识别 `capture_id/thread_id`；
- 默认 Flash converter 不识别 `role/content`；
- 不保存结构化 tool call；
- 全局退化检测错误地阻止 fallback；
- 42,318 条带 tool call 的 assistant message 中，大量空 content 会导致动作直接消失。

因此不能复用其 converter 或 IR。

### 4.4 值得重写的逻辑

采用状态：`SELECTIVE_REWRITE`。

1. 显式检查状态：

   ```text
   PASS / FAIL / NOT_APPLICABLE / ABSTAIN / ERROR
   ```

   禁止 skip/error 隐式当 pass。

2. Typed TriggerSelector：

   ```text
   event_kind / role / tool_name / phase / event_id / query_turn_id
   ```

3. 两层 invariant：

   ```text
   DomainInvariantSpec
       来自 DomainKit 和工具 contract

   CandidateRevisionInvariantSpec
       来自 TaskWorldCandidateRevision、Truth 和 EvidenceGraph
   ```

   两者都必须在 rollout 前冻结。

4. Violation 与 telemetry：保存 criterion、binding revision、event/evidence ref、状态、reason code、checker version 和成本。

5. 失败 taxonomy 子集：plan adherence、无依据编造、非法调用、tool output 误读、intent 偏移、意图不完整、能力不支持、权限阻断和 infra failure。

6. “最早未恢复问题”改写为确定性 issue lifecycle：

   ```text
   OPEN → RECOVERED
   OPEN → UNRESOLVED
   ```

### 4.5 禁止照搬

- Dynamic invariant 看过当前步骤后才生成，只能做事后假设，不能成为预冻结 Verifier；
- 模型生成 Python 后在主进程 `exec()`，缺少隔离和完整自测；
- checker 存在 fail-open；
- Judge prompt 强制十选一，不允许可靠 `NO_ERROR/ABSTAIN`；
- Judge 没有真正收到其声称的 Gold action；
- LLM root-cause 分类不能生成 Truth 或认证 World。

## 5. TRACE 仓库：能力定向训练

位置：`refer_repo/TRACE`

### 5.1 通用 Prompt 声明的处理链

```text
可靠 benchmark pass/fail
→ capability discovery
→ 10 个隔离 labeler 标 NA/PRESENT/LACKING
→ Cov / Δ / K-of-N 聚合
→ 选择高 contrast capability
→ Coding Agent 生成能力微环境
→ 自测与多 rollout 校准
→ 定向 mutation
→ capability-specific GRPO LoRA
→ gate 选择 adapter
```

关键文件：

- `prompts/general/capability_selection.md`；
- `pipeline/aggregate_capabilities.py`；
- `prompts/general/environment_generation.md`；
- `pipeline/calibrate_environment.py`；
- `train/collect_rollouts.py`；
- `train/train_grpo.py`。

`render_pipeline.py` 只渲染 Markdown Prompt，不是自动执行上述阶段的 pipeline。

### 5.2 实际 SWE 脚本链

```text
SWE-bench unresolved + non-empty model patch + Gold patch
→ 同一模型做 root cause 和聚类
→ 每个失败样本单标签
→ 直接选择最大簇
→ 专用脚本生成 Python module + pytest + oracle replacement
→ pre-fix fail / post-fix pass / regression self-test
→ 同一模型 N=10 smoke
→ 过易时修改代码表面
→ 重新 self-test 和 smoke
```

关键文件：

- `swebench/qwen3.6_self_trace/phase2_capability_mining.py`；
- `phase3_synth_semantic_logic_precision.py`；
- `phase3_synth_deep_call_graph_traversal.py`；
- `phase4_env_validation.py`。

实际脚本没有执行通用 Prompt 声称的 10-labeler `Cov/Δ` 链，而是依赖 Gold patch 选择最大失败簇。文档与实际实现必须分开理解。

### 5.3 最值得重写的 self-test

采用状态：M4 `SELECTIVE_REWRITE`，capability mining 为 `POST_MVP`。

```text
生成候选环境
→ pre-fix 要求目标测试失败
→ regression tests 保持通过
→ 应用明确 Oracle witness
→ post-fix 要求目标测试通过
→ regression tests 仍通过
→ 多 rollout 判断难度
→ mutation 后重新执行全部自测
```

映射到 TraceForge：

```text
World reset
→ Control Truth 可计算
→ ReferenceWitness 可执行
→ Verifier 接受 Reference
→ 典型错误 mutation 被拒绝
→ 合法替代路径被接受
```

### 5.4 Rollout calibration

`pipeline/calibrate_environment.py` 统计 mean/std reward、success/zero rate、constant group、informative group 和 Pass@k。它衡量训练信号，不是可解性。

TraceForge 只重写：

- 排除 `INFRA/INVALID/UNSCORABLE`；
- 按 binding 和 model manifest 分组；
- criterion pass rate；
- Pass@1/Pass@k；
- 组内 variance、失败 criterion、工具/token/成本。

### 5.5 禁止照搬

- R01 没有可信 pass/fail 或 Gold，不能运行其 capability contrast；
- 不能把 `reasoning_content` 原文作为语义输入；M1 只允许生成固定审计摘要；
- 微环境只有 3–5 个内存工具，不代表复杂业务 World；
- mutation 会一次混合多个变化，无法归因；
- `rate <= 0.5` 会接受 0% 成功任务；
- API/超时异常被混入模型失败；
- smoke 只检查 target test，可能忽略 regression；
- training/gate 发布代码存在缺文件、stub 和 README/argparse 不一致，不进入 MVP。

只有 M4/M5 产生认证成功/失败和 criterion evidence 后，才可设计 `CapabilityHypothesis`，并扩展为：

```text
NA / PRESENT / LACKING / ABSTAIN / INFRA
```

## 6. Agent-World 论文：环境—任务发现与自演化

位置：`refer_repo/agent-world.pdf`

### 6.1 处理链

```text
MCP specs + tool docs + industrial PRDs
→ environment theme taxonomy
→ Deep Research 从 Web 挖掘 topic database
→ 多轮 database complexification
→ Coding Agent 生成 DB-grounded tools + unit tests
→ compile/test 过滤
→ graph-based 或 programmatic task synthesis
→ sandbox execution 产生 answer/rubric/verifier
→ ReAct agent 5 次，至少 2 次成功后保留
→ 扩大路径、工具和逻辑复杂度
→ arena 评测、诊断弱环境、定向扩展和继续 RL
```

### 6.2 最值得吸收的思想

采用状态：`PRINCIPLE_ONLY`。

- 先有可执行 World 和工具，再反向生成最终任务；
- tool dependency graph 与 programmatic control flow 是两类不同任务结构；
- Reference execution 用真实 observation 约束任务表达；
- 每轮都生成 fresh verifiable tasks；
- 模型失败反馈只调整环境/任务生成方向；
- 环境、任务和模型能力可以形成闭环，但 verifier 必须先存在。

### 6.3 TraceForge 的深化

Agent-World 从主题出发，TraceForge 从真实 Episode 的需求、工作流和环境暴露出发：

```text
Observed Task/Environment constraints
→ TaskIntent
→ Fact-first World
→ independent SemanticTruth
→ ReferenceWitness
→ final Task render
→ Verifier
```

TraceForge 不直接采用“主题 → Web mining → database complexification”作为 MVP。未来若增加数据库挖掘，必须额外解决来源许可、内容摘要、时间冻结、冲突消解、PII、可复现性和独立 Truth。

### 6.4 禁止照搬

- topic-driven 生成不等于真实回流条件化；
- Web 数据丰富不等于 World 关系和 Truth 可靠；
- 完全连接工具图中的 independent edge 可能产生不真实链路；
- tool test accuracy `> 0.5` 对认证过弱；
- graph task 的 JSON GT/rubric 仍部分由同一 LLM 生成；
- agent 多次答案一致只说明经验 closure，不能定义 Truth；
- rubric LLM judge 不能成为事实正确性的唯一 Verifier；
- 人工合并 taxonomy 不符合当前“人工 review 非必要”的 MVP。

## 7. ASTRA：轨迹合成与 QA 驱动环境合成

位置：`refer_repo/astra`

### 7.1 两条真实流水线

轨迹合成：

```text
MCP Server/tool schema
→ 推断工具依赖图
→ 子链枚举或随机游走
→ 投票与反向翻译检查
→ 工具链生成 query
→ Agent 执行
→ 多维 LLM reward
→ SFT trajectory
```

环境合成：

```text
已知领域 QA/答案
→ 子问题 DAG
→ 工具必要性检查
→ 原子性、依赖和完备性验证
→ 生成工具文档、调用和 Python 实现
→ 沙盒执行
→ stdout 是否包含预期子答案
→ 合并相似工具并修改 mock data
```

关键文件：

- `env_synthesis/src/step_01_gen_QA_for_pipeline.py`；
- `step_02_check_tool_necessity.py`；
- `step_03_verify.py`；
- `step_04_env_synthesis.py`；
- `step_05_merge_tools.py`；
- `trajectory_synthesis/src/1_graph_build/`；
- `trajectory_synthesis/src/3_interaction/interact_qwen_agent.py`；
- `trajectory_synthesis/src/4_reward/reward.py`。

### 7.2 值得吸收

采用状态：`PRINCIPLE_ONLY`。

- 子任务依赖 DAG；
- 工具必要性和强制串行性检查；
- 工具图、子链和调用参数连接；
- 生成代码必须先沙盒执行；
- 相似工具合并需要同步更新 schema 和 mock data。

### 7.3 禁止照搬

- ASTRA 从已知 QA 和子答案开始，而 R01 没有答案；
- 修改 mock data 让工具返回预期答案容易形成答案服务环境；
- stdout 包含答案不证明语义正确；
- LLM 投票和反向翻译不构成 GT；
- 没有 reset、权限、时间、版本和跨资源一致性；
- 没有 Public/Control、provenance grade 或 binding digest；
- 多处异常给安全分，存在 fail-open。

TraceForge 只能在独立 Truth 已经存在后借鉴 DAG、工具必要性和代码自测。

## 8. EnvHarness：可信基础环境上的难度层

位置：`refer_repo/envharness`

### 8.1 处理链

```text
已有可信 ActionableEnv + task + evaluate()
→ K 次 baseline rollout
→ HarnessAgent 生成 Setup/Rules 代码
→ Setup 改初始状态
→ Rules 拦截 action/transition/observation
→ K 次 mutated rollout
→ ACCEPT / REFINE / REJECT
→ 搜索目标成功率区间
→ 输出候选代码、状态快照、轨迹和日志
```

`Link` 可以组合两个已有环境。三类 layer 都工作在 `reset/step/observe/evaluate` 标准接口上，并保留底层目标判定。

关键文件：

- `envharness/core/actionable_env.py`；
- `envharness/core/envharness.py`；
- `envharness/core/code_loader.py`；
- `envharness/harnesses/setup.py`；
- `envharness/harnesses/rules.py`；
- `envharness/harnesses/link.py`；
- `envharness/agents/harness_agent.py`；
- `envharness/orchestration/runner.py`；
- `envharness/orchestration/objectives.py`。

### 8.2 值得吸收

采用状态：`POST_MVP`。

- `Action/Observation/Step` 和环境 snapshot 契约；
- 难度层只改变初态、可见 observation 和 action surface；
- 保持底层 evaluator 不变；
- baseline → proposal → K rollout → accept/refine/reject；
- reset、checkpoint、cleanup 和子进程隔离；
- difficulty target band。

TraceForge 的 HardnessOperator 可以吸收 Setup/Rules 思想，但采用声明式 schema：

```text
precondition
mutation
preserved invariants
verifier extension
rollback
```

### 8.3 禁止照搬

- EnvHarness 假设 base world、state transition 和 verifier 已经可信，不能证明初始重建正确；
- 保留原 evaluator 不保证变异后证据仍可达；
- Rules 可能过滤关键 observation；
- HarnessAgent 解析异常存在默认接受的 fail-open 风险；
- 动态加载模型生成 Python 代码不进入 TraceForge 核心；
- Link 串联两个 episode，不等于构造一个共享业务 World；
- 难度搜索可能过拟合当前模型。

正确顺序是：

```text
先认证基础 TaskWorld
→ 应用一个 typed HardnessOperator
→ 重新运行 Truth/Reference/Verifier/Teacher gates
```

## 9. 轨迹诊断论文：上下文故障归因与修复

位置：`refer_repo/轨迹诊断.pdf`

### 9.1 处理链

```text
完整 execution trace + 用户 correction/DSAT signal
→ Detector 识别显式/隐式不满意
→ 计算“期望与实际”的 delta
→ 将 trace 逆序提供给 Root Cause Agent
→ 归因到 system/tool prompt/KB/KG/skill context source
→ Recommender 主动读取被归因的真实 context source
→ 交叉验证 stale/gap/retrieval 等假设
→ 生成 CREATE/UPDATE/DELETE/NO_ACTION 建议
→ Human Review
```

### 9.2 值得吸收

采用状态：`PRINCIPLE_ONLY`。

- trajectory 可以建模为 context influence graph；
- 用户纠正是候选 delta，不是自动 Truth；
- root-cause attribution 必须当作待验证假设；
- repair 前必须主动读取权威来源并交叉核验；
- attribution 与 recommendation 分离；
- context component 使用显式 CRUD/NO_ACTION，而不是自由重写整个系统。

这映射到：

```text
Observed failure signal
→ RepairHypothesis
→ 独立检查 World/Task/Tool/Verifier 事实
→ RepairDiagnosis
→ typed repair
```

### 9.3 禁止照搬

- 论文依赖用户 correction 或 DSAT，R01 并不总有；
- 依赖完整轨迹和可访问的 system prompt、KB、KG、skill 文件；
- 主要归因方法使用 chain-of-thought，TraceForge 禁止把 `reasoning_content` 原文用于归因；
- 其评测是带已知 fault ground truth 的合成数据；
- 最终仍需要 human review；
- 逆序单次 LLM 调用只能生成根因候选，不能证明因果。

因此它用于 Repair Router 的方法约束，不进入 Truth 或自动认证主链。

## 10. 旧 seed2traj：回流到训练轨迹的历史管线

位置：`refer_repo/seed2traj`

### 10.1 处理链

```text
回流 JSONL
→ 规则画像与粗筛
→ LLM 任务分段
→ 提取 user/tool/path/read/edit evidence
→ 同一 LLM 生成 TaskSpec、workspace、hidden goal 和 rubric
→ 隐私替换、路径检查、workspace materialize
→ 确定性文件噪声
→ 强模型 reference rollout
→ LLM judge
→ 从成功轨迹反推 state invariant 和 required reads
→ direct/underspecified/correction variants
→ SFT/DPO/discard export
```

关键位置：

- `src/seed2traj/profiling/`；
- `src/seed2traj/restore/segment.py`；
- `extract_evidence.py`；
- `generate_taskspec.py`；
- `noise.py`；
- `reconcile.py`；
- `derive_verifier.py`；
- `privacy.py`；
- `taskspec/materialize.py`；
- `src/executor/rollout/`；
- `src/seed2traj/verify/`；
- `src/seed2traj/export/`。

当前工作区另有通用 Search 审计资产：

```text
/Users/wujian1/Downloads/seed2traj/src/seed2traj/search_audit
/Users/wujian1/Downloads/seed2traj/scripts/run_search_audit.py
```

### 10.2 值得吸收

采用状态：`PRINCIPLE_ONLY`，个别纯函数可按新契约重写。

- compaction、path、tool 和 evidence surface 提取；
- 字节来源、hash manifest 和幂等输出；
- deterministic workspace materialization；
- privacy/redaction；
- noise manifest；
- infra 与业务失败分离；
- success/failure/discard 分池；
- 执行层通过文件契约解耦。

### 10.3 禁止照搬

- 每条 JSONL 独立处理，没有 CaptureGraph；
- 每个 user message 直接切 task 会破坏复合交付；
- 同一模型同时生成任务、环境、hidden goal 和 rubric，形成循环 Truth；
- 强模型走通一次不证明任务唯一可解；
- 从成功 rollout 反推 Verifier 会固化模型路径；
- 文件噪声不能表达权限、政策、服务状态和实体关系；
- 默认制造 missing-info 会改变真实任务；
- 旧代码部分消费 `reasoning_content`；
- 人工 review、旧 badcase verdict 和 TaskSpec v7 不进入 TraceForge 核心。

本地未发现明确许可证，禁止直接复制代码。

## 11. QC_postprocess：质量过滤和多轮检查

位置：`refer_repo/QC_postprocess`

该目录包含两个不同项目。

### 11.1 data_filter

```text
本地/远端 JSONL/CSV
→ schema/format check
→ 轮次、长度、重复和黑名单规则
→ exact/prefix dedup
→ PII redaction
→ 可选 LLM 分类/评分
→ stage accepted/rejected + metrics + resume state
```

关键文件：

- `data_filter/src/data_filter/input/`；
- `core/view_builder.py`；
- `core/stage_io.py`；
- `core/pii.py`；
- `stages/prefix.py`；
- `stages/redaction.py`；
- `stages/quality.py`；
- `scoring/trajectory.py`。

值得吸收：流式处理、message fingerprint、typed content pointer、阶段 attrition、reject reason 和 resume。

必须修改：prefix 关系不能删除短 capture，而应保留两个 CaptureNode 并建立候选 lineage edge；scalar difficulty 只能成为低置信度 observed estimate。

### 11.2 LLMChecker

```text
轨迹格式转换
→ 为不同检查轮生成不同 evidence projection
→ 机械、放弃、文本质量、难度、grounding、subagent、多模态检查
→ 条件路由
→ SQLite 保存 record × round
→ clean/dirty/failed + report
```

关键位置：

- `LLMChecker/llmchecker/formats.py`；
- `trajectory.py`；
- `rounds/`；
- `routing.py`；
- `store.py`；
- `pipeline.py`。

值得吸收：不同目标使用最小 evidence view、结构检查先于 LLM、逐轮状态、条件路由、单轮失败隔离和断点续跑。

禁止照搬：有损 view 不能成为 canonical IR；单条记录模型不解决 lineage；tool pairing 不够严格；会使用 reasoning；clean/dirty 不是重建资格或可解性。

该目录未发现明确 Git 快照和 LICENSE，只能参考逻辑。

## 12. cardgame golden：认证 artifact 样例

位置：`refer_repo/cardgame-sutures-of-the-sleepless`

该目录不是环境合成框架，而是一条固定 GameCraft 任务的完整证据包。

### 12.1 记录链

```text
固定 Godot task + rubric
→ 模型构建
→ visual self-check attempt 1 失败
→ recovery
→ attempt 2 失败
→ recovery
→ attempt 3 通过
→ fresh launch 重放 10 demos
→ 保存 MP4/frame/process log
→ criterion judge
→ trajectory/workspace/rubric/verifier artifact hash
```

关键 artifact：

- `raw/trajectory.json`；
- `raw/workspace/validation/manifest.json`；
- `checker/purified/`；
- `external_replay/attestation.json`；
- `external_replay/run/verify_out/`；
- `SHA256SUMS`。

### 12.2 值得吸收

采用状态：`PRINCIPLE_ONLY`。

- failed attempt 不删除；
- recovery 与最终通过保持同一 lineage；
- raw/clean/replay 分层；
- content-addressed evidence；
- criterion breakdown；
- external attestation；
- accepted、reward 和 threshold 分开保存。

### 12.3 禁止照搬

- 它只有单一固定任务，不能估计分布；
- 没有 synthesis 实现；
- verifier 源码不在目录中；
- 视觉 LLM judge 不是确定性 Truth；
- `accepted=true` 只表示 replay/evidence 被接受，不等于 reward 达标；
- 当前样例 reward 约 `0.207`，threshold 也未达到；
- 本地未见明确 LICENSE。

它用于定义 `CertificationRevision` 和 `ProvenanceManifest` 的产物形态，不提供 World 生成逻辑。

## 13. Multi-turn 论文：复合 Query 与上下文难度

位置：`refer_repo/13781_LLMs_Get_Lost_In_Multi_T.pdf`

论文把同一个完整 instruction 拆成逐轮透露的 shards，并比较：

```text
FULL：单轮完整约束
CONCAT：同内容拼接成单轮
SHARDED：跨多轮逐步透露
```

主要启发：多轮下降不只是信息丢失，而包含早期错误假设、过早给最终答案、依赖此前错误答案和中间约束遗失。

采用状态：`POST_MVP`。

可用于：

- 证明 QueryTurn 不能机械拆成独立 task；
- `REFINES/CORRECTS/DEPENDS_ON` Episode edge；
- `constraint_sharding` HardnessOperator；
- FULL/CONCAT/SHARDED matched variant；
- 区分 aptitude 与 multi-turn unreliability。

不能把 LLM 用户模拟器当作真实用户分布，也不能让该算子在 M1/M2 阻塞基本轨迹结构化。

## 14. 腾讯 AGS、Hermes 与 TokenHub 指南

位置：`refer_repo/腾讯AGS沙盒中Hermes与TokenHub使用指南_20260831.md`

该文件是已验证运行底座，不是任务或 World 合成方法。

```text
Host 创建腾讯 AGS sandbox
→ node-python-hermes template
→ 注入 TokenHub endpoint/model/secret
→ Hermes 在 sandbox 内执行 tool loop
→ 导出 conversation/session/workspace/artifact
→ 校验 tool call/result、泄漏和资源销毁
```

TraceForge 使用边界：

- 保留为 rollout infrastructure 的 known-good smoke；
- `RunnableTaskWorldCandidateBundle` 输出给可选 rollout integration；
- 下游回传 `RolloutEvidence`；
- Harbor、AGS 生命周期、Hermes 安装和 TokenHub 配置不进入 TraceForge 核心。

禁止复制密钥、机器路径和临时 runner。直接 runner 可以作为下游兼容性基准，不成为 TraceForge batch backend。

## 15. 当前缺失的历史参考

此前讨论过 `AgentHER` 和 `CSO`，但当前 `refer_repo` 快照中不存在对应目录；`GameCraft-Bench` 也不在本地参考根目录。

处理原则：

- 不根据聊天记忆写实现结论；
- 不把缺失仓库列为当前代码依据；
- 恢复具体路径、提交和 LICENSE 后单独审计；
- 审计完成前状态统一为 `BLOCKED`。

GameCraft/Harbor 只作为仓内可选 rollout integration 的参考，TraceForge core 只保留 Bundle、RolloutEvidence 与 EvaluationEvidence 边界。

## 16. 明确禁止的跨仓库拼接

以下组合看起来能形成完整系统，但会制造循环监督或错误主张：

1. 用 AgentRx/轨迹诊断的 LLM 根因标签直接决定 GT；
2. 用 ASTRA 把模型推测的答案写进 mock tool，再称任务可解；
3. 用旧 seed2traj 的同一模型同时生成 Task、World、hidden goal 和 rubric；
4. 用 TRACE 的失败簇处理没有可信 pass/fail 的原始 R01；
5. 在未认证 base world 上直接运行 EnvHarness 式环境演化；
6. 用强模型多数票替代 SemanticTruth；
7. 把 QC clean/dirty 或 GameCraft accepted 当作任务成功；
8. 把原轨迹调用过的工具解释为正确或必需工具；
9. 把 EnvironmentExposureProfile 写成用户真实环境分布；
10. 将 infra failure 计入模型难度或 capability lacking。

## 17. TraceForge 的最终组合方式

参考项目只在对应阶段提供局部约束：

```text
QC / AgentRx / search_audit
→ 结构化事实与诊断候选

Agent-World / ASTRA
→ World-first、图结构和自测思想

TraceForge 自身
→ independent Truth
→ TaskWorldCandidateRevision
→ G0–G7 certification

TRACE / EnvHarness / Multi-turn
→ 已认证 release 上的难度搜索

cardgame / 腾讯运行指南
→ provenance、attempt、rollout 与 artifact 边界
```

唯一权威实现始终位于 TraceForge。参考仓库不能成为绕过契约、认证或测试的捷径。
