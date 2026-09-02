# TraceForge 总体实施计划

版本：v0.2

日期：2026-08-31

状态：总体方向与 M0–M7 阶段顺序已确认；实施仍按模块逐项审核

本文定义 TraceForge 的目标架构、模块职责、核心数据契约、MVP 顺序、Post-MVP M7 和验收条件。项目背景、数据事实和主张边界见 [`background-and-goals.md`](background-and-goals.md)，参考实现的处理逻辑与采用边界见 [`reference-repositories.md`](reference-repositories.md)，开发规范只引用 [`../AGENTS.md`](../AGENTS.md)。

## 1. 最终目标主链

```text
R01 Flowback JSONL
→ SourceRecordRef / NormalizedCapture / RequestBoundary
→ Immutable Visible EventLog
→ RequestLineageForest + CaptureRelationGraph
→ QueryTurn / TaskEpisode DAG
→ ObservedTaskDistribution + EnvironmentExposureProfile
→ ReconstructionCandidate
→ TaskIntent + DomainKit + WorldTemplate
→ TaskWorldCandidateRevision
→ SemanticTruth + ReferenceWitness + Verifier
→ G0–G5 + G7
→ RunnableTaskWorldCandidateBundle
→ RolloutEvidence + EvaluationEvidence
→ G6 + TargetedRepair
→ CertifiedTaskWorldRelease
→ CertifiedDifficultyProfile
```

M6 验收后的代际反馈链为：

```text
新增 Reality evidence + 已认证 Capability evidence
→ GenerationPlan
→ 新物化的 Task–World candidate family
→ Truth / Reference / Verifier / G0–G5 + G7
→ RunnableTaskWorldCandidateBundle
→ rollout / independent evaluation / G6
→ 新一代 release、轨迹证书与边界证据
→ 下一代 GenerationPlan 或结构化停止
```

系统包含两个反馈来源，但不能把它们混成一个判断器：

```text
Reality guidance
ObservedTaskDistribution + EnvironmentExposureProfile
→ 决定哪些任务、工作流和环境形态值得生成

Capability guidance
Teacher/Target RolloutEvidence
→ 决定已认证任务在哪些难度维度上需要增强或减弱
```

两个来源只能修改白名单生成参数。任何一方都不能直接覆盖 Truth、Verifier policy 或已冻结的 candidate revision。

## 2. 总体架构

```text
                    ┌────────────────────────────┐
R01 JSONL ─────────▶│ 1. Trajectory Compiler    │
                    │ Source / Request / Event / Graph│
                    └─────────────┬──────────────┘
                                  ↓
                    ┌────────────────────────────┐
                    │ 2. Distribution Profiler  │
                    │ Task / Environment Exposure│
                    └─────────────┬──────────────┘
                                  ↓
                    ┌────────────────────────────┐
                    │ 3. Candidate Router        │
                    │ Eligibility / Value / Cost │
                    └─────────────┬──────────────┘
                                  ↓
DomainKit + WorldTemplate ────────┤
                                  ↓
                    ┌────────────────────────────┐
                    │ 4. Task–World Compiler     │
                    │ Facts / Graph / Candidate  │
                    └─────────────┬──────────────┘
                                  ↓
                    ┌────────────────────────────┐
                    │ 5. Static Certification   │
                    │ Truth / Reference / G0–G5 + G7│
                    └─────────────┬──────────────┘
                                  ↓
                    RunnableTaskWorldCandidateBundle
                                  ↓
                    Rollout Contract / Harbor 边界
                                  ↓
                    G6 / Targeted Repair
                                  ↓
                    ┌────────────────────────────┐
                    │ 6. Difficulty Calibration │
                    │ H-vector / Boundary Search │
                    └─────────────┬──────────────┘
                                  ↓
                    CertifiedTaskWorldRelease
```

`pipeline.py` 只编排公开接口和 artifact 传递，不承载解析、推断、合成、判分或课程逻辑。

## 3. 模块职责

| 模块 | 负责 | 不负责 | 主要输出 |
| --- | --- | --- | --- |
| `trajectory` | R01 接入、请求边界、事件标准化、call/result 配对、lineage、Turn 图 | 任务难度、GT、World 合成 | `RequestLineageForest`、`EventLog`、`ThreadTurnGraph` |
| `profiling` | Episode 语义、任务分布、环境暴露、重建 eligibility 和初始难度 | 生成 World facts、判断最终可解性 | `TaskEpisode`、`ObservedTaskDistribution`、`EnvironmentExposureProfile`、`ReconstructionCandidate` |
| `synthesis` | TaskIntent 与 World 联合物化、Public/Control 投影、Reference 与 Truth 输入 | 模型 rollout、外部调度 | `TaskWorldCandidateRevision` |
| `certification` | 静态门、rollout 可解性、歧义、泄漏、Verifier mutation 和定向修复 | 根据模型多数票生成 GT | `RunnableTaskWorldCandidateBundle`、`CertificationReport`、`CertifiedTaskWorldRelease` |
| `calibration` | 六维结构难度、rollout 难度证据和有界矫正建议 | 修改 Truth 或无界自动造题 | `CertifiedDifficultyProfile`、`DifficultyAdjustment` |
| `evolution` | M6 之后编排现实反馈、能力反馈、代际计划、质量多样性档案和算子效果证据 | 修改 Truth/Verifier、训练模型权重、把检索结果当事实 | `GenerationPlan`、能力边界快照、轨迹证书和下一代建议 |
| `export` | 稳定 Public/Control Bundle 与最小 rollout 输入输出契约 | Harbor Job、Trial、并发和沙盒生命周期 | `RolloutRequest`、`RolloutEvidence`、`EvaluationEvidence` |

跨模块对象必须使用显式类型和版本。模块只能依赖其他模块的公开契约，不能导入其私有实现。

## 4. 轨迹结构化设计

当前精确算法、契约、CLI 和全量验收只在 [`r01-processing-spec.md`](r01-processing-spec.md) 定义。本阶段依次为 M1A Source Adapter、M1B Structural Compiler、M1C Request/Capture Graph 和 M1D QueryTurn；M1A、M1B v3 与 M1C 已通过（M1C 契约见 [`m1c-processing-spec.md`](m1c-processing-spec.md)，验收见 [`r01-m1c-validation.md`](r01-m1c-validation.md)），已验收代码停止在 M1C，M1D 规格处于评审稿。

M1A 必须将物理格式与语义 schema 分开：`jsonl` 只负责字节账本，`traceforge.restored-long-capture.v1` 是当前唯一显式来源契约。它通过单一 adapter 产生 typed envelope，不做 schema 猜测、fallback、注册表或配置 DSL。未来只有在第二种真实输入出现后才新增 adapter。

### 4.1 SourceRecordRef

R01 没有顶层 `record_id`。每条原始行生成：

```yaml
SourceRecordRef:
  schema_version:
  source_record_id:
  dataset_id:
  dataset_sha256:
  line_number:
  byte_offset:
  byte_length:
  line_sha256:
  ingestion_status: PARSED | QUARANTINED
  parse_error: null | {code, message}
```

原始字节不可覆盖。任何清洗、合并、推断和标签都必须作为派生 artifact 保存来源引用。

### 4.2 RequestBoundary 与关系图

`source_request_ids` 与 `terminal_prefix_depths` 先建立确定性 `RequestBoundary`。跨 capture 关系分为高可信 Request lineage 和低等级可见投影，`thread_id/account_id` 只缩小比较范围，不能直接建边。

```text
RequestLineageForest      → Grade A 显式 request 关系
CaptureRelationGraph      → Grade A 重复/共享关系
                         + Grade B 可见前缀投影
```

不得直接选最长 capture 后丢弃其他记录。Grade B 不能用于因果继承或主分布硬去重。边只承载已成立的正向关系，不存在"无法判定"的边；`raw_request_hash` 不满足格式契约时以逐 capture 的 `raw_request_hash_status=UNKNOWN` 表达缺席，不产生占位关系值（见 [`m1c-processing-spec.md`](m1c-processing-spec.md) §5）。

### 4.3 EventLog

统一事件类型：

```text
SYSTEM
USER
ASSISTANT_MESSAGE
TOOL_CALL
TOOL_RESULT
```

事件同时保留：

- 规范化可见时序边；
- tool call/result 显式 ID 边；
- capture 和 source lineage；
- content 形态和长度；
- 完整性、冲突和推断状态。

M1 v3 的每个 event 都保存 `visible_payload_utf8_byte_length`、`visible_payload_sha256` 和 `integrity_status=COMPLETE`。前两者来自去除 reasoning 摘要后的可见 payload 的 canonical JSON bytes；`COMPLETE` 只表示该可见事件完整映射到当前契约，不代表原始 wire 日志、任务结果或环境状态完整。

只按显式 ID 建立确定 pairing。基于位置推断的关系必须单独标记，不能进入认证任务的硬证据。

compaction 只有 capture 级证据时标记 `UNLOCALIZED_COMPACTION_EVIDENCE`，不得虚构事件位置。M1 不保存旧回流 `reasoning_content` 原文，也不把它作为语义输入；assistant event 中同名字段只承载存在性、UTF-8 字节数、SHA-256 和来源 JSON pointer 组成的审计摘要。

### 4.4 ActionBatch 与 AgentStep

同一个 assistant decision 中的一个或多个 tool call 构成 `ActionBatch`：

```text
AgentStep
= AssistantDecision
 + ActionBatch?
 + ToolObservation*
```

并行语义明确时保留并行；不明确时使用 `UNKNOWN`，不能根据 tool result 到达顺序反推因果顺序。

### 4.5 QueryTurn

```text
QueryTurn
= UserBlock
 + AgentStep*
 + AssistantOutcome?
```

`UserBlock` 允许包含连续 user messages。AssistantOutcome 缺失时 Turn 仍可存在，但状态为 `INCOMPLETE`。

### 4.6 TaskEpisode DAG

多个 QueryTurn 根据闭合关系组成一个 TaskEpisode：

```text
REFINES
CONTINUES
DEPENDS_ON
CORRECTS
CANCELS
BRANCHES_FROM
NEW_TASK
AMBIGUOUS
```

合并条件：共享根目标、后续交付依赖前序产物、共享明确状态，或者后续轮次是在纠正/完善此前任务。无共享交付和状态的独立请求必须拆开。

例如“查找 10 条新闻”与“将其中一条整理成报告”具有 `DEPENDS_ON`，应合成一个复合 TaskEpisode。

TaskEpisode 属于 M2 profiling，而不是 M1 结构编译。语义关系采用两次独立、封闭枚举的模型提取并引用 event ID；低置信度、无证据或两次结果不一致时标记 `AMBIGUOUS`。

## 5. 数据质量与阶段 eligibility

不得用一个全局 `valid` 标志决定一条 capture 的全部用途。至少分开：

```yaml
Eligibility:
  task_profile: ELIGIBLE | PARTIAL | INELIGIBLE
  environment_profile: ELIGIBLE | PARTIAL | INELIGIBLE
  reconstruction: ELIGIBLE | INELIGIBLE
```

典型处理：

- final 缺失，但 user goal 明确：任务画像可用；
- tool result 部分缺失：环境画像部分可用；
- tool schema 冲突：只记录冲突，不生成硬工具契约；
- task 边界含糊：Episode 保留，但不进入重建；
- 原始内容含不可处理敏感信息：隔离，不进入下游模型。

每条输入最终只能处于 `USABLE_COMPLETE`、`USABLE_PARTIAL` 或 `QUARANTINED`，且必须有原因码。

## 6. 任务与环境分布

### 6.1 ObservedTaskDistribution

统计单位是 lineage 去重后的 TaskEpisode。字段至少包括：

- 业务 Domain 和 task family；
- 用户目标、约束种类和证据要求；
- QueryTurn 数量与组合关系；
- 工作流 motif 和 canonical tool role；
- 交付形式；
- 时间、版本和 scope 要求；
- 显式 persona、组织和权限；
- `ObservedDifficultyEstimate`；
- unknown、abstain 和 eligible denominator。

`domain_meta.task`、rubric 和 risk 只作为后处理先验，语义结论必须能够回指原始 user event。

### 6.2 EnvironmentExposureProfile

字段至少包括：

- system/harness fingerprint；
- declared、called 和 observed tool roles；
- schema 冲突和推断状态；
- 可见资源类型和引用关系；
- observation 类型、字节/token 负载；
- empty、partial、error、truncated 和 recovery；
- 可见状态读写证据；
- 时间、版本、权限与 compaction 线索；
- 不可观测性声明。

聚合结果统一称为 `R01 observed episode distribution`，不能写成“真实生产环境分布”。

## 7. ReconstructionCandidate

### 7.1 准入 Gate

任务进入重建前必须满足：

- 来源和隐私允许；
- TaskEpisode 边界足够明确；
- 能形成闭合 TaskIntent；
- 至少能构造一个确定性 Truth resolver 或封闭答案集合；
- 证据可通过计划中的工具到达；
- 具备可实现的 Verifier；
- 不是纯基础设施问题。

### 7.2 选择向量

`ReconstructionCandidate` 不使用不透明单分数决定一切。保留：

```text
现实覆盖价值
业务任务完整度
工作流组合丰富度
环境可重建性
Verifier 可构造性
ObservedDifficultyEstimate
新颖性
歧义风险
隐私风险
预估构造成本
```

选择采用 Gate 后的分层配额：按 task family、难度区间、环境表面和交付类型采样，避免只选择最常见或最难任务。

## 8. Task 与 World 联合合成

### 8.1 三层环境模型

```text
DomainKit
    固定 canonical tools、资源类型和状态规则

WorldTemplate
    人物、项目、资源、权限、时间和政策的关系拓扑

WorldInstance
    针对 TaskIntent 生成的具体事实、文件、消息、索引和状态
```

R01 是来源适配器，不是 DomainKit。首个 DomainKit 由任务分布选择，当前优先候选是“多源证据调查与专业交付”。

### 8.2 WorldGraph

V0 支持的节点候选：

```text
Actor
Organization
Project
Resource
ResourceVersion
Message
Policy
Tool
Clock
State
```

关系候选：

```text
OWNS
CAN_READ
CAN_WRITE
SUPERSEDES
REFERENCES
CONFLICTS_WITH
PRODUCED_BY
DEPENDS_ON
REQUIRED_FOR
```

每个事实或资源必须标记：

```text
OBSERVED_STRUCTURE
DERIVED_CONSTRAINT
SYNTHETIC_CONTENT
CONTROL_TRUTH
```

### 8.3 真实感规则

环境噪声只能来自关系一致的自然邻域，例如：

- 同一项目的过期版本；
- 同名但 scope 不同的实体；
- 相关但不能支持决定性结论的消息；
- 与正确资料共享关键词的错误资料；
- 符合真实 observation 分布的空结果或受控错误。

每个困难或噪声都必须具有前置条件、保持不变量，并由对应 Verifier 覆盖。随机填充无关文件不算真实感。

### 8.4 Reference-first 编译顺序

```text
1. 从 ReconstructionCandidate 冻结 TaskIntent，不包含事实答案
2. 物化 WorldGraph 和 CanonicalFactTable
3. 由 Control World 独立计算 SemanticTruth
4. 建立 EvidenceRequirementGraph
5. 构建并执行 ReferenceWitness
6. 检查 ReferenceClaims 与 SemanticTruth 一致
7. 最后渲染用户可见任务
8. 编译 Verifier
9. 冻结 TaskWorldCandidateRevision
```

`TaskWorldCandidateRevision` 的 digest 同时覆盖任务、Public World、Control World、工具契约、Truth、Reference、Verifier 和 provenance。任一组成变化后必须产生新 revision，并重新认证。

## 9. Public 与 Control 隔离

```text
TaskWorldCandidateRevision
├── public_rollout_bundle/       → Student / Teacher
│   ├── TaskPublicView
│   ├── WorldPublicView
│   └── RuntimePublicContract
├── control_evaluation_bundle/   → Oracle / Reference / Verifier
└── static_certification/
```

Public 与 Control 必须物理分包。Agent sandbox 只能获得 `public_rollout_bundle`，独立 verifier 才能获得 `control_evaluation_bundle`。

Public 侧禁止出现：

- SemanticTruth；
- Reference answer；
- Verifier criterion 的隐藏答案；
- 原始回流路径和敏感 literal；
- difficulty arm 或 operator 名称；
- host 绝对路径；
- source lineage 解析信息。

## 10. 可解性认证

质量门按顺序执行：

```text
G0 Schema / Provenance / Privacy
G1 Task–World Grounding
G2 Truth Closure
G3 Evidence Reachability
G4 Reference Execution
G5 Verifier Mutation & Alternative Path
G7 Public/Control Leakage Scan
导出 RunnableTaskWorldCandidateBundle
Rollout + Independent Evaluation
G6 Blind Teacher Closure
发布 CertifiedTaskWorldRelease
```

### G1：Task–World Grounding

任务中的每个实体、约束、时间边界、权限和交付要求都必须在 World 或公开任务条件中有对应来源。

### G2：Truth Closure

Truth 必须由确定性 Oracle/程序/查询从 Control facts 计算。允许封闭答案集合，不强制单字符串答案。

### G3：Evidence Reachability

每个必需 evidence leaf 都必须能在权限、工具和预算约束下由 Public World 获取。

### G4：Reference Execution

Reference 在干净 reset 上执行，并证明至少存在一条合法可达路径。Reference 不定义 Truth，也不能成为 Verifier 强制的唯一动作序列。

### G5：Verifier Validation

Verifier 至少拒绝：错实体、错版本、错时间、缺关键字段、未观察却伪造引用、空答案和典型近似错误；同时接受一条不同于 Reference 的合法路径或合法等价输出。

### G7：Public/Control Leakage Scan

G7 必须在任何 Student 或 Teacher 模型看到 Public Bundle 之前运行。每次修复和 difficulty mutation 后都必须重新扫描并重新导出物理分包。

### G6：Blind Teacher Closure

强模型只能看到 Public Bundle。MVP 默认 3 次 rollout，至少 2 次通过确定性 Verifier，且不能出现 Verifier 接受但 Oracle 不支持的分歧答案。

模型多数票不能定义或修改 GT。新的候选答案只有经 Oracle 从 World facts 独立确认后才能进入合法答案集合。

G6 同时消费 `RolloutEvidence` 和独立的 `EvaluationEvidence`。rollout、capture、verifier 或 sandbox 基础设施失败不能折算为任务失败。

## 11. 定向纠正

认证失败生成结构化 `RepairDiagnosis`，只允许修改责任组件：

| 失败码 | 责任组件 | 允许动作 |
| --- | --- | --- |
| `TASK_UNDERSPECIFIED` | Task renderer | 补足公开约束或明确允许范围 |
| `EVIDENCE_UNREACHABLE` | World/Tool | 修资源、索引、权限或工具可达性 |
| `TRUTH_NON_UNIQUE` | Facts/Intent | 收紧约束或声明封闭答案集合 |
| `REFERENCE_MISMATCH` | Reference/Truth | 修 Witness 或 resolver，不掩盖不一致 |
| `VERIFIER_BRITTLE` | Verifier | 修 criterion、comparator 或证据覆盖 |
| `PUBLIC_LEAKAGE` | Projection | 删除泄漏并重新编译 Public Bundle |
| `INFRA_FAILURE` | Execution | 保持 candidate revision 不变，只按预算重试 |

每次修复产生新的 revision 和 parent digest。MVP 默认最多两轮自动修复；仍未闭合则 `QUARANTINED`，不能持续改写直到通过。`TOO_EASY/TOO_HARD` 属于 M5 难度校准，不进入静态认证修复；难度修改必须生成新 revision 并从 G0 重新执行。

## 12. 难度模型

### 12.1 重建前难度

`ObservedDifficultyEstimate` 从原 Episode 的可见事实估计，包含六维值、置信度、缺失字段和证据 event IDs。它只参与候选选择，不能作为生成任务的最终难度标签。

### 12.2 重建后结构难度

```text
H = [H_retrieval, H_planning, H_tool, H_state, H_judgment, H_delivery]
```

每维范围 `0–5`：

- `H_retrieval`：证据源数量、跳数、稀疏度、干扰相似度；
- `H_planning`：子目标、依赖深度、分支和回退；
- `H_tool`：工具数、schema 歧义、参数精度和受控失败；
- `H_state`：时间版本、权限、部分可观测和状态依赖；
- `H_judgment`：证据冲突、政策、scope、专业判断和合理弃权；
- `H_delivery`：格式、引用覆盖、计算、专业报告和文件交付。

综合分可以用于排序，但认证、分析和纠正必须保留原始向量。

### 12.3 模型实测难度

`CertifiedDifficultyProfile` 同时记录：

- Teacher pass rate；
- 目标模型 criterion pass rate；
- Pass@1 / Pass@k；
- 工具步数、token 和执行成本；
- failure criterion 分布；
- infra、invalid 和 unscorable 计数。

MVP 的目标模型默认跑 3 次：

- 3/3 通过：候选过易，可增加一个 Hardness Operator；
- 1/3 或 2/3：边界任务；
- 0/3 且 Teacher 通过：候选过难，回滚最后算子或生成 relief；
- Teacher 未闭合或存在 infra：不产生正式难度桶。

任务库的推荐 criterion pass rate 区间为 30%–80%。

## 13. Hardness Operator

每个算子必须声明：

```yaml
HardnessOperator:
  operator_id:
  target_dimension:
  preconditions:
  mutation:
  preserved_invariants:
  verifier_extension:
  rollback:
```

候选类型：

- Context expansion；
- Distractor injection；
- Constraint entanglement；
- Partial observability；
- State dependency；
- Tool ambiguity；
- Controlled failure；
- Temporal inconsistency；
- Policy conflict；
- Counterfactual branch；
- Recovery requirement；
- Client-ready output。

MVP 只冻结一个由 R01 画像支持的算子，一次只施加一个。自动发现新算子属于 Post-MVP。

## 14. Task 与 World 联合演化

本节定义 M6 纵向闭环验收后的 Post-MVP 目标。M0–M6 未完成并通过审核前，不实现本节模块、不冻结新增契约，也不创建空包。

联合演化的目的不是让 Task 和 World 无约束互相改写，而是在保持可解性、来源闭合和真实结构覆盖的前提下寻找模型边界，并持续产生可验证且具有训练或诊断价值的轨迹。这里的“自进化”首先指 Task、World、课程和轨迹资产演化，不等同于模型权重自动更新。

### 14.1 来源闭合的现实—能力双环

系统保留两条相互独立的反馈环：

```text
Reality loop
新增回流
→ RequestLineageForest / TaskEpisode
→ ObservedTaskDistribution + EnvironmentExposureProfile
→ 现实覆盖缺口与生成约束

Capability loop
CertifiedTaskWorldRelease + bound Teacher closure evidence
+ Target RolloutEvidence + EvaluationEvidence
→ CertifiedDifficultyProfile
→ 能力边界与算子效果证据
```

两条反馈只能在代际规划时合并为白名单 `GenerationPlan`：

```text
真实任务结构约束
        ∩
DomainKit / WorldTemplate 可执行能力
        ∩
已认证 rollout 暴露的能力边界
        ↓
下一代 Task–World candidate family
```

Reality loop 防止系统只围绕模型弱点漂离真实需求；Capability loop 防止系统只复刻高频但没有信息量的普通任务；认证主链防止二者用不可解任务、答案泄漏或 Verifier 漏洞制造虚假难度。

回流只能决定需求、约束、工作流和环境暴露的生成先验，不能提供 GT。Target failure、Teacher majority、检索相似度和模型归因都不能修改 `SemanticTruth`、`Verifier` policy 或已冻结 revision。

### 14.2 Trace-to-World 派生规划视图

M7 可以从既有 `TaskEpisode`、`ObservedTaskDistribution`、`EnvironmentExposureProfile` 和 `ReconstructionCandidate` 派生 `TaskEnvironmentGenome` 概念对象：

```yaml
TaskEnvironmentGenome:
  source_episode_refs:
  intent_shape:
  constraint_shape:
  workflow_partial_order:
  evidence_requirement_shape:
  environment_topology:
  resource_and_tool_roles:
  state_permission_version_patterns:
  delivery_contract:
  supported_hardness_axes:
  derivation_provenance:
  unknowns:
```

该对象是面向生成规划的 typed view，不是第二套 Episode、Task、World 或 Truth 事实源。Genome 只表达 `OBSERVED_STRUCTURE`、`DERIVED_CONSTRAINT` 和 unknown：观察结构字段引用 source/episode/event evidence，派生约束字段引用 profile、derivation rule 和 artifact digest，未知字段记录缺失原因。`derivation_provenance` 必须按字段路径记录这些引用；`supported_hardness_axes` 只记录真实画像支持的轴，能力环的具体选择及其证据进入 `GenerationPlan`。`SYNTHETIC_CONTENT` 不进入 Genome，只能在后续 `GenerationPlan` 和 `WorldInstance` 中显式物化。

它保存可复用的真实结构，例如“多源查询 → 判断指定时间点的正确版本 → 排除过期材料 → 形成带证据的专业交付”，不复制真实用户实体、原 assistant 答案、敏感 literal 或 `reasoning_content`。结构事实仍由确定性代码产生；语义推断继续遵循 M2 的证据绑定、双独立确认和 abstain 规则。

合成目标不是恢复唯一原环境，而是在上述约束下物化一个最小充分、可执行、可重置且可独立计算 Truth 的新 World。环境侧还必须从 `DomainKit`、`WorldTemplate`、合法动作和证据可达性反向确认其 affordance；候选只有同时满足真实结构约束和环境可执行能力时才进入编译。

### 14.3 版本化演化记忆与 GraphRAG 边界

演化记忆按语义作逻辑与契约分域，节点只引用权威 artifact，不复制事实；Public/Control 继续遵循第 9 节的物理分包：

```text
现实证据域
  Source / Event / Turn / Episode / observed profile

任务环境域
  Genome view / WorldTemplate / candidate revision / certification

能力证据域
  model-harness-tool manifest / attempt / criterion result / difficulty

演化审计域
  generation / parent-child / operator / repair / rejection / attrition
```

`OBSERVED_STRUCTURE`、`DERIVED_CONSTRAINT`、`SYNTHETIC_CONTENT` 和 `CONTROL_TRUTH` 不得混写。合成 rollout 永远不能反向更新 `ObservedTaskDistribution`；历史 candidate、attempt 和淘汰记录只能新增 revision，不能覆盖。

GraphRAG 可以作为上述记忆的可选检索层，用于回答真实覆盖缺口、结构重复、历史算子效果、失败归属和候选邻域。检索输出必须是带来源、版本、置信状态和 digest 的 typed evidence bundle。GraphRAG 不产生 Truth、不判定正式难度、不替代 Verifier，也不要求 MVP 或 M7 初版采用图数据库；在查询模式稳定前优先使用版本化文件和简单索引。

### 14.4 反事实 Task–World 任务族

一个入选的 Genome/`GenerationPlan` 条目可以产生具有显式关系的 sibling family：

- Canonical：基础闭合版本；
- Truth-preserving twin：在声明 canonical graph isomorphism、实体映射和保持不变量后，只改变实体名、顺序或关系一致的非决定性邻居，并重新计算 Truth 验证其保持不变；
- Truth-changing twin：只改变一个决定性 World fact，由 Oracle 重新计算 Truth；
- Hard/relief sibling：只应用一个已冻结的 `HardnessOperator` 或其回滚。

每个 sibling 必须记录 parent revision、目标难度维度、唯一 World/Task delta、保持不变量、Truth 重算结果、Verifier extension、rollback、`generation_seed` 和配对 `rollout_attempt_seed`。一次只允许一个白名单变化；任何 Task、World、Truth、Reference、Verifier 或 provenance 变化都产生新的 `TaskWorldCandidateRevision`，并从 G0 重新执行完整认证。

首个 M7 不追求完整 sibling family，只要求 canonical 加一个由真实画像支持的单因素 twin。后续扩展必须由已观察到的覆盖缺口或算子效果证据驱动，不能用随机增加文件、工具或上下文冒充环境多样性。

### 14.5 代际规划、质量多样性与主动边界搜索

闭环必须把生成前规划和生成后认证分开，不能用尚未生成的 Truth、Verifier 或 Teacher 结果作为本 candidate 的规划准入条件：

```text
1. 规划准入
   来源/lineage/隐私 + ReconstructionCandidate + Genome evidence + DomainKit affordance
2. 规划选择
   现实覆盖缺口 + 结构新颖性 + 历史预计认证率 + 预计信息量 + 成本
3. 编译
   GenerationPlan → TaskWorldCandidateRevision
4. 正式认证
   G0–G5 + G7 → RunnableTaskWorldCandidateBundle
   → rollout / independent evaluation → G6
5. 实测与归档
   CertifiedTaskWorldRelease + CertifiedDifficultyProfile + criterion evidence
```

规划准入失败进入 attrition；它不能通过软分数补偿。规划选择中的“历史预计认证率”只能来自相同 family/operator 的既有代际证据，用于预算排序，不能替代本 candidate 的正式认证。只有已发布且完成 Target evaluation 的 release 才进入质量多样性档案；未发布 revision、Teacher 未闭合、invalid、unscorable 和 infra 结果进入独立审计与淘汰视图。

质量多样性档案按以下维度形成确定性分桶和配额：

```text
task family
× environment motif
× 六维难度区间
× behavioral failure signature
× delivery type
```

初版不实现通用进化算法。每个 cell 的容量在 `GenerationPlan` 中冻结，只保留一个主 release 和有限个结构不同的 release。归档和下一代选择保留完整向量，不压成不透明单分数：

- 真实覆盖增益；
- 结构新颖性和重复风险；
- 能力边界信息增益及其不确定性；
- 轨迹证据价值；
- 构造、认证和 rollout 成本。

每一代开始前冻结 `GenerationPlan`，至少记录输入 snapshot、model/harness/tool manifest、选择依据、允许算子、source lineage 引用、`generation_seed`、预分配的 `rollout_attempt_seed` 矩阵、预算、archive cell 容量、最大纯合成祖先深度、最大 repair/mutation 轮数、边界不确定性阈值和停止条件。本代结果只能影响下一代，不能看见结果后为某一组补样或改变判定规则。

三个 seed 概念必须分开：source lineage 是观察来源选择，不称为随机 seed；`generation_seed` 只控制 Task–World 确定性物化；`rollout_attempt_seed` 只控制一次 rollout 的可复现实验条件。冻结 holdout lineage 永远不能进入 `GenerationPlan`、演化记忆检索或训练数据。

M5 的 3 次 Target rollout 只提供 MVP 难度桶。M7 必须同时记录样本不确定性，不能把一次 1/3 或 2/3 结果直接宣称为稳定边界或稳定算子效果。过易、边界和过难的后续动作遵循第 12 节；歧义、任务无效、不可判分和基础设施失败分别进入诊断、隔离或预设重试，不触发无约束难度演化。若后续需要状态枚举，必须在 M5/M7 对应契约开始实现时统一冻结。

### 14.6 可证明轨迹与对比轨迹包

最终答案通过不等于轨迹高质量。Control 侧应派生 `TrajectoryCertificate`，至少引用：

```text
certified release ID + candidate revision digest
model / harness / tool manifest
实际发生的 tool call、observation 和 state transition
output claim → observed evidence → Control World fact 的证据边
完整 Verifier hidden criterion results
infra、privacy 和 leakage status
执行成本与预算
```

其中包含 Control fact、Reference 或隐藏 criterion 的内容只能存放在 `control_evaluation_bundle` 或物理隔离的 control-only 代际产物中，绝不能进入 Public Bundle、Student/Teacher 上下文或训练导出。证书只使用模型外部可观察行为，不能读取或保存 `reasoning_content`，也不能把事后 LLM root-cause 解释提升为事实。

若需要发布或训练，必须从 Control 证书生成单独的安全投影（概念名 `TrajectoryPublicView`）。该投影只包含模型实际收到的 Public observation、可公开的工具/状态转换、成本和非泄漏行为标签，并再次执行 G7 等价的泄漏扫描；不得包含 Control fact、Reference answer、隐藏 criterion、difficulty arm 或 operator 名称。

高价值轨迹可以在 Control 侧组合为对比轨迹包：canonical 的已验证成功轨迹、单因素 hard twin 的局部失败轨迹、Reference/Teacher 的合法恢复轨迹，以及明确的 operator delta 和 criterion evidence。其可发布 `ContrastiveTrajectoryPacket` 只能由通过泄漏扫描的安全投影组成。失败轨迹必须作为独立资产标明失败类型，不能混入正向监督或伪装成成功轨迹。

### 14.7 演化不变量与停止条件

当前 M0–M6 会用全量 R01 完成工程画像，因此不能在 M7 启动后再从同一 R01 切出 lineage 并声称它是独立 holdout。正式研究验证只能选择以下协议之一：在一次独立 research rerun 中于 M2 开始前预注册 discovery/generation/holdout lineage；或使用 M6 之后到达、在 `GenerationPlan` 和选择规则冻结前从未进入规划侧画像、演化记忆、检索或训练数据的未来 cohort 作为外部 anchor。计划冻结后，可以用同版本的冻结 M1/M2 编译器在物理隔离的 evaluation-only 环境中生成带 evaluation scope 的 `ObservedTaskDistribution` 来计算 motif 覆盖；该画像及其原始 lineage 永远不能回流到规划侧画像、GraphRAG、下一代计划或训练数据。当前全量 R01 MVP 只验证工程闭环，不承担独立泛化结论。

除本计划其他章节的不变量外，代际循环还必须满足：

- 研究划分一经冻结不得跨域检索；holdout lineage 只能在计划和选择规则冻结后用于独立评估；
- 生成器只能提案，Truth resolver、Reference 和 Verifier 保持独立权威，不能由同一模型循环自证；
- `generation_seed`、`rollout_attempt_seed`、attempt 矩阵和选择规则预分配，构建、认证、Teacher、Target 和 infra 的完整分母进入 attrition；
- 每次只进行一个可归因变化，保留所有 parent-child、operator 和 manifest；
- 使用新的 `generation_seed` 物化下一代，并按 `GenerationPlan` 的数值上限约束纯合成祖先深度，防止无限自我复制；
- 任何修复或变异都从 G0 重跑，G7 在任何模型看到 Public Bundle 前完成；
- 达到预算、连续无合格候选、Verifier 无法可靠区分、边界不确定性不再下降或真实约束不足时，必须结构化停止或 quarantine，不能持续改写直到通过。

未来若接入模型训练，新的模型权重必须绑定新的 model manifest，并在冻结的外部 anchor/holdout 上重新评估。训练数据不得反向进入同 lineage、同 WorldTemplate 或同 operator family 的正式评测。模型训练不属于 M7 的完成条件。

### 14.8 可证伪的研究验证

双环价值必须通过预注册对照验证，而不是根据最终发布结果反推：

```text
theme-only 生成
vs. 回流文本检索
vs. typed Genome + provenance
vs. Genome + provenance + counterfactual + active boundary
```

主要指标至少包括正式认证产率、预注册 holdout 或未来 cohort 上的真实 motif 覆盖、结构重复率、Verifier false accept/false reject、单位 rollout 的边界信息增益、轨迹 evidence coverage、成本和各阶段 attrition。只有完整方法同时改善现实覆盖与高信息边界任务产率，才能支持“双环优于单环”的研究主张；训练收益需要在等任务数、token 和算力的独立实验中另行验证。

GraphRAG、自动出题、失败后加难或强模型筛题本身不构成研究创新。TraceForge 待验证的差异化主张是：从不完整真实回流中编译带来源证明的最小交互结构，联合生成可执行 Task–World，通过独立 Truth/Reference/Verifier 认证，再用单因素反事实实验定位能力边界，并以轨迹信息量驱动下一代生成。该主张只有在上述对照和冻结 holdout 上成立后才能对外使用。

### 14.9 演化白名单与禁止项

允许的演化：

- Surface variant：保持 Truth 与结构，只改实体和表达；
- World expansion：增加关系一致的资源和上下文；
- Difficulty mutation：应用一个受控 Hardness Operator；
- Task composition：合并具有真实依赖的子目标；
- Temporal/version drift：在重新计算 Truth 后推进世界状态。

禁止直接修改：

- 已冻结的来源声明；
- Oracle 规则；
- Verifier policy；
- Public/Control 边界；
- 在不重新认证的情况下修改任务或 World。

## 15. Artifact 与输出契约

建议 artifact 布局：

```text
artifacts/r01/<run_id>/
├── source_manifest.json
├── trajectory/
├── profiling/
└── candidate_revisions/
    └── <revision_id>/
        ├── public_rollout_bundle/
        ├── control_evaluation_bundle/
        ├── static_certification/
        └── manifest.json
```

只有 M7 启动并冻结相应契约后，才增加代际 artifact；不得在当前 M1 或 M0–M6 实现期间创建占位目录：

```text
artifacts/r01/<run_id>/evolution/<generation_id>/
├── planning/
│   ├── generation_plan.json
│   ├── evidence_bundle.json
│   └── archive_snapshot.json
├── control_evaluation/
│   ├── operator_effect_evidence.jsonl
│   ├── capability_boundary_snapshot.json
│   └── trajectory_certificates.jsonl
├── release/
│   └── trajectory_public_views.jsonl
└── attrition_report.json
```

代际 artifact 必须引用既有 source、profile、candidate revision、rollout 和 evaluation digest，不能复制或覆盖其权威内容。`planning/` 是不向 Student/Teacher 暴露的内部规划区，只能保存引用和获准的生成参数，不能复制 Control payload；`control_evaluation/` 继承 Control 侧访问边界；`release/` 只能包含经过泄漏扫描的安全投影。正式文件名和 schema 在 M7 开始时按实际首个实现冻结，上图不构成当前代码接口。

完整 artifacts、真实数据和 rollout 结果不提交 Git。Git 只保存 schema、代码、脱敏小 fixture 和文档。

### 15.1 下游 rollout 边界

TraceForge 只定义：

```text
RolloutRequest
  candidate_revision_id
  public_bundle_ref
  agent/model manifest
  attempt_id
  seed
  budget

RolloutEvidence
  attempt_id
  delivered observations
  tool calls/results
  final deliverable
  infra status
  artifact refs

EvaluationEvidence
  attempt_id
  verifier manifest
  criterion results
  task/infra status
  artifact refs
```

`RolloutRequest.seed` 是单次执行条件，对应 M7 的 `rollout_attempt_seed`；它不是 source lineage，也不控制 Task–World 物化。

TraceForge core 只定义稳定契约，不依赖 Harbor、E2B 或 AGS。仓库可以包含 `integrations/harbor_ags/` 可选实现，依赖方向只能是 integration 指向 core public contracts；Harbor Job/Trial、AGS 生命周期、Hermes 和 TokenHub 都不能进入 core。当前 M1A、M1B 不创建或调用该 integration。

## 16. 首个 MVP 规模

```text
输入：R01 全部 1,683 captures
画像：全量可用维度，显式报告各自 denominator
Domain：1 个由画像支持的业务 Domain
Family：1 个任务族
DomainKit：1 个 read-only kit
Canonical tools：画像后冻结，当前候选 search/open/read/grep/time
Hardness Operator：1 个
Smoke：3 个预分配 seed lineage
Pilot：10 个预分配 seed lineage
Variant：每个 seed 一个 canonical + 一个困难变体
Teacher：每个可认证任务默认 3 次
Target model：每个闭合任务默认 3 次
```

预分配 attempt 后不得按结果为某一组补样。构建失败、认证失败、Teacher 失败和 infra failure 都进入 attrition 账本。

## 17. 分阶段实施与验收

### M0：文档与数据快照

工作：

- 冻结背景、总体计划和开发规范入口；
- 固定 R01 path、digest 和 schema 事实；
- 建立 `.gitignore` 规则，防止真实数据和 artifacts 入库。

验收：新会话只读仓库文档即可准确说明目标、边界、输入和第一阶段任务。

### M1：Trajectory Compiler

M1 按以下门依次实施：

- M1A：流式来源适配、`SourceRecordRef` 和稳定来源账本；
- M1B：`NormalizedCapture`、`RequestBoundary`、EventLog、ActionBatch、pairing 和多维质量状态；
- M1C：`RequestLineageForest` 与 `CaptureRelationGraph`；
- M1D：`UserBlock`、`QueryTurn` 与 `ThreadTurnGraph`。

M1A、M1B v3 已正式通过，精确契约和验收见 [`r01-processing-spec.md`](r01-processing-spec.md) 与 [`r01-m1-v3-validation.md`](r01-m1-v3-validation.md)。M1C 已正式通过，契约与验收见 [`m1c-processing-spec.md`](m1c-processing-spec.md) 与 [`r01-m1c-validation.md`](r01-m1c-validation.md)；本次只实现 Grade-A 关系，Grade-B 可见前缀投影缓做。M1D 在其规格评审通过并单独验收前，不得宣称完成。

M1 总体验收：

- 1,683 条 capture 全部有终态；
- 不要求一次性加载 560 MB 文件；
- 同一输入产生稳定 ID 和字节稳定输出；
- 正常、缺 result、重复 result、冲突 schema、空 final 和多 capture fixture 有测试；
- 不保存 `reasoning_content` 原文，也不将其用于结构或语义判断，只生成固定审计摘要。

### M2：Distribution Profiler

工作：

- TaskEpisode DAG 与双独立语义确认；
- ObservedTaskDistribution；
- EnvironmentExposureProfile；
- lineage 去重与各维度 denominator；
- closed-label semantic extraction；
- ReconstructionCandidate。

验收：

- 结构事实由确定性代码产生；
- 语义模型输出带 event evidence，无法确认时 abstain；
- 报告 observed distribution，不作总体 prevalence claim；
- 人工 review 不是必需步骤。

### M3：首个 DomainKit 与 Task–World Compiler

工作：

- 根据 M2 冻结一个 Domain/Family；
- TaskIntent；
- WorldTemplate 和 WorldGraph；
- Public/Control；
- Truth resolver；
- Evidence graph；
- Reference；
- TaskWorldCandidateRevision。

验收：

- 同 `generation_seed` 和生成参数可复现；
- facts 不直接存 task answer；
- 至少存在一条 reference-pass 路径；
- Public 侧不含 Control 数据；
- 不依赖固定真实用户实体。

### M4：Certification、Rollout 与 Repair

工作：

- G0–G5、G7 静态门；
- mutation tests；
- alternate solution；
- RunnableTaskWorldCandidateBundle 导出；
- RolloutEvidence 与 EvaluationEvidence 导入；
- blind Teacher closure；
- RepairDiagnosis 和两轮上限。

验收：

- 每个发布 release 有完整 CertificationReport；
- Truth、Reference、Verifier 分离；
- 模型投票不能改变 Truth；
- 无法闭合的 candidate revision 被 quarantine；
- infra 不记为模型失败。

### M5：Difficulty Calibration

工作：

- 六维结构难度；
- RolloutEvidence 导入；
- criterion pass rate；
- 一个 Hardness Operator；
- canonical/hard matched variants。

验收：

- 难度变化对应明确维度和算子；
- 1/3、2/3 边界任务可识别；
- 过易、过难和 unscorable 分开；
- 难度修改后重新认证。

### M6：MVP 纵向闭环

工作：

- 3-seed smoke；
- 10-seed preallocated pilot；
- 完整 artifacts 与 attrition report；
- 输出通用 CertifiedTaskWorldRelease。

验收：

- 从 R01 source ref 到最终 release 可追溯；
- 所有 attempt 均在分母；
- 每个发布任务通过正式认证；
- 新会话能够用文档和 CLI 独立复现；
- 不以训练收益作为 MVP 完成条件。

### M7：来源闭合的现实—能力双环演化

启动门：M0–M6 全部完成并通过独立审核。M7 启动前仍以 README 的当前阶段为准；不得因本计划已记录目标而提前实现 Genome、GraphRAG、演化控制器或模型训练。

工作：

- 明确研究验证协议：使用未来 cohort 的物理隔离 evaluation-only 画像，或启动一次在 M2 前预注册 lineage 划分的独立 research rerun；当前全量 R01 不作为独立 holdout；
- 从版本化 `TaskEpisode`、`ObservedTaskDistribution`、`EnvironmentExposureProfile` 和 `ReconstructionCandidate` 派生首个 `TaskEnvironmentGenome` typed view；
- 建立只引用权威 artifact 的版本化演化记忆，先使用简单索引验证查询模式，GraphRAG 保持可选；
- 冻结第一代 `GenerationPlan`、source lineage 引用、`generation_seed`、白名单单轴算子、预算和预分配 `rollout_attempt_seed` 矩阵；
- 对一个 family 生成 canonical 加一个单因素 twin，并逐 revision 重算 Truth、执行 Reference、编译 Verifier，依次运行 G0–G5、G7，导出 `RunnableTaskWorldCandidateBundle`，再执行独立 rollout/evaluation 和 G6；
- 生成 criterion-level behavioral failure signature、算子效果证据、能力边界快照、Control 轨迹证书及其无泄漏安全投影；
- 以现实覆盖、质量多样性和能力信息增益共同决定第二代 `GenerationPlan`；
- 使用固定 fixture 和确定性 rollout 替身验证至少两代的状态转换；真实 pilot 按冻结规则形成下一代计划或结构化停止，并保留所有构建、认证、Teacher、Target、infra、无效应、不确定和淘汰记录。

验收：

- 第一代结构化证据无需人工挑选结果，按冻结的选择、并列决策和停止规则产生第二代 `GenerationPlan` 或结构化停止；研究上允许得到正向效应、无效应或不确定结论；
- 固定 fixture 与确定性替身路径连续完成至少两代，每代产生一个 `CertifiedTaskWorldRelease`，并引用具有 parent-child lineage 的新 `TaskWorldCandidateRevision`；真实在线 pilot 不因未产生正向能力差异而失败；
- 固定输入 evidence snapshot、`GenerationPlan` 和 `generation_seed` 必须字节稳定地产生 candidate；固定已捕获的 `RolloutEvidence`、`EvaluationEvidence` 和 verifier manifest 必须稳定重放 Evaluation/G6；在线模型调用只要求协议、manifest、预算、attempt 和原始 evidence 可审计，不承诺输出或通过状态完全复现；
- 每个修改后的 revision 都重新计算 `SemanticTruth`，按 G0–G5 + G7 → Runnable bundle → rollout/independent evaluation → G6 的顺序重新闭合；
- Reality evidence、派生规划和合成内容作逻辑与契约分域，Public/Control 保持物理分包，合成结果不回写观察分布；
- Target 失败、Teacher majority、GraphRAG 检索或模型归因均不能修改 Truth 或 Verifier policy；
- `generation_seed` 和 `rollout_attempt_seed` 预分配，所有 candidate 与 attempt 均在固定分母，attrition 报告包含每个阶段的拒绝原因；
- 单因素 twin 的配对 rollout 必须按预定义 criterion 报告效应、无效应或不确定；无论结果方向如何都保留固定分母，并按冻结规则更新下一代或停止，不能事后编造根因；
- Control `TrajectoryCertificate` 能把关键输出 claim 回指到实际 observation、Control World fact 和 state transition；其发布投影不含 Control fact、隐藏 criterion、Reference answer 或 operator 信息，并通过泄漏扫描；
- 外部 anchor/holdout 符合第 14.7 节协议；其 evaluation-only 画像物理隔离，原始 lineage 和评估结果永远不进入 `GenerationPlan`、规划侧 `ObservedTaskDistribution`、GraphRAG/演化记忆检索或训练数据；
- 达到预算、无合格候选或认证无法闭合时能确定性停止，不进行无界修复或 mutation；
- M7 的完成不依赖 SFT、DPO、RL 或模型权重提升，不以内部训练收益替代冻结 holdout 上的独立验证。

M7 之后若开展模型训练，必须另立阶段、冻结训练/评测 lineage 和外部 anchor，并把新模型作为新的不可变 manifest 重新进入能力环；这不反向扩大 M7 的授权范围。

## 18. 仓库映射

目录只在首次有真实实现时创建，不提前提交空包：

```text
traceforge/
├── AGENTS.md
├── README.md
├── docs/
│   ├── background-and-goals.md
│   ├── implementation-sources.md
│   ├── overall-plan.md
│   ├── reference-repositories.md
│   └── r01-processing-spec.md
├── scripts/
│   └── validate_m1_run.py
├── src/traceforge/
│   ├── cli.py
│   └── trajectory/
│       ├── artifacts.py
│       ├── compiler.py
│       ├── contracts.py
│       ├── json_codec.py
│       ├── pipeline.py
│       ├── source.py
│       ├── source_adapter.py
│       └── validation.py
└── tests/
```

上图是当前 M1A、M1B 的真实目录，不为 M1C、M2 或后续模块提交空包。`integrations/harbor_ags/` 也只在首次实现真实 Harbor 适配时创建。

稳定扩展点只保留已确认边界：来源适配器、DomainKit、Hardness Operator 和下游 Exporter。没有第二个真实实现前不建设插件框架。

## 19. 参考资源的使用边界

本节只保留架构级摘要。文件级处理链、已验证缺陷、提交和许可证见 [`reference-repositories.md`](reference-repositories.md)。

| 资源 | 可参考 | 不迁移 |
| --- | --- | --- |
| `seed2traj/search_audit` | canonical artifact、显式消息位置/tool ID、typed observation 等通用底层思路 | rubric、人工 review、badcase、LLM judge、旧 pipeline 状态和运行时代码 |
| AgentRx | Trajectory IR、静态/动态 invariant、失败分类 | LLM Judge 作为 Truth |
| Agent-World | world-first、execution-first、Reference/Verifier 思路 | 无回流约束的自由环境生成 |
| TRACE capability training | Family/capability 组织、self-test、难度 mutation | 直接套用有 Gold 的 pass/fail 前提 |
| Trace trajectory attribution | 归因作为假设、独立读取权威 context 再修复 | 使用 reasoning 或 LLM 归因生成 Truth |
| ASTRA | 工具图、环境编译和 self-test | 在缺失答案时直接执行 QA→Environment |
| EnvHarness | 已认证基础环境上的 Setup/Rules 和目标难度搜索 | 用它证明初始 World 正确，或执行任意生成代码 |
| `river-world` | Public/Control、Truth/Reference/Verifier 分离、artifact lineage | AW00 15-stage、固定 line 122、context-pressure pilot、整包复制 |
| GameCraft/Harbor | 下游任务执行与批量评测契约 | 任务和 World 的核心数据模型 |
| Multi-turn reliability | 复合 Query、constraint sharding 和 matched variants | 把用户模拟器当真实用户分布 |
| 腾讯 AGS/Hermes 指南 | 已验证 rollout 基线和 artifact 检查 | 沙盒与模型配置进入 TraceForge 核心 |

外部代码只允许选择性重写。若未来直接移植，必须按 AGENTS.md 记录来源、提交版本、许可证、原文件和本地修改。

## 20. 开发纪律

每次只推进一个模块：

```text
冻结输入输出
→ 先写正常与关键失败测试
→ 实现最小逻辑
→ 运行测试和静态检查
→ 检查 diff 与敏感数据
→ 中文小提交
→ 等待模块审核
```

不得以“整体框架需要”为理由提前实现后续模块。已删除的需求同步删除代码、测试和文档，版本历史交给 Git。
