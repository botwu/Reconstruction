# 轨迹分析方法复用

TraceForge 将回流轨迹分析拆成两个互补阶段：

- **AgentRx 单轨迹诊断**：把轨迹规范化为 `trajectory_id/instruction/steps/substeps`，先生成静态约束，再对每个前缀生成动态约束，最后按“首个未解决失败”算法做根因分类。`python_check` 只产生 `NEEDS_SANDBOX`，`nl_check` 在证据不足时保持 `UNCLEAR`；生成代码不会在主机执行。
- **TRACE 跨轨迹能力分析**：先 discovery 得到通用能力，再由相互独立的 labeling run 标注 `NA/PRESENT/LACKING`，最后按 coverage、contrastive gap 和 consistency 阈值筛选能力。

上游方法来自 AgentRx commit `f228165b` 和 TRACE commit `d2db2308`，均为 MIT。原始 taxonomy、根因 prompt、TRACE Phase 2 prompt 和许可证保存在 `reference_assets/`、`third_party/`；`reference_prompts.provenance()` 提供源文件和资产 SHA-256。运行时不依赖上游仓库，也不执行上游 runtime。

## 入口

Python 调用：

```python
from traceforge.failure_analysis.agentrx_pipeline import run_agentrx_diagnosis
report = run_agentrx_diagnosis(trajectory_ir, model)
```

命令行：

```bash
traceforge failure-analysis agentrx \
  --trajectory-json trajectory.json --output agentrx_report.json
traceforge failure-analysis capabilities-aggregate \
  --runs-json labeling_runs.json --outcomes-json outcomes.json \
  --output capability_metrics.json
```

`outcomes.json` 只有人工或外部 verifier 明确确认的 `SUCCESS/FAILURE` 才能进入 TRACE 对照组。缺少任一组时，聚合结果为 `UNAVAILABLE_MISSING_OUTCOME_LABELS`，指标为 `null`，不会用零分母伪造 Δ。

## 与重建闭环的边界

AgentRx 的诊断结果用于定位根因和提供 evidence refs；它不会直接决定任务是否可以重建。任务/环境重建仍需通过现有的证据引用、环境候选充分性、隐藏 verifier、Harbor 质量门禁和 RED-check。TRACE 的能力指标只用于选择训练方向，不把失败轨迹自动当作失败标签。
