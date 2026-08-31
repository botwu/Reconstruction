# TraceForge 背景与目标

版本：v0.1

日期：2026-08-31

状态：总体方向已确认；作为新开发会话的背景基线

## 1. 项目背景

真实企业 Agent 回流记录了用户请求、模型回复、工具调用、工具 observation、上下文包装和部分运行元信息。这些数据比从空白 Prompt 出发的纯模型造题更接近真实工作，但通常同时缺少：

- 可验证的标准答案；
- 完整的用户原始环境；
- 可靠的任务成功与用户满意度标签；
- 未访问资源、隐藏权限和服务端状态；
- 对模型、Harness、工具和环境责任的明确区分。

因此，TraceForge 不把回流轨迹直接改写成训练样本，也不声称恢复用户原环境。项目要解决的问题是：

> 如何从真实回流中提取可审计的任务和环境约束，再把这些约束编译成任务与 World 严格绑定、可执行、可验证、可调难度的合成任务单元。

最终目标不是单纯“生成更多难题”，而是持续生成位于目标模型能力边界、保留真实业务结构且具有可靠监督信号的任务与环境。

## 2. 当前数据基础

首批开发数据是本地 R01 回流：

```text
/Users/wujian1/Downloads/seed2traj/return_data/four_batch/by-rubric/R01.jsonl
```

该文件只作为本地受控输入，不进入 TraceForge Git 仓库。

### 2.1 已确认的数据事实

| 项目 | 当前值 |
| --- | ---: |
| JSONL capture 数量 | 1,683 |
| 文件大小 | 560,481,884 bytes |
| SHA-256 | `3832d8aa4ecd577636ce67b56d8798d9fb6311662a7bbce65814e5e8d260d4d8` |
| 数据时间范围 | 2026-07-20 至 2026-07-24 |
| thread/account lineage key 数量 | 956 |
| 存在多 capture 的 thread 数量 | 181 |
| 多 capture 覆盖记录数 | 908 |
| 单个 thread 最大 capture 数 | 32 |
| 历史 `seed2traj` 严格校验完整通过 | 670 |
| 历史 `seed2traj` 严格校验未通过 | 1,013 |

一条 JSONL 是一次 capture 或快照，不等于一个独立 Session，更不等于一个独立任务。同一 thread 的多个 capture 可能共享前缀、经历 compaction、形成分支或代表同一执行链的不同时刻。

### 2.2 输入结构

每条记录包含四个顶层字段：

```text
domain_meta
messages
meta
tools
```

- `domain_meta`：Rubric、任务摘要、风险和输入审计等后处理标注；
- `messages`：system、user、assistant、tool call 和 tool result；
- `meta`：capture、thread、request、模型、Harness、compaction 和规范化元信息；
- `tools`：该 capture 声明的 function schema。

数据没有顶层 `record_id`。TraceForge 必须根据数据快照摘要、行号和原始行摘要产生稳定的来源标识。

### 2.3 数据质量边界

当前审计还发现：

- 1,248 条记录存在未配对 tool call，共涉及 6,474 个唯一 call ID；
- 817 条记录的最后一个 assistant content 为空；
- 11 条记录存在重复 result ID；
- 349 条记录存在工具定义冲突；
- 280 条记录使用了推断补充的工具定义。

`meta.leaf_response_status=completed` 只说明响应捕获完成，不能解释为任务完成或答案正确。上表的 670/1,013 是旧审核管线的历史结果，不是 TraceForge M1 的 `processing_status`。未通过历史完整校验的记录也不能整体丢弃：用户请求可能仍可用于任务画像，部分 observation 可能仍可用于环境暴露画像。每个阶段必须独立判断 eligibility。

`reasoning_content` 原文不进入 M1 派生产物、任务画像、World 合成、GT、Verifier 或任何下游模型表面。M1 只为来源审计保存存在性、UTF-8 字节数、SHA-256 和原始 JSON pointer。

## 3. 我们能够从回流中利用什么

### 3.1 任务侧

可以观测和提取：

- 用户目标、约束和交付要求；
- 多轮补充、纠正、取消和范围变化；
- 子任务之间的依赖和组合方式；
- 任务族、业务场景和显式人物角色；
- 时间、版本、证据和引用要求；
- 实际出现的工作流和工具角色。

这些信息形成 `ObservedTaskDistribution`。该名称只表示当前 R01 cohort 中观测到的 Episode 分布，不代表线上总体的无偏真实分布。

### 3.2 环境侧

可以观测和提取：

- system 与 Harness 表面；
- 声明工具、实际调用工具和 schema；
- 文件、URL、数据库、实体等资源引用；
- observation 的类型、长度、错误、空结果和截断；
- read、write、edit、exec 等可见状态操作；
- compaction、上下文负载和工具恢复过程；
- 显式出现的权限、版本和时间线索。

这些信息形成 `EnvironmentExposureProfile`。它只描述模型实际看到的环境切片，不能证明未访问资源规模、真实噪声比例、隐藏权限、完整工具语义或真实副作用。

## 4. World 在系统中的作用

World 不是为了让任务“看起来复杂”的背景材料。它承担六项不可替代的职责：

1. 提供任务事实和确定性 Truth；
2. 规定 Agent 能观察和操作什么；
3. 提供真实的状态、权限、版本和时间关系；
4. 承载与任务相关的证据、自然邻域和受控噪声；
5. 让 Reference 和 Verifier 可以在同一初态上执行；
6. 提供可以被难度算子安全修改的结构。

环境重建分为三个层级：

```text
DomainKit
    通用工具语义、资源类型和运行规则

WorldTemplate
    一类业务场景的实体与关系结构

WorldInstance
    针对具体 TaskIntent 物化的事实、资源和状态
```

真实感主要来自关系一致性，而不是随机增加文件数量。人物、项目、消息、多版本资源、政策、权限、时间和干扰项必须处于同一个一致的 WorldGraph 中。没有回流证据的人物或背景可以合成，但必须明确标记为 `SYNTHETIC_CONTENT`，不能被报告为真实分布。

## 5. 核心方法立场

TraceForge 坚持以下不变量：

- Flowback 提供生成约束，不提供 Ground Truth；
- R01 是来源 cohort，不是业务 Domain；
- 一条 capture 不等于一个任务；
- 正确规模化单位是 Task/World Family，不是一条轨迹对应一道题；
- Task 与 World 必须作为同一个 candidate revision 联合构造和认证；
- Truth、Reference、Verifier 三者相互独立；
- Reference 证明至少存在一条可达解法，但不定义 Truth，也不限制其他合法解；
- 强模型 rollout 验证可达性、发现歧义和校准难度，不能通过多数投票产生 GT；
- Public World 与 Control/Oracle 必须隔离；
- 基础设施失败、任务无效、不可解和模型失败必须分开记录；
- 无法可靠判断时显式 `AMBIGUOUS`、`ABSTAINED` 或 `QUARANTINED`，不能伪造确定结论；
- MVP 自动管线不依赖人工 review，自动证据不足时选择弃权。

## 6. 总体目标

### 6.1 工程目标

建立一条可复现、可审计的纵向链路：

```text
真实回流 JSONL
→ Source/Capture/Request/Event 结构化
→ QueryTurn / TaskEpisode DAG
→ 任务与环境暴露画像
→ 重建候选选择
→ Task–World 联合合成
→ 静态认证与 Public/Control 分包
→ Rollout 可解性认证与定向纠正
→ 六维难度和模型边界校准
→ CertifiedTaskWorldRelease
```

所有派生产物必须具有明确版本、来源引用、稳定 ID、状态和失败原因。

### 6.2 研究目标

在工程闭环之后，逐步回答：

1. 回流条件化是否比 theme-only 造题覆盖更多真实任务和环境结构；
2. 哪些真实信号能够产生可验证而非任意的模型行为差异；
3. 结构难度和模型实测难度怎样共同定位能力边界；
4. 经认证的边界任务是否比未认证难题产生更高训练价值；
5. 新回流和模型能力变化怎样共同驱动 Task 与 World 的后续演化。

研究结果允许为负。不能根据结果反向修改分母、筛选规则或成功定义。

## 7. 首个 MVP 的范围

首个 MVP 聚焦 R01，但只选择一个由画像结果支持的真实任务族。当前优先候选是“多源证据调查与专业交付”，而不是泛化到所有 R01 任务。

MVP 包含：

- 全量 R01 capture 接入与 lineage 处理；
- SourceRecordRef、RequestBoundary 和 Immutable Visible EventLog；
- 后续审核通过后的 RequestLineageForest、QueryTurn 和 TaskEpisode DAG；
- `ObservedTaskDistribution`；
- `EnvironmentExposureProfile`；
- `ReconstructionCandidate`；
- 一个 read-only DomainKit；
- TaskIntent、WorldGraph 与 TaskWorldCandidateRevision；
- Truth、Reference、Verifier 和自动纠正；
- 六维结构难度和 rollout 难度证据；
- 面向下游执行的 `RunnableTaskWorldCandidateBundle`；
- rollout 闭合后的 `CertifiedTaskWorldRelease`。

TraceForge core 的 MVP 不包含：

- Harbor、E2B 或 AGS 运行时依赖；仓内可选 integration 可以独立提供 rollout 执行；
- 多 Domain；
- 恢复用户原始生产环境；
- 自动发现任意新工具；
- 写状态和真实外部副作用；
- 自动 Challenge Miner；
- SFT、DPO、RL；
- 多轮持续学习；
- `claw-eval` 兼容。

## 8. 两类难度问题

TraceForge 必须分开处理两种难度：

1. `ObservedDifficultyEstimate`：重建前根据原轨迹估计，用于判断某类 seed 是否值得重建；它带有模型、Harness 和缺失环境的混杂影响。
2. `CertifiedDifficultyProfile`：任务和 World 闭合后，根据结构特征与固定 rollout 协议实测，用于判断生成任务是否处于目标模型边界。

正式难度是六维向量，而不是单一“难题”标签：

```text
H = [H检索, H规划, H工具, H状态, H专业判断, H交付]
```

综合分只用于排序和展示，研究与纠正必须保留各维度证据。

## 9. 项目成功标准

工程成功意味着：

- 每条输入 capture 都被成功处理或留下结构化失败记录；
- Capture、Request、Event、Turn、Episode 和 candidate revision 的 lineage 可追溯；
- 分布统计具有明确去重单位、分母、未知项和适用范围；
- 每个发布任务都与唯一版本的 World、Truth 和 Verifier 绑定；
- Reference 可在重置环境中稳定执行；
- Verifier 能拒绝典型错误并接受合法替代解；
- rollout 失败不会被误记为 GT；
- 难度变化可以定位到具体算子和具体维度；
- 所有未通过任务留在 attrition 账本中，不按结果补样。

研究成功需要在工程成功之后另行验证。仅仅生成一批强模型能做、弱模型不能做的题，不足以证明真实回流条件化或自演化有效。

## 10. 新开发会话的阅读顺序

新会话开始开发前必须依次阅读：

1. [`../AGENTS.md`](../AGENTS.md)：唯一开发规范；
2. 本文：问题背景、数据事实和目标边界；
3. [`overall-plan.md`](overall-plan.md)：架构、模块、阶段和验收；
4. [`r01-processing-spec.md`](r01-processing-spec.md)：当前 R01 模块的精确输入、契约、输出和停止线；
5. [`reference-repositories.md`](reference-repositories.md)：参考项目的真实处理链、采用方式和禁止照搬项；
6. [`../README.md`](../README.md)：当前仓库状态和实际入口。

实现状态以代码、测试和 README 为准；目标架构以总体计划为准。两者不得混写。
