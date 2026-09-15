# 重建筛选实施计划

更新时间：2026-09-14

本文是「重建筛选」的审查结论与实施计划。不以旧编号称呼上游阶段。筛选在轨迹编译、证据投影之前，只对入选样本进入后续重建。


## 一、现有代码审查

仓库里**没有**名为筛选的独立模块。相关逻辑散落三处，且都接在「已经编译完」之后。

### 1. 结构检查（`failure_analysis/extractor.py`）

对编译后的 capture 只查：事件序号、工具配对、记录是否合法结束。没看到工具结果记 UNCLEAR，不当失败。绝大多数真实轨迹得到：

```text
primary_failure=INCONCLUSIVE
failure_layer=UNCLEAR
recoverability=UNKNOWN
confidence=0.0
```

这是录像完整性检查，不是「任务失败、值不值得重建」。

### 2. 无模型门禁（`reconstruction/pipeline.py`）

```text
INCONCLUSIVE → DEFER
SYSTEM/USER 层 → REJECT
置信度≥0.75 且可恢复性高/中 → ELIGIBLE
其余 → REVIEW
```

结构检查几乎总是 INCONCLUSIVE，所以计划入口会把几乎全部样本推迟。它还要求先有编译 run，并**不会**调用单条重建。

### 3. 模型裁决（`failure_analysis/model_analyzer.py`）

这里才有重建 rubric（0–3）：

```text
task_identifiability
failure_evidence
initial_environment_visibility
environment_completion_value
verifier_constructability
episode_boundary_confidence
privacy_processability
estimated_cost
```

契约 `ReconstructabilityGateV1` 已定义，测试只校验字段范围，**没有任何流水线写出 gate**。`model-judge` 必须自带 report + evidence，且假设证据已从编译产物投影出来。

### 4. 文档与代码的冲突

`docs/p0-triage-rubric.md` 写过正确的业务门槛（有用户任务、有尝试、有失败/未完成信号、能定位 attempt），但实现顺序是「先全量编译再筛」。R01 原文即可做轻量扫描：`messages` / `meta.leaf_response_*` / `source_request_count`。第一条记录是 restored-long 四元组，不一定带 `turn_quality_meta`。

### 5. 审查结论

| 问题 | 结论 |
|------|------|
| 有没有筛选？ | 有碎片，没有产品入口 |
| 能不能筛出任务失败？ | 不能 |
| 顺序对不对？ | 不对，现在先编译再筛 |
| 能否复用？ | 复用 rubric 键、GateDecision、ChatModel；不要复用「先编译」的编排 |


## 二、目标设计

```text
原始 JSONL（一行 = 一次 capture）
  → 规则粗筛（确定性，不调模型）
  → 模型细筛（只对规则通过的样本；第二期）
  → SelectionManifest
        ELIGIBLE  → 才做轨迹编译 / 证据投影 / 回放 / 重建
        REVIEW    → 人工或下一批模型
        DEFER     → 暂缓（成本、快照、缺依赖）
        REJECT    → 丢弃
```

筛选单位第一期：**一条 capture（一行 JSONL）**，目标问题取最后一个 USER。同一 thread 多 capture 的合并放到更后。

规则不能单独给出 ELIGIBLE：`leaf_response_status=completed` 只表示抓取完成，不等于任务成功。第一期规则只做 REJECT / REVIEW / DEFER，并把特征留给模型。


## 三、分期实施

### 第一期：规则筛选入口（已落地）

- 模块 `traceforge.screening`，直接读 JSONL。
- CLI：`traceforge screening run --input ... --output ... --rules-only`
- 产物：`selection_manifest.json` + `private/records.jsonl`

### 第二期：规则 + 必要模型细筛（本次落地）

- Rubric 与门禁：`src/traceforge/screening/rubric.py`，说明见 [reconstruction-screening-rubric.md](reconstruction-screening-rubric.md)。
- 对规则 REVIEW 且 `rule_pass=true` 的样本调用 `ChatModel`。
- 可观察轨迹序列化：按 user span 切分，发给模型 **全部** span 的可观察轮次（含 tool 结果）；超长字段机械截断；不默认 omit 更早任务。
- 模型返回 `tasks[]`；`admit_after_tasks`：任一失败/未完成且 R1≥2、R2≥2、`code_file` 的任务过线则整条 ELIGIBLE，并记录 `selected_span_ids`。
- 终态由门禁决定，模型不能推翻规则硬拒绝。
- 入选硬门槛（按任务）：意图能拟合（R1≥2）、没做成（`FAILURE`/`INCOMPLETE` 且 R2≥2）、环境有抓手（`code_file`）、`needs_reconstruction=true`。过长仍由规则层 DEFER。检索仍 DEFER。
- 默认 CLI 调模型；`--rules-only` 只跑规则。
- 普通 CI 只用替身模型，不打真实 API。

### 第三期：接到重建

- 只对 ELIGIBLE 调编译 / 投影 / 回放 / `reconstruct workflow`。
- `reconstruct pipeline` 的旧门禁改为消费 SelectionManifest，不再用 INCONCLUSIVE→DEFER。


## 四、第一期规则

硬拒绝 REJECT：

- 行无法解析为对象
- 没有 USER 消息
- 没有 Agent 尝试（无 assistant，且无 tool / tool_calls）

暂缓 DEFER：

- `source_request_count > 20` 或消息数 > 200（成本过高，留给抽样/人工）

其余可解析样本 REVIEW：

- `rule_pass=true`，等待模型
- 记录失败/未完成信号，供第二期优先打分：`leaf_response_error`、`leaf_incomplete_details`、最后一条 assistant 空、工具调用缺少 result

不在第一期做：隐私深度检测、TaskEpisode 切分、独立 critic、按 code_file/retrieval 自动 ELIGIBLE。


## 五、验收

第一期：

- `pytest tests/test_screening.py tests/test_cli.py` 中与 screening 相关用例通过
- `ruff check` 覆盖新文件
- 对 R01 可用 `--limit` 跑出 manifest，统计 REJECT/REVIEW/DEFER
- 不产生全量编译产物

第二期以后才能声称「已筛出入选重建样本」。
