# 轨迹分析方法复用

TraceForge 把回流轨迹分析拆成两个互补阶段。它们**不**决定一条 capture 能不能重建，也不写 Intent 的 q。

- **AgentRx 单轨迹诊断**：把轨迹规范化为 `trajectory_id/instruction/steps/substeps`，先生成静态约束，再对每个前缀生成动态约束，最后按「首个未解决失败」做根因分类。`python_check` 只产生 `NEEDS_SANDBOX`，`nl_check` 在证据不足时保持 `UNCLEAR`；生成代码不会在主机执行。prompt 版本：`agentrx-static-v1` / `agentrx-dynamic-prefix-v1` / `agentrx-failure-judge-v1`。
- **TRACE 跨轨迹能力分析**：先 discovery 得到通用能力，再由相互独立的 labeling run 标注 `NA/PRESENT/LACKING`，最后按 coverage、contrastive gap 和 consistency 阈值筛选能力。prompt 版本：`trace-capability-selection-adapter.v1`。

上游方法来自 AgentRx commit `f228165b` 和 TRACE commit `d2db2308`，均为 MIT。原始 taxonomy、根因 prompt、TRACE Phase 2 prompt 和许可证保存在 `reference_assets/`、`third_party/`；`reference_prompts.provenance()` 提供源文件和资产 SHA-256。运行时不依赖上游仓库，也不执行上游 runtime。

## 入口

Python：

```python
from traceforge.failure_analysis.agentrx_pipeline import run_agentrx_diagnosis
report = run_agentrx_diagnosis(trajectory_ir, model)
```

命令行（`--config` / `--channel` 见 [model-gateway-config.md](model-gateway-config.md)）：

```bash
PYTHONPATH=src python -m traceforge failure-analysis agentrx \
  --trajectory-json trajectory.json --output agentrx_report.json \
  --config config.yaml --channel gemini

PYTHONPATH=src python -m traceforge failure-analysis model-judge \
  --report-json report.json --evidence-json evidence.json --output judge.json

PYTHONPATH=src python -m traceforge failure-analysis review-batch \
  --input-jsonl reports.jsonl --output artifacts/review-batch

PYTHONPATH=src python -m traceforge failure-analysis capabilities-aggregate \
  --runs-json labeling_runs.json --outcomes-json outcomes.json \
  --output capability_metrics.json
```

`outcomes.json` 只有人工或外部 verifier 明确确认的 `SUCCESS/FAILURE` 才能进入 TRACE 对照组。缺少任一组时，聚合结果为 `UNAVAILABLE_MISSING_OUTCOME_LABELS`，指标为 `null`，不会用零分母伪造 Δ。

## 与重建闭环的边界

AgentRx 只定位根因、提供 evidence refs。它不能：

- 改筛选尺或把 REVIEW/DEFER 改成 ELIGIBLE
- 代替 Intent 写出 q（原始 query 作锚点、同目标 FILE 深化）
- 代替 Stage1 回放或 Completion 补 E
- 代替 `workspace_sufficiency` 判断 `(q, E)` 是否可解
- 代替 FILE Verifier / Harbor RED

原始会话通过 `reconstruct raw-run` 直接进入重建。terminal 的文件验收与校准、search 的检索环境按各自领域执行。TRACE 的能力指标只用于选择训练方向，不把失败轨迹自动当成失败标签。
