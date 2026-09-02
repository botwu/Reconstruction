# R01 M1C 全量验收报告

日期：2026-09-02（v1）；2026-09-03（v2）；2026-09-03（v2 重绑定 M1B v4）

范围：M1C 跨 capture 关系图（`RequestLineageForest` + `CaptureRelationGraph`，全 Grade-A）

状态：**v2（`lineage-compiler-m1c-v2`）在 M1B v4 run `6be45e01…` 上的 lineage run `9ff708d9…` 正式通过，为当前有效结论（§0′）。§0 的 run `978c0347…` 绑定已被 v4 取代的 M1B v3 run，保留为历史；v1 结论由 v2 取代，其运行事实保留于 §1–§7。**

## 0′. v2 重绑定 M1B v4 验收（2026-09-03，当前有效）

### 0′.1 动机

M1B v4（[`r01-m1b-v4-validation.md`](r01-m1b-v4-validation.md)）改变了 compiler contract 与四张事件表 schema，产出新的 M1B run `6be45e01…`。M1C 代码零运行时变化（`src/traceforge/lineage/` 相对 `fcff8cf` 仅 `reader.py` 模块文档字符串中的契约名 `V2→V3` 两行），但 lineage run ID 与所有节点/边 ID 按规格 §5.6 以 `m1b_run_id` 命名空间化，因此必须在新 M1B run 上重跑并补验收。

### 0′.2 运行身份

| 项目 | 验收值 |
| --- | --- |
| 输入 M1B run | `6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed`（manifest `179b82efca85fc6132bfa806d45b081729b0d5627878b0d517e978070fe99eb1`，contract `trajectory-compiler-m1ab-v4`） |
| lineage contract | `lineage-compiler-m1c-v2` |
| Git commit / tree | `943ba922270a368440ad5a81028da4beda58508b` / `aa083fbe9d46fba3b786e2d4d375967034b78576`，`dirty=false` |
| lineage run ID | `9ff708d98773a175f9552b09ea1171f16a6ba3bbe4a4ffbcc4fdec2612acb17b` |
| lineage `artifact_manifest` SHA-256 | `83934c6c4f9366dd7046e136161f78367a082f5748eb404532fa4816cef9eaeb` |

### 0′.3 双全量运行门禁

| 验收项 | 第一次 | 第二次 |
| --- | ---: | ---: |
| 建图回执时长 | 112.479697 秒 | 114.908946 秒 |
| 峰值 RSS | 244,387,840 bytes | 244,424,704 bytes |
| 512 MiB 停止线 | 通过 | 通过 |
| 独立双参 validator | `ok=true`，5 files | `ok=true`，5 files |

两次 run ID 相同；排除 `run_receipt.json` 后递归 diff 退出码 0。执行环境：本地盘干净克隆，provenance 自然 `verified`。

### 0′.4 计数与 `978c0347…` 产物对照

| 计数 | 观测 | 规格 §4.4 基线 |
| --- | ---: | ---: |
| capture / `RequestNode` / 候选组 | 1,683 / 6,301 / 956 | 同 |
| `SHARED_SOURCE_REQUEST` 边 | 1,671 | 1,671 |
| `EXPLICIT_REQUEST_SUCCESSOR` 边 | 4,994 | 4,994 |
| `COMPLETE_DUPLICATE_CAPTURE` 边 | 0 | 0 |

与绑定 v3 的 run `978c0347…` 对照：7 个文件**字节全部不同、字节长度全部相同**。逐字段归因——`request_node_id`、`edge_id` 及由此决定的行序随 `m1b_run_id` 改变；`lineage_manifest.json` / `lineage_report.json` / `artifact_manifest.json` 只变 `lineage_run_id`、`m1b_run_id`、`m1b_artifact_manifest_sha256` 三个绑定字段。剥离 ID 后做集合级比较：6,301 个 `RequestNode` 按 `source_request_id` 对齐后 `boundary_occurrences` 全部相同；4,994 条后继边以 `(parent, child)` 的 `source_request_id`、relation、evidence 表达后集合相同；1,671 条 capture 边以端点 capture ID、relation、evidence 表达后集合相同。即业务内容零变化，字节差异全部来自规格规定的 run 绑定。

### 0′.5 证据位置

```text
artifacts/r01/lineage/9ff708d98773a175f9552b09ea1171f16a6ba3bbe4a4ffbcc4fdec2612acb17b/
artifacts/r01/acceptance_m1c/9ff708d98773a175f9552b09ea1171f16a6ba3bbe4a4ffbcc4fdec2612acb17b/
```

`determinism.json` 额外记录相对 `978c0347…` 的重绑定归因与集合级相等结论。`978c0347…` 及其证据目录保留、只读。

---

## 0. v2 验收（2026-09-03，绑定 M1B v3 run，已被 §0′ 取代）

### 0.1 变更与动机

v1 验收后对冻结产物做独立复算（[`m1c-known-items.md`](m1c-known-items.md) K1）：同 `raw_request_hash` 的 19 组 capture 中 18 组请求输入内容不同、19 组 `tool_catalog_id` 不同，该字段相等不蕴含同一请求。提交 `fcff8cf88e7edcd645484318fd8bd12de50b7af7` 删除 `IDENTICAL_RAW_REQUEST_HASH` 关系及门④格式契约，契约升为 `lineage-compiler-m1c-v2`，规格升为 v0.4；剩余 3 类关系全部只依赖 M1B 已发布的可重算可见事实。净删除 245 行。

### 0.2 运行身份

| 项目 | 验收值 |
| --- | --- |
| 输入 M1B run | `519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d`（manifest `9dcdb083…`，与 v1 相同） |
| lineage contract | `lineage-compiler-m1c-v2` |
| Git commit / tree | `fcff8cf88e7edcd645484318fd8bd12de50b7af7` / `482b2a72c4b3b17bdb15c143f06bb5c1b48bb416`，`dirty=false` |
| lineage run ID | `978c0347b4198223cada09ccfd7bf55cf50a42a0ee7a049d55feedf793c7e12e` |
| lineage `artifact_manifest` SHA-256 | `837c9da7f89b16658f4eacca7ea04a0dd851c42abd9f93f2e094bddcfd9450f9` |

### 0.3 双全量运行门禁

| 验收项 | 第一次 | 第二次 |
| --- | ---: | ---: |
| 建图回执时长 | 116.317097 秒 | 118.341063 秒 |
| 峰值 RSS | 244,486,144 bytes | 243,265,536 bytes |
| 512 MiB 停止线 | 通过 | 通过 |
| 独立 validator | `ok=true`，5 files | `ok=true`，5 files |

两次 run ID 相同；排除 `run_receipt.json` 后递归 diff 退出码 0。

### 0.4 计数与 v1 产物对照

| 计数 | v2 观测 | 规格 §4.4 基线 |
| --- | ---: | ---: |
| capture / `RequestNode` / 候选组 | 1,683 / 6,301 / 956 | 同 |
| `SHARED_SOURCE_REQUEST` 边 | 1,671 | 1,671 |
| `EXPLICIT_REQUEST_SUCCESSOR` 边 | 4,994 | 4,994 |
| `COMPLETE_DUPLICATE_CAPTURE` 边 | 0 | 0 |

v2 产物与 v1 run `19d0325c…` 逐记录对照：`request_nodes.jsonl` 与 `request_successor_edges.jsonl` **逐字节相同**；`capture_relation_edges.jsonl` 恰好少 41 条 `IDENTICAL_RAW_REQUEST_HASH` 边，其余 1,671 条逐条相同，无新增。即 v2 只做了删除，未触碰任何保留关系。

### 0.5 证据位置

```text
artifacts/r01/lineage/978c0347b4198223cada09ccfd7bf55cf50a42a0ee7a049d55feedf793c7e12e/
artifacts/r01/acceptance_m1c/978c0347b4198223cada09ccfd7bf55cf50a42a0ee7a049d55feedf793c7e12e/
```

`determinism.json` 额外记录 v1→v2 的边集差异。v1 run 与证据目录保留，不再作为当前结论。

---

以下 §1–§8 为 v1（`lineage-compiler-m1c-v1`，2026-09-02）的原始验收记录，保留运行事实；其"正式通过"结论已由 v2 取代。

## 1. 结论（v1）

代码提交 `e50e999cf96c14d57d266f2f8e707f5bfc856832`（树 `9c17188d2e77054a20d024a8b9571abdf3a26cfa`，`dirty=false`）在冻结 M1B run `519a86d3…e06d1d` 之上完成两次独立全量建图。两份 lineage run 的内容寻址 run ID 与 `artifact_manifest.json` 摘要一致，排除非确定性的 `run_receipt.json` 后递归 diff 无差异；两份 run 均通过独立双参 validator；九项聚合计数与 [`m1c-processing-spec.md`](m1c-processing-spec.md) §4.4 的实测基线逐项吻合；门②、请求森林拓扑另经独立脚本复算确认。

因此 `lineage-compiler-m1c-v1` 可以认定为 M1C 正式通过。该结论只覆盖在已发布 M1B run 之上派生 Grade-A 关系边，不包含 Grade-B `NORMALIZED_VISIBLE_PREFIX_OF`（规格 §1 明确缓做）、QueryTurn、TaskEpisode 或任何语义关系。

## 2. 输入、版本与运行身份

| 项目 | 验收值 |
| --- | --- |
| 输入 | 已发布 M1B run `519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d` |
| 输入 `artifact_manifest` SHA-256 | `9dcdb083bd202157ab8c9e90b8c660a0e18130c62d65cafe743a7c27a5549876` |
| 输入语义契约 | `traceforge.restored-long-capture.v1` |
| lineage contract | `lineage-compiler-m1c-v1` |
| TraceForge | `0.3.0` |
| Git commit | `e50e999cf96c14d57d266f2f8e707f5bfc856832` |
| Git tree | `9c17188d2e77054a20d024a8b9571abdf3a26cfa` |
| 内容寻址 lineage run ID | `19d0325c32a470f1d3f08433be83d176ed16e591343b391eb88b98de663aa359` |
| lineage `artifact_manifest` SHA-256 | `9423ee2416f9ee21d13d02e62a8b64b97fa6a1ab26cc4b77b79ec927e4fd6f99` |

M1C 不读取原始 R01 JSONL；本次验收机器上的 R01 副本不完整这一事实不影响 M1C 验收（见 §7）。

两次回执均记录 `git_provenance.available=true`、`dirty=false`、`git_provenance_verified_at_completion=true`，并绑定同一 commit、tree、run ID 和 manifest 摘要。

## 3. 双全量运行门禁

| 验收项 | 第一次 | 第二次 |
| --- | ---: | ---: |
| 建图回执时长 | 114.524946 秒 | 111.146084 秒 |
| 峰值 RSS | 243,896,320 bytes | 244,428,800 bytes |
| 512 MiB 停止线 | 通过 | 通过 |
| 独立 validator | `ok=true`，退出码 0 | `ok=true`，退出码 0 |
| validator 文件数 | 5 | 5 |

两次 run ID 完全相同；排除 `run_receipt.json` 后递归 diff 退出码 0 且无输出。

补充：在同一提交的脏工作区（AFS 共享盘、含另一会话的未跟踪 M1D 文件）上另做两次建图，业务产物与正式 run 逐字节一致，说明确定性不依赖工作区状态；这两次因 §7 所述的 Git 超时问题被 validator 以 `RUN_RECEIPT_GIT_PROVENANCE_UNVERIFIED` 拒绝，符合"正式 run 必须 Git 来源可验证"的 fail-closed 设计，不作为正式证据。

## 4. 与规格基线的逐项对照

| 计数 | validator 观测 | 规格 §4.4 基线 |
| --- | ---: | ---: |
| capture 节点 | 1,683 | 1,683 |
| `RequestNode` | 6,301 | 6,301（distinct `source_request_id`） |
| 候选组 | 956 | 956 |
| `SHARED_SOURCE_REQUEST` 边 | 1,671 | 1,671 |
| `EXPLICIT_REQUEST_SUCCESSOR` 边 | 4,994 | 4,994 |
| `IDENTICAL_RAW_REQUEST_HASH` 边 | 41 | 19 组 / 47 capture（组大小 5,4,3×4,2×13 → 41 边） |
| `COMPLETE_DUPLICATE_CAPTURE` 边 | 0 | 0 |
| `raw_request_hash` QUALIFIED / UNKNOWN | 1,675 / 8 | 1,675 / 8 |

独立脚本复算（不调用 M1C 代码，只读已发布 `private/*.jsonl` 与 M1B `captures.jsonl`）：

- 门②：IDENTICAL 19 个连通分量中 **5 个跨候选组**，与规格一致；SHARED 边 0 例跨候选组；
- 请求森林：入度 ≤ 1、最大出度 13、无环，与规格 §4.2 一致。

## 5. 四门与测试门禁

四条硬门在代码与 validator 中的兑现方式见规格 §3、§8、§11.1。本次提交前门禁：lineage 相关 77 项测试通过（含跨候选组 IDENTICAL、门③分区负例、node/successor bijection、关系越级、`target_hash` 混入 evidence、报告闭合、M1B 身份绑定、reader 类型守卫），Ruff lint 与 format 通过。

## 6. 轻量验收证据

正式 run 保存在本地：

```text
artifacts/r01/lineage/19d0325c32a470f1d3f08433be83d176ed16e591343b391eb88b98de663aa359/
```

必要证据保留在 Git 忽略目录：

```text
artifacts/r01/acceptance_m1c/19d0325c32a470f1d3f08433be83d176ed16e591343b391eb88b98de663aa359/
├── run_a_receipt.json / run_b_receipt.json
├── run_a_artifact_manifest.json / run_b_artifact_manifest.json
├── build_a.json / build_b.json                 # 退出码、墙钟、峰值 RSS
├── validation_a.json / validation_b.json
├── determinism.json
├── measurements.json
└── dirty_tree_build_a.json / dirty_tree_build_b.json / dirty_tree_validation_a.json
```

真实 run 和验收证据均不进入 Git。

## 7. 环境已知项（非代码缺陷，登记）

`trajectory/provenance.py` 对每条 git 命令设 5 秒超时。本次验收所在的 AFS 共享盘上 `git status --porcelain` 实测 7.7–7.8 秒，导致直接在该工作区运行时回执记录 `available=false`，validator 按设计拒绝。正式 run 因此在本地盘的干净克隆（同一提交 `e50e999`）上执行。该超时属于冻结 M1B 代码，本次不修改；若未来在慢盘上需要就地做正式 run，应走单独 reopen 评估放宽超时或改用 `--untracked-files=no`，并同步重新验收。

另登记：验收机器上 `return_data/four_batch/by-rubric/R01.jsonl` 为 119,013,376 字节 / 368 行的不完整副本，与冻结基线（560,481,884 字节 / 1,683 行）不符。M1C 不消费该文件，故不影响本报告；但在该机器上无法重跑 M1B 全量编译，进入需要原始 JSONL 的阶段前必须先取回完整文件并核对 SHA-256。

## 8. 停止线

本次正式结论停止在 M1C。M1D 的规格 [`m1d-processing-spec.md`](m1d-processing-spec.md) 处于评审稿状态，其实现与验收另行报告。进入 M2 前仍须先冻结最小、带来源的 `SourceAnnotationProjection` 或只读 `SourceResolver`（[`r01-processing-spec.md`](r01-processing-spec.md) §8.3）。
