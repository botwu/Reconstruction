# TraceForge

TraceForge 是一个将真实回流轨迹转化为可验证任务与环境，并进一步进行可解性认证、难度纠偏和模型边界搜索的框架。

## 核心链路

```text
原始回流 JSONL
→ 事件账本
→ InteractionTurn
→ TaskEpisode DAG
→ 任务与环境重建
→ 可解性认证
→ 难度纠偏
→ 模型边界校准
```

## 当前阶段

项目处于框架初始化阶段。第一阶段将先冻结整体模块边界和数据契约，同时完成轨迹结构化模块：

```text
Raw JSONL
→ Immutable EventLog
→ InteractionTurn
→ TaskEpisode DAG
→ GoalGraph
```

其余模块先建立清晰边界，随后按模块逐步实现，不迁移无关历史包袱。

## 开发规范

所有开发工作遵循 [AGENTS.md](AGENTS.md)。该文件是开发原则、代码风格、测试纪律和 Git 规范的唯一事实来源。

## 范围约束

- 旧 `seed2traj`、`river-world`、TRACE、ASTRA、AgentRx 仅作为只读参考来源。
- 采用契约优先的选择性重写，不整体复制参考仓库。
- 真实回流数据、运行结果、模型缓存和研究资料不进入本仓库。
- `claw-eval` 不属于本项目范围。
