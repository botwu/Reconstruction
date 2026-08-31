# TraceForge

TraceForge 是一个将真实回流轨迹转化为可验证任务与环境，并进一步进行可解性认证、难度纠偏和模型边界搜索的框架。

## 开始之前

新开发会话必须依次阅读：

1. [AGENTS.md](AGENTS.md)：唯一开发规范；
2. [当前会话交接](docs/session-handoff.md)：当前检查点、证据、停止线和下一步；
3. [背景与目标](docs/background-and-goals.md)：问题背景、数据事实、目标和主张边界；
4. [总体实施计划](docs/overall-plan.md)：架构、模块、数据契约、阶段和验收；
5. [R01 回流处理实施规格](docs/r01-processing-spec.md)：当前模块的输入、契约、输出与停止线；
6. [R01 M1 v3 验收报告](docs/r01-m1-v3-validation.md)：当前 M1A/M1B 正式验收事实来源；
7. [参考仓库处理逻辑](docs/reference-repositories.md)：已有项目的真实处理链、采用方式和禁止照搬项；
8. [M1 实现来源与迁移记录](docs/implementation-sources.md)：旧轨迹审核代码的逐文件来源、采用项和剥离项；
9. [R01 M1 v2 历史验收报告（结论已撤销）](docs/r01-m1-v2-validation.md)：保留全量运行事实，不作为当前完成证据；
10. [R01 M1 v1 历史验收报告（结论已撤销）](docs/r01-m1-validation.md)：保留正常路径历史事实，不作为当前完成证据。

## 核心链路

```text
真实回流 JSONL
→ SourceRecordRef / RequestBoundary
→ Immutable Visible EventLog
→ RequestLineageForest / QueryTurn
→ TaskEpisode DAG
→ ObservedTaskDistribution
  + EnvironmentExposureProfile
→ ReconstructionCandidate
→ TaskWorldCandidateRevision
→ Truth / Reference / Verifier
→ G0–G5 + G7
→ RunnableTaskWorldCandidateBundle
→ Rollout / G6
→ 六维难度与模型边界
→ CertifiedTaskWorldRelease
```

## 当前阶段

M1A/M1B v3 已正式通过。三项 P1 及脱敏 Data URL 终态的同步重签变体已经闭合；冻结 R01 已在干净代码冻结点完成两次独立全量编译、两次 validator、确定性对比和轻量留证。输入 `source_schema` 仍是 `traceforge.restored-long-capture.v1`。当前仍停止在 M1B，M1C 尚未启动。

当前实现边界：

```text
R01 JSONL
→ SourceRecordRef
→ NormalizedCapture / RequestBoundary
→ Immutable Visible EventLog
→ ActionBatch / ToolPairing
```

当前实现仍停止在 M1B；尚未实现跨 capture 建图、QueryTurn、TaskEpisode、任务画像、World、认证、难度或 Harbor 接入。进入 M2 前必须单独冻结并审核最小、带来源的 `SourceAnnotationProjection` 或只读 `SourceResolver`。

## 运行当前编译器

```bash
uv sync --dev --python 3.12
uv run traceforge trajectory compile \
  --input <R01.jsonl> \
  --dataset-id r01-four-batch-202607-v1 \
  --source-schema traceforge.restored-long-capture.v1 \
  --expected-sha256 <frozen_sha256> \
  --output artifacts/r01
```

运行单元测试和静态检查：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

编译结果采用内容寻址目录。确定性业务产物、私有事件表与公共聚合报告物理分离；真实产物已由 `.gitignore` 排除。

编译器和 validator 不内置 R01 的路径、摘要或统计值。`source_schema` 显式声明语义输入契约：当前只有一个 restored-long adapter，不会猜测或尝试多种 JSON 结构。不支持的 schema 在读取来源和创建 staging 前整批失败。validator 只重算并检查已发布 run 的通用契约：

```bash
uv run python scripts/validate_m1_run.py <content_addressed_run_dir>
```

R01 摘要和统计只作为文档化的外部验收基线。若后续需要机器比较，必须由调用方显式提供独立 expectation/profile，不能把特定数据常量写进核心或通用 validator。validator 只依据已发布结构独立重算 boundary ownership、message/event 覆盖、ActionBatch、完整 pairing 状态、CaptureQuality 和 attrition report，不信任 pairing、quality 或 report 的自报语义。

M1 v3 不保存旧轨迹任意深度的 `reasoning_content` 原文，也不把它作为语义输入；只保留固定审计摘要，并从可见指纹中递归排除。完整 Base64 Data URL 使用版本化隐私 envelope 摘要，孤立的普通文本 `;base64,` 不视为 Data URL。

## 核心边界

- 回流提供生成约束，不提供 Ground Truth；
- 一条 capture 不等于一个 Session 或任务；
- R01 是来源 cohort，不是业务 Domain；
- 合成 World 不声称恢复用户原环境；
- Task 与 World 必须绑定后共同认证；
- 强模型 rollout 不通过多数投票产生 GT；
- TraceForge 核心不依赖 Harbor；Harbor/AGS 只能作为仓内独立可选集成接入稳定契约；
- 真实数据、运行结果、模型缓存和参考仓库不进入本仓库；
- `claw-eval` 不属于项目范围。

## 开发规范

所有开发工作遵循 [AGENTS.md](AGENTS.md)。README 和设计文档不重复定义代码风格、测试纪律或 Git 规则。

## 参考资源

`refer_repo` 当前可见的旧 `seed2traj`、TRACE、ASTRA、AgentRx、EnvHarness、QC_postprocess、固定任务 artifact、相关论文与运行指南仅作为只读参考。项目采用选择性重写，不整体复制历史实现。此前讨论但当前快照缺失的 AgentHER、CSO 和 GameCraft-Bench 暂不作为实现依据。具体文件和处理边界见 [参考仓库处理逻辑](docs/reference-repositories.md)。
