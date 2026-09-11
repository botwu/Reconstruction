# Agent World Reconstruction：多模型协作项目计划

## 0. 项目目标

从真实用户的 Agent 线上执行轨迹中，重建一个可以在新沙盒中执行、独立验证、并产生高质量 SFT 轨迹的 Agent World。

输入可能是失败、未完成、工具报错、环境不完整且没有显式 reward 的历史轨迹。输出不是摘要，而是：TaskHypothesis、EnvironmentHypothesis、VerifierSpec、HarborTaskBundle、RolloutRecord 和 SFTExample。

研究假设是：失败轨迹中包含足够的任务意图和环境线索，但这些线索必须经过证据绑定、冲突处理和环境闭包，才能转化为可训练数据。

## 1. 总体原则

### 1.1 数据平面和模型平面分离

确定性代码负责原始日志读取、hash、去重和隐私边界；tool call/result 配对；文件状态 replay；schema 校验、路径安全和 artifact manifest；Harbor bundle 编译；verifier 执行、reward 和数据划分。

模型负责用户目标恢复、失败原因解释、缺失环境假设、依赖/服务候选、verifier 草案和证据冲突的语言层审查。模型不能绕过代码质量门，也不能直接修改原始数据、control truth 或 verifier 结果。

### 1.2 观察、推断、执行必须分开

每个字段必须标记 observed、derived、inferred 或 unknown。completion 产生的内容默认是 inferred，除非能绑定到 replay 证据。解决方案、最终答案和 teacher 轨迹永远不能成为公开初始环境的依据。

### 1.3 每个阶段都允许拒绝

系统宁可输出 insufficient_evidence、environment_partial 或 verifier_missing，也不能制造一个看起来完整但不可追溯的 task。

## 2. 统一中间对象

### 2.1 NormalizedTrajectory

现有 TraceForge trajectory/compiler 负责这一层。统一记录 trajectory id、source record id 和 source hash；message、tool call、tool result 的稳定 ordinal；event kind、scope、integrity、terminal status；tool schema、pairing、输入截断、privacy status；workspace root、cwd、失败和终止边界。本层不执行历史命令，不调用 teacher。

### 2.2 EvidenceLedger

新增的项目级对象，连接现有 TraceForge 产物和后续重建：

```json
{
  "schema": "agent-world/evidence-ledger/v1",
  "trajectory_id": "...",
  "facts": [
    {
      "fact_id": "f-001",
      "kind": "user_requirement|initial_file|dependency|service|failure|artifact",
      "value_ref": "blob-or-structured-value",
      "status": "observed|derived|inferred|unknown|conflicted",
      "source_event_ids": ["..."],
      "confidence": 0.0,
      "conflicts_with": []
    }
  ],
  "barriers": [],
  "coverage": {},
  "open_questions": []
}
```

### 2.3 TaskHypothesis

```json
{
  "schema": "agent-world/task-hypothesis/v1",
  "status": "recoverable|insufficient_evidence|conflicted",
  "objective": "...",
  "requirements": [
    {"id": "r1", "text": "...", "source_fact_ids": ["f-001"], "confidence": 0.0}
  ],
  "inputs": [],
  "deliverables": [],
  "constraints": [],
  "execution_scope": "local_terminal|external_service|unclear",
  "unrelated_segments": [],
  "uncertainties": []
}
```

### 2.4 EnvironmentHypothesis

```json
{
  "schema": "agent-world/environment-hypothesis/v1",
  "workspace": [
    {
      "path": "src/main.py",
      "state": "observed|completed|unknown",
      "content_ref": "...",
      "source_fact_ids": ["f-007"],
      "confidence": 1.0
    }
  ],
  "runtime": [],
  "dependencies": [],
  "services": [],
  "datasets": [],
  "environment_variables": [],
  "permissions": [],
  "network_policy": "none|allowlist|public|unknown",
  "working_directory": "/home/user/workspace",
  "closure_status": "closed|partial|unknown",
  "uncertainties": []
}
```

### 2.5 ReconstructionCandidate

```json
{
  "schema": "agent-world/reconstruction-candidate/v1",
  "task": {},
  "environment": {},
  "verifier": {},
  "evidence_ledger_ref": "...",
  "candidate_id": "...",
  "quality_gate": {},
  "model_provenance": {},
  "status": "candidate|accepted|rejected|needs_review"
}
```

## 3. 模块和 Agent 职责

### M0：数据接入与治理（确定性）

输入 R01 JSONL、meta.json、dataset manifest。负责 JSONL 完整性、source hash、数据集版本、凭据/个人标识隔离、远端数据边界和统计输出。输出 RawRecordManifest、source scan、privacy report。验收：记录数守恒；source signature 不变化；没有凭据进入公开 artifact；每条记录可追溯。

### M1：轨迹规范化（确定性）

复用现有 traceforge.trajectory。负责适配不同日志格式、配对 tool call/result、标记失败/缺失/截断/未知工具、稳定 event id、首次不可解释 mutation 的 recovery barrier。质量门：严格 JSON；tool call/result 一对一；事件 ordinal 单调；unknown action 不能被默认为 read-only；privacy validator 通过。

### M2：会话结构和来源投影（确定性）

复用 query_turns、source_projection、lineage。负责编译 user block、agent step、assistant outcome；区分真实 USER 文本和工具/文件文本；分离同一任务、后续澄清和无关话题；建立 request/capture/successor/duplicate 关系。这一层不做任务语义生成，只提供可信输入。

### M3：TurnEvidence Agent（模型 + 规则）

输入 M1/M2 产物和只读 evidence view。负责对每个 turn 生成可审计 evidence item，标注直接用户要求、用户澄清、工具观察、模型推断，标记 solution leakage、prompt injection 和无关内容。模型必须严格 JSON，每个结论绑定 source event，不得把 tool output 当 user requirement，不得执行工具。规则审查 source id、scope 和不可提升的 uncertainty。

### M4：Failure Analysis Agent（模型 + 确定性统计）

输入 NormalizedCapture、QueryTurn、tool errors、terminal status。负责分类任务理解、环境缺失、代码错误、工具失败、权限、网络、验证、超时和用户中断；定位首个关键失败步骤；区分根因和连锁错误；判断可恢复性。输出 FailureReport：primary_failure、critical_event_ids、causal_hypotheses、recoverability、open_questions。

M3 和 M4 可以并行，但都必须先于任务/环境重建。

### M5：Task Recovery Agent（强模型）

输入 EvidenceLedger、TurnEvidence、FailureReport、用户文本投影。负责恢复同一个用户任务的 objective，合并后续澄清和约束，排除话题切换，提取输入、交付物和验收语义，为每条 requirement 绑定 source fact id 和 confidence。质量门：requirement source 完整；不得引用不存在的 event；不得将 assistant 自己提出的目标提升为用户要求；低置信度 objective 进入人工 review。

### M6：Initial State Replay（确定性）

输入 NormalizedCapture。负责在首次成功修改之前恢复完整文件，记录 partial read、edit old string 和 excluded file，保留 external path 证据但不自动加入 workspace，不执行历史 shell 命令。输出 ReplayResult：observed_initial_files、partial_evidence、excluded_files、mutation_boundaries、unknown_action_barriers。这是环境重建的硬证据层，completion 不能覆盖它。

### M7：Environment Reconstruction Agent（强模型）

输入 TaskHypothesis、ReplayResult、EvidenceLedger、FailureReport。负责推断缺失 workspace 文件片段、运行时、依赖、服务、数据、配置、工作目录，并生成最小可执行环境。允许只读文件查询、包/版本元数据查询、静态依赖分析和 schema 检查；禁止执行历史 shell、访问 teacher solution、修改 observed 初态。输出多个 EnvironmentHypothesis 候选，而不是一个未经比较的答案。

### M8：Environment Closure Agent（模型 + 沙盒探针）

输入 EnvironmentHypothesis 候选。负责检查依赖是否闭合、工作目录/入口/artifact 约定、服务启动和权限。未知依赖只能创建最小 mock 或标记 unknown，不能把“能启动”误判成“任务正确”。

### M9：Sufficiency Judge（独立模型）

输入 TaskHypothesis、EnvironmentHypothesis、ClosureReport。职责只有一个：判断是否足以让另一个 Agent 开始执行任务。不能生成修复方案，也不能看到 teacher rollout。输出 task_sufficient、environment_sufficient、blocking_items、confidence、reason_codes。

### M10：Verifier Synthesis Agent（强模型 + 规则）

输入 TaskHypothesis、可观察交付物和领域 schema。负责生成 deterministic verifier 草案、边界用例和 reward 分解，并明确哪些条件无法验证。Verifier 不能引用原始会话或 teacher solution，不能只检查文件存在，必须区分 infrastructure error 和 task failure。

### M11：Critic / Red Team Agent（独立模型）

输入 TaskHypothesis、EnvironmentHypothesis、VerifierSpec。负责查找 task/environment inconsistency、solution leakage、verifier loophole、instruction 解法泄漏和无证据事实。只有通过 Critic 和确定性 schema validator 才能编译 bundle。

### M12：Harbor Bundle Compiler（确定性）

输入通过质量门的 ReconstructionCandidate。负责生成 task.toml、instruction.md、workspace、environment、solution、tests、control，写 tree hash/source hash/model provenance，拒绝路径冲突、symlink、unknown root entry，并保证 agent workspace 不含 solution/tests。

### M13：Hermes/AGS Rollout Agent（执行型 Agent）

输入 Harbor bundle 的公开面和 runtime appendix。只在独立 AGS sandbox 中执行；只能看到 instruction 和 public workspace；最终 artifact 写入约定路径；保存完整 tool trace、模型版本、Hermes commit、bundle hash；不能改变 control truth。

### M14：独立 Verifier Trial（确定性）

输入 Agent artifact、tests、control。在 separate no-network AGS sandbox 中运行，区分 infra error、task fail、task pass、partial，生成 reward、verdict 和 breakdown。

### M15：Rollout Judge / Data Curator（模型 + 规则）

输入原始轨迹、新 rollout、verdict、FailureReport。负责判断新轨迹是否真正解决原任务，过滤伪成功和 verifier loophole，标记 recovery behavior，生成 SFT tool/loss mask，做 near-duplicate、benchmark contamination 和 split isolation。

## 4. Orchestrator 调度

使用确定性的 DAG，不让多个 Agent 自由聊天：

```
M0 → M1 → M2
          ├→ M3 TurnEvidence
          └→ M4 FailureAnalysis
                 ↓
              M5 TaskRecovery
M1 ─────────────→ M6 Replay
M5 + M6 + M3 + M4 → M7 EnvironmentReconstruction
M7 → M8 Closure → M9 Sufficiency
M5 + M7 + M9 → M10 Verifier → M11 Critic
M11 → M12 Bundle → M13 Rollout → M14 Verifier
M1 + M5 + M13 + M14 → M15 SFT Curator
```

每个节点都记录 input artifact hash、prompt/template version、model/version、sampling 参数、structured output schema、output hash、reject reason 和 retry count。模型之间不传自然语言历史，只读写 versioned artifact。

## 5. 模型分工

标准化、replay、schema、verifier execution 不使用模型；TurnEvidence、FailureAnalysis 使用中等模型；TaskRecovery、EnvironmentReconstruction、Verifier synthesis 使用强模型；Closure、Sufficiency 使用独立模型；Critic 使用独立模型或独立 prompt；Rollout 使用 Hermes + teacher；SFT curation 规则优先、模型辅助。所有实验必须记录模型替换、prompt 版本和执行框架。

## 6. 质量门

Gate A：source hash、schema、privacy、tool pairing 通过。

Gate B：objective 非空、requirement 有证据、execution scope 明确、无严重话题冲突。

Gate C：workspace provenance 完整，依赖/服务 closure 通过，completion 未覆盖 observed state，无 solution leakage。

Gate D：verifier 不只检查文件存在，control 与 public workspace 分离，reward 有明确分解，能区分 infra error/task fail。

Gate E：rollout 通过 verifier，artifact 与 trace 对应，没有 hidden test/tool schema 泄漏，没有 benchmark contamination，source/train/eval 按 group 隔离。

## 7. 实验基线和指标

基线：原始失败轨迹直接 SFT；只做 Task Recovery；Replay-only；Task Recovery + Replay；不带证据门控的 Completion；Evidence-gated Completion；完整流程；Oracle initial environment。

指标：task requirement precision/recall、初始文件 exact match、环境 closure rate、Harbor compile rate、AGS startup rate、verifier pass rate、新 rollout 完成率提升、SFT 后 Pass@1、solution leakage rate、unsupported inference rate、token/time/sandbox cost。

消融：去掉 failure analysis、去掉 provenance、允许/禁止覆盖 observed 初态、文件-only 与文件+依赖+服务、sufficiency judge 开关、teacher 强度、rollout 次数、verifier 过滤、Single-WS/Cross-WS/Multi-Round。

## 8. 阶段计划

P0：项目审计和标注协议。完成现有 TraceForge M1/M2 与新模块的字段映射。选择约 100 条真实轨迹，标注 task、初始环境、失败原因和 verifier 充分性。产出一条完整手工标注样例和冻结的 schema。

P1：单条真实轨迹闭环。TraceForge artifact → TaskHypothesis → ReplayResult → EnvironmentHypothesis → Harbor bundle → verifier。不调用 teacher，先证明 reconstruction 可审计。

P2：多候选重建和 closure。同一轨迹生成多个 environment candidates，用 closure judge 和 critic 排序，统计拒绝率和错误类型。

P3：真实 Hermes/AGS rollout。只对通过 Gate D 的 bundle 执行 rollout，固定模型、Hermes commit、工具集、sandbox、超时、网络和 bundle hash。

P4：批量重建和 SFT。扩展到 100/1000 条轨迹，建立 baseline 和完整消融，之后才开始训练。

## 9. 当前状态

现有 TraceForge 作为底层数据平面继续使用，不重写。已有 trajectory、query turn、source projection、lineage、provenance、artifact validation，以及 TurnEvidence、EnvironmentExposureProfile、SemanticExtraction、TaskEpisode、ReconstructionCandidate 的规格线索。

需要新增或落地的接口是 EvidenceLedger、TaskRecovery、EnvironmentRecovery、Closure、Verifier synthesis 和 Harbor bridge。暂不做真实 teacher 调用、批量 rollout 和 SFT 训练。

第一条验收标准不是代码写完，而是：一条真实失败轨迹能生成一个证据可追溯、环境边界清楚、verifier 独立、可在 Harbor/AGS 中重新执行的 task。
