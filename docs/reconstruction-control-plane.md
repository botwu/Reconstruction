# 可控的 Agent World 重建框架

本项目的控制对象是一条 `attempt`。`session` 只用于切分和溯源，不能直接作为训练样本；一个 session 可以产生多个 attempt。每个 attempt 必须经过固定状态机：

```text
RECEIVED -> ANALYZED -> TASK_READY -> ENV_CANDIDATES
          -> ENV_SELECTED -> VERIFIER_READY -> RED_PASSED
          -> ROLLOUT_DONE -> CURATED
```

任意阶段都可以进入 `REVIEW`、`REJECTED` 或 `FAILED`。下游模块不能跳过状态，也不能覆盖已发布 artifact；重跑必须生成新的 run id 并记录父 run。

## 控制面

| 控制项 | 默认约束 | 放行条件 |
| --- | --- | --- |
| 输入边界 | 仅消费冻结的 attempt、report、evidence | evidence 引用可解析、hash 命中 |
| 模型调用 | temperature=0；模型名、超时、重试显式配置 | 响应符合 schema；不保存密钥 |
| 任务重建 | 只保留用户明示目标、约束和验收义务 | 原始用户文本可回溯；无 agent-only 注入 |
| 环境候选 | 最多 5 个；每个候选独立物化 | 文件路径安全、来源可追溯、无答案泄漏 |
| 环境充分性 | 独立只读判定 | `SUFFICIENT + READY` |
| Verifier | 隐藏测试、至少两个 oracle、一个 mutation | AST 校验通过、期望值独立计算 |
| RED-check | oracle PASS、no-op FAIL、mutation FAIL | 三类均通过；基础设施错误不算 FAIL |
| Hermes rollout | trials、并发、墙钟显式限制 | 轨迹完整、quality gate 通过 |
| SFT | pass-only | verifier PASS、无泄漏、质量和可复现性达标 |

## 模型权限

模型只负责语义推断和候选内容生成，不负责改变编排状态，不负责发布 artifact，不负责执行生成代码。所有模型输出都先经过 schema、evidence、路径和泄漏校验。环境生成模型看不到 hidden control；Verifier 模型可以读取受控的 withheld changes，但其输出只能进入隐藏控制目录。

## 人工介入

以下情况必须进入人工复核：失败状态不确定、episode 边界不确定、evidence 引用未知、环境候选互相矛盾、充分性为 UNKNOWN、Verifier RED-check 失败、Harbor quality gate 失败、rollout 不可复现。人工复核只修改 review decision artifact，不修改原始轨迹或模型原文。

## 论文对齐

Terminal-Universe 的三阶段被固定为：

1. 首次完整观察重放，后续 mutation 单独 withholding；
2. 生成最多五个 `solvable but NOT solved` 环境候选；
3. 用只读判官判断上下文充分性，然后才构造 Harbor bundle。

该机制位于 `reconstruction/terminal_universe_environment.py`，现有 `environment_completion.py` 负责 artifact runner 和 workflow 接缝。这样可以把论文机制替换为第二个环境后端，而不改变 Task/Verifier/Harbor 接口。

## 最小可观察指标

每次运行至少记录：候选生成数、候选 READY 数、充分性通过率、答案泄漏拒绝数、RED-check 三类通过率、Hermes 完成率、轨迹捕获率、SFT eligibility rate、人工复核率和每阶段模型调用/耗时。指标只从已发布 artifact 计算，不能由模型声明。
