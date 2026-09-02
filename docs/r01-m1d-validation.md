# R01 M1D 全量验收报告

日期：2026-09-03。本报告记录 `query-turn-compiler-m1d-v1`（提交 `1c588be`，规格 [`m1d-processing-spec.md`](m1d-processing-spec.md) v0.3）在已正式验收的 M1B v4 run `6be45e01…` 之上的两次独立全量构建与独立双参 validator 结果。开发纪律只引用 [`../AGENTS.md`](../AGENTS.md)；验收流程与 M1C 同构（[`r01-m1c-validation.md`](r01-m1c-validation.md)）。

## 1. 结论

代码提交 `1c588bed6cfbb696611823ce427fdaa8fd06e249`（树 `2bc0f4bc4da4eadeb3fbcfa3a187618d3e6669f8`，`dirty=false`）在本地盘干净克隆上，对已发布 M1B v4 run `6be45e01…` 完成两次独立全量构建。两次均复现内容寻址 run ID `84d826b3…`（该 ID 只绑定 `m1b_run_id` + 其 `artifact_manifest` SHA-256，与审核方会话在评审整改前单跑得到的 ID 一致）；两次 `artifact_manifest.json` 摘要一致，排除非确定性的 `run_receipt.json` 后递归 diff 无差异；两次 run 均通过独立双参 validator（`ok=true`，8 files）；13 项聚合计数与 [`v4-completion-handoff.md`](v4-completion-handoff.md) §4 的审核向量逐项吻合，且与 M1D 在 M1B **v3** 冻结 run 上的计数完全一致（M1B v4 对 M1D 产物字节中性的预期成立）。

因此 `query-turn-compiler-m1d-v1` 认定为 M1D 正式通过。结论只覆盖：在已发布 M1B run 之上确定性地派生 `UserBlock`／`AgentStep`／`AssistantOutcome`／`QueryTurn`／capture 内 `STRUCTURAL_NEXT_TURN` 结构边，并对不可定位前缀与孤儿观测记账。不包含任何语义关系、`TaskEpisode`、跨 capture 线程合并或前缀内容还原（规格 §11，留 M2）。

## 2. 输入、版本与运行身份

| 项目 | 验收值 |
| --- | --- |
| 输入 | 已发布 M1B v4 run `6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed`（[`r01-m1b-v4-validation.md`](r01-m1b-v4-validation.md)） |
| 输入 `artifact_manifest` SHA-256 | `179b82efca85fc6132bfa806d45b081729b0d5627878b0d517e978070fe99eb1` |
| 输入语义契约 / compiler contract | `traceforge.restored-long-capture.v1` / `trajectory-compiler-m1ab-v4` |
| query-turn contract | `query-turn-compiler-m1d-v1` |
| TraceForge / Python | `0.3.0` / `3.12.13` |
| Git commit | `1c588bed6cfbb696611823ce427fdaa8fd06e249` |
| Git tree | `2bc0f4bc4da4eadeb3fbcfa3a187618d3e6669f8` |
| 内容寻址 M1D run ID | `84d826b3d7abddffb5f8fde28d7e5592590720bd6909c24a8b17b7f0c70b66ea` |
| M1D `artifact_manifest` SHA-256 | `56a3befe44bec087a52aaa680f592eb71e196c7d7bd946e47c172540cb190bfe` |

M1D 不读取原始 R01 JSONL，只读消费已发布 M1B run（reader 先跑 M1B 权威 `validate_compiled_run`，再抽取冻结标量）。两次回执均记录 `git_provenance.available=true`、`dirty=false`、`git_provenance_verified_at_completion=true`，并绑定同一 commit、tree、run ID 与 manifest 摘要。

## 3. 双全量运行门禁

| 验收项 | 第一次 | 第二次 |
| --- | ---: | ---: |
| 构建回执时长 | 211.258606 秒 | 202.558055 秒 |
| 墙钟（含解释器启动） | 224.529893 秒 | 217.494569 秒 |
| 峰值 RSS | 244,621,312 bytes（233.3 MiB） | 244,928,512 bytes（233.6 MiB） |
| 512 MiB 停止线 | 通过 | 通过 |
| 独立双参 validator | `ok=true`，退出码 0，8 files | `ok=true`，退出码 0，8 files |

两次 run ID 完全相同；排除 `run_receipt.json` 后递归 diff 退出码 0 且无输出。产物 14 MB：六张私有表共 21,722 行（`agent_steps` 14,375、`query_turns` 2,610、`capture_turn_accounting` 1,683、`assistant_outcomes` 1,200、`user_blocks` 927、`thread_turn_edges` 927）。

## 4. 与审核向量及 v3 基线的逐项对照

| 计数 | validator 观测 | 审核向量（handoff §4；与 v3 run 一致） |
| --- | ---: | ---: |
| `capture_count` | 1,683 | 1,683 |
| `query_turn_count` | 2,610 | 2,610 |
| `prefix_rooted_turn_count` | 1,683 | 1,683 |
| `observed_rooted_turn_count` | 927 | 927 |
| `complete_turn_count` | 1,200 | 1,200 |
| `incomplete_turn_count` | 1,410 | 1,410 |
| `agent_step_count` | 14,375 | 14,375 |
| `user_block_count` | 927 | 927 |
| `assistant_outcome_count` | 1,200 | （含于 complete 1,200；R01 无 `EMPTY_OUTCOME`） |
| `thread_turn_edge_count` | 927 | 927 |
| `orphan_tool_observation_count` | 324 | 324 |
| `captures_with_compaction_count` | 119 | 119 |
| `captures_with_unlocalizable_prefix_count` | 1,683 | （向量未列；与规格 §0 事实一致，见下） |

审核向量中的 `eligible_capture_count` / `quarantined_capture_count` 在 v0.3 已删除（M1D 输入全集恒为 M1B 已编译 capture，M1B validator 断言其 `COMPLETE`；quarantine 分母属 M1B attrition report，规格 §6），不再是报告键。

结构守恒关系（由已发布表直接可验）：`prefix_rooted_turn_count = capture_count = 1,683`——每个 capture 的观测窗口都以 boundary-0 的终止 assistant 起头，故恒有且仅有一个 `PREFIX_ROOTED` 首回合，且 100% capture 存在不可定位前缀（规格 §0 事实 2）；`observed_rooted_turn_count = user_block_count = thread_turn_edge_count = 927 = query_turn_count − capture_count`——每个可定位 UserBlock 恰起一个回合、每个非首回合恰有一条来自前驱的结构边；`assistant_outcome_count = complete_turn_count = 1,200`——R01 观测窗口内无 `EMPTY_OUTCOME`，1,410 个 `INCOMPLETE` 回合全部是"末步为工具调用步"或 USER-only 回合。

## 5. 验收方法与 validator 信任边界

validator 取双参 `<m1d_run> <m1b_run>`，分两层（规格 §8）：

- **篡改检测**：从 M1B 已发布字段重建输入视图，调用与 pipeline 同一个纯 fold `build_query_turn_graph` 重建期望，六张私有表逐一与 `private/` 双向集合相等（canonical 字节比对）。
- **正交不变量（不经 fold）**：每 capture 观测事件 ID 集合恰被 UserBlock/AgentStep(assistant)/工具观测/孤儿/SYSTEM/TOOL_CALL(经 batch) 划分覆盖，无遗漏、无重复、kind 相符、不引用前缀事件；记账逐 kind 计数与 M1B 视图相等；capture 末 assistant 的结构映射与 M1B `CaptureQualityV3.terminal_status` 一致；报告计数由已发布表独立重算。R01 全量 175,858 事件（其中观测窗口内事件全部落入划分）在本次 run 上均通过上述断言。

反向核对：以 M1B **v3** 冻结 run `519a86d3…` 作 oracle 校验本 run → 退出码 1、`M1B_INPUT_INVALID`（v4 reader 对 v3 run 按设计 fail-closed），validator 不会在错误上游上冒充通过。

## 6. 跨会话产物对照

审核方会话曾在评审整改前的未提交 M1D 工作树上单跑一次同输入（产物留在 `/tmp/tf_v4_audit/m1d/`）。与本次正式 run 逐文件比对：`user_blocks` / `agent_steps` / `assistant_outcomes` / `query_turns` / `thread_turn_edges` 五张业务表与 `query_turn_manifest.json` **逐字节相同**；`capture_turn_accounting.jsonl` 仅差被删除的 `processing_status` 字段（去掉该字段后 1,683 行完全相等）；`reports/m1d_report.json` 仅差被删除的 `eligible_capture_count` / `quarantined_capture_count` 两键，其余键值相等；`artifact_manifest.json` 因内嵌上述两文件的摘要与长度而异。评审整改（D1–D5、validator 重构）未改变任何业务派生结果。

## 7. 测试与静态门禁

提交前门禁：`test_m1d_*` 55 项通过（三种 capture 形态、中途插话、孤儿观测、SYSTEM 断开 UserBlock、前缀记账守恒、观测事件分区守恒属性、D-a"结果全到仍 INCOMPLETE"、空观测流、确定性；reader 身份绑定与 fail-closed；pipeline 产物结构与双构建逐字节一致；validator 篡改重签 bijection、置空 bijection 后不变量层仍 fail-closed 且合法 run 零误报、错误 oracle、隐私兜底、报告闭合）；全量 273 项通过；Ruff lint 与 format 通过。

## 8. 轻量验收证据

正式 run 保存在本地：

```text
artifacts/r01/query_turns/84d826b3d7abddffb5f8fde28d7e5592590720bd6909c24a8b17b7f0c70b66ea/
```

必要证据保留在 Git 忽略目录：

```text
artifacts/r01/acceptance_m1d/84d826b3d7abddffb5f8fde28d7e5592590720bd6909c24a8b17b7f0c70b66ea/
├── run_a_receipt.json / run_b_receipt.json
├── run_a_artifact_manifest.json / run_b_artifact_manifest.json
├── build_a.json / build_b.json                 # 退出码、墙钟、峰值 RSS
├── validation_a.json / validation_b.json       # ok、8 files、13 项计数
├── determinism.json                            # 双跑 diff、跨会话逐文件对照、错误 oracle 结果
├── measurements.json
└── wrong_oracle_v3_validation.txt
```

真实 run 和验收证据均不进入 Git。

## 9. 环境已知项（非代码缺陷，登记）

与 [`r01-m1c-validation.md`](r01-m1c-validation.md) §7 相同：AFS 共享盘上 `git status` 超过 `provenance.py` 的 5 秒超时，就地 run 会被 validator 以 `RUN_RECEIPT_GIT_PROVENANCE_UNVERIFIED` 拒绝；正式 run 因此在本地盘干净克隆 `/tmp/tf-m1d`（提交 `1c588be`，`git status` 0.4 秒）上执行，M1B 输入 run 只读自 AFS `artifacts/`。构建耗时约 3.5 分钟、validator 约 3.5 分钟，主要花在 M1B 权威 validator 对 706 MB 上游 run 的重哈希与重算上，属先校验后消费的必要成本。

[`m1ab-v3-known-items.md`](m1ab-v3-known-items.md) R9（`conftest.py` 夹具先于其引用的 `query_turns` 模块提交）随本次 M1D 提交 `1c588be` 自愈：干净克隆上完整 pytest 不再有 `ModuleNotFoundError`。

## 10. 停止线

本次正式结论停止在 M1D。M1 主线（M1A/B v4 → M1C v2 → M1D v1）在 R01 上全部正式通过，且三者的 run 以内容寻址身份链式绑定：`84d826b3…` ← `6be45e01…` → `9ff708d9…`。进入 M2 前仍须先冻结最小、带来源的 `SourceAnnotationProjection`（[`m2-source-projection-spec.md`](m2-source-projection-spec.md) 待评审；[`r01-processing-spec.md`](r01-processing-spec.md) §8.3）。
