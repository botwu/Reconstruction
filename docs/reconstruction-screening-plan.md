# 重建筛选实施计划

> 官方路径是 `screening run` → `reconstruct run`。轨迹编译栈已删除。

更新时间：2026-09-15
状态：第二期已落地并冻结为 `reconstruction-screening-v9`。筛选不再调尺；下一阶段是把 ELIGIBLE 接到真正重建。

权威尺：[reconstruction-screening-rubric.md](reconstruction-screening-rubric.md)
下一阶段入口：[rebuild-live-map.md](rebuild-live-map.md)


## 已冻结流程

```text
原始 JSONL（一行 = 一次 capture）
  → 规则粗筛（坏 JSON / 无用户 / 无尝试 → REJECT；过长 → DEFER；其余 REVIEW）
  → 模型细筛（整条可观察轨迹 → tasks[]）
  → SelectionManifest
        ELIGIBLE  → 才做轨迹编译 / 证据投影 / 回放 / 重建
        REVIEW    → 通道失败或分数不够；不是「不可重建」证明
        DEFER     → 成本过高，暂缓
        REJECT    → 无任务、无尝试，或整条都已做好
```

单位：一条 capture（一行 JSONL）。整条里任一失败/未完成任务过线即入选，记录 `selected_span_ids`。
规则不能单独给出 ELIGIBLE。`leaf_response_status=completed` 只表示抓取完成，不等于任务成功。


## 冻结的入选尺

硬门槛按 task：

- R1 `task_identifiability` ≥ 2（有效任务）
- outcome 为 FAILURE / INCOMPLETE，且 R2 `failure_evidence` ≥ 2（没完成或完成不好）
- `needs_reconstruction=true`

工具与 `domain_route` 只辅助，**不是**入选条件。缺必要工具也要入选。检索不再因领域 DEFER。


## 分期

### 第一期：规则筛选入口（已落地）

- 模块 `traceforge.screening`，直接读 JSONL
- CLI：`traceforge screening run --input ... --output ... --rules-only`
- 产物：`selection_manifest.json` + `private/records.jsonl`

### 第二期：规则 + 模型细筛（已冻结）

- 实现与说明见 rubric 文档
- 可观察轨迹：全部 user span；超长字段机械截断；不 omit 更早任务
- 模型返回 `tasks[]`；`admit_after_tasks` 任一过线则整条 ELIGIBLE
- 默认 CLI 调模型，`--concurrency` 默认 8；`--rules-only` 只跑规则
- 普通 CI 只用替身模型

### 第三期：接到重建（当前工作）

- 只对 ELIGIBLE 抽行 → `reconstruct run`（可选先 `reconstruct source`）
- 旧的 `reconstruct pipeline` / `prepare` / `workflow` 已删除，不再当重建入口
- 细节见 [rebuild-live-map.md](rebuild-live-map.md)
