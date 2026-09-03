# R01 `UserTextProjection` 全量验收报告

日期：2026-09-03。本报告记录 M2 前置 `UserTextProjection`（契约 `user-text-projection-v1`，提交 `1de39ae`，规格 [`m2-source-projection-spec.md`](m2-source-projection-spec.md) v0.3）在已正式验收的 M1B v4 run `6be45e01…` 之上、绑定已正式验收的 M1D v1 run `84d826b3…` 的两次独立全量构建、一次未绑定构建与独立 validator 结果，并记录同批提交的 validator 公共原语重构（`f70710f`）对 M1B/M1C/M1D 的重新验收。开发纪律只引用 [`../AGENTS.md`](../AGENTS.md)；验收流程与 M1D 同构（[`r01-m1d-validation.md`](r01-m1d-validation.md)）。

## 1. 结论

代码提交 `1de39aeda894b55cc84d1fb8f8923e2b4c57380f`（树 `8fbd9729996bd1b2621ed27092db6cf3f1ef274a`，`dirty=false`）在本地盘干净克隆上，对已发布 M1B v4 run `6be45e01…` + M1D v1 run `84d826b3…` 完成两次独立全量构建，均复现内容寻址 run ID `47cfac20…`；两次 `artifact_manifest.json` 摘要一致，排除非确定性的 `run_receipt.json` 后递归 diff 无差异；两次 run 均通过独立三参 validator（`ok=true`，3 files）。未绑定 M1D 的第三次构建得到另一 run ID `9dc26f2f…`，其注解表除 `user_block_id` 全空外与绑定 run 逐字节相同、报告计数完全相同（绑定只影响回指与身份，不影响任何分类）。全部 23 项报告计数与 12 个白名单标签计数与规格 §6 验收向量**逐项相等**；1,182 个 `OBSERVED` USER 事件全部回指到 UserBlock；跨模块不变量 330 ≤ 342 成立。

因此 `user-text-projection-v1` 认定为正式通过。结论只覆盖：对已发布 M1B run 的**每条 USER 事件**做纯结构注解（可定位性、内容形态、开头标签白名单类别、可选 UserBlock 回指）与诚实分母报告。不包含任何语义判断（"像不像真实 query"）、`PLAIN_USER_TEXT` 的细分、前缀内部结构推断、任务意图或 `TaskEpisode`（规格 §1/§7，留 M2）。

## 2. 输入、版本与运行身份

| 项目 | 验收值 |
| --- | --- |
| 输入 M1B run | `6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed`（[`r01-m1b-v4-validation.md`](r01-m1b-v4-validation.md)），manifest SHA-256 `179b82efca85fc6132bfa806d45b081729b0d5627878b0d517e978070fe99eb1` |
| 可选输入 M1D run | `84d826b3d7abddffb5f8fde28d7e5592590720bd6909c24a8b17b7f0c70b66ea`（[`r01-m1d-validation.md`](r01-m1d-validation.md)），manifest SHA-256 `56a3befe44bec087a52aaa680f592eb71e196c7d7bd946e47c172540cb190bfe` |
| 输入语义契约 / compiler contract | `traceforge.restored-long-capture.v1` / `trajectory-compiler-m1ab-v4` |
| projection contract | `user-text-projection-v1` |
| TraceForge / Python | `0.3.0` / `3.12.13` |
| Git commit | `1de39aeda894b55cc84d1fb8f8923e2b4c57380f` |
| Git tree | `8fbd9729996bd1b2621ed27092db6cf3f1ef274a` |
| 内容寻址 run ID（绑定 M1D） | `47cfac20b89ed066f388da6679cb53f745e6a0392cd5f6e459e869c7076f689c` |
| 其 `artifact_manifest` SHA-256 | `b45193276fe0a83d45bf5fa8bd4bc8e6edc303aad493ec3c32fc33894a01ceab` |
| 内容寻址 run ID（未绑定 M1D） | `9dc26f2fc2ae2ce50e002834597142b4accf5c0f7e410ff798c691b4fa05c627` |
| 其 `artifact_manifest` SHA-256 | `b7393fb5bc4ebd397d14365e12ea4189a20ce0ee41d80733105e7474a0754da3` |

投影不读取原始 R01 JSONL，只读消费已发布 M1B run（reader 先跑 M1B 权威 `validate_compiled_run`）与 M1D run（先跑 M1D 权威 `validate_query_turn_run`，后者内部再次核验同一 M1B run 并断言二者绑定一致）。run ID = `stable_id(contract, m1b_run_id, m1b_manifest_sha256, m1d_run_id | null, m1d_manifest_sha256 | null)`，与路径、机器、provenance 无关。三次回执均记录 `git_provenance.available=true`、`dirty=false`、`git_provenance_verified_at_completion=true`，并绑定同一 commit 与 tree。

## 3. 全量运行门禁

| 验收项 | 绑定 A | 绑定 B | 未绑定 C |
| --- | ---: | ---: | ---: |
| 构建回执时长 | 397.426230 秒 | 398.604551 秒 | 193.778914 秒 |
| 墙钟（含解释器启动） | 411.135412 秒 | 410.901038 秒 | 205.829059 秒 |
| 峰值 RSS | 257,028,096 bytes（245.1 MiB） | 256,225,280 bytes（244.4 MiB） | 245,571,584 bytes（234.2 MiB） |
| 512 MiB 停止线 | 通过 | 通过 | 通过 |
| 独立 validator | `ok=true`，退出码 0，3 files，410.7 秒 | `ok=true`，退出码 0，3 files，409.6 秒 | `ok=true`，退出码 0，3 files，208.5 秒 |

A 与 B run ID 完全相同；排除 `run_receipt.json` 后递归 diff 退出码 0 且无输出。产物 6.0 MB：`private/user_text_annotations.jsonl` 14,407 行 / 6,198,825 字节，`projection_manifest.json` 618 字节，`reports/projection_report.json` 1,236 字节。

绑定构建与未绑定构建对照：注解表剥离 `user_block_id` 字段后逐行相等；未绑定 run 的 `user_block_id` 全为 `null`；两份报告的 23 项计数完全相等。绑定路径耗时约为未绑定的两倍，因为 M1B run（706 MB）被权威 validator 重验两次（reader 直接一次、M1D validator 内部一次），属先校验后消费的必要成本（§9）。

## 4. 与规格 §6 验收向量的逐项对照

validator 由**已发布注解表**独立重算的 23 项计数（`observed_counts`）与规格 §6（§8 探针在同一 v4 run 上的参考计算）逐项相等：

| 计数 | validator 观测 | 规格 §6 |
| --- | ---: | ---: |
| `capture_count` | 1,683 | 1,683 |
| `user_event_count`（= 注解行数） | 14,407 | 14,407 |
| `content_form`：`TEXT_STRING` / `TEXT_WITH_DATA_URL_SEGMENTS` / `DATA_URL_SUMMARY` / `CONTENT_BLOCKS` | 14,405 / 2 / 0 / 0 | 14,405 / 2 / 0 / 0 |
| `OBSERVED`：`PLAIN_USER_TEXT` / `HARNESS_CONTEXT` / `HARNESS_CAPABILITY` / `CONTROL_SIGNAL` / `UNKNOWN_TAGGED` / `EMPTY_TEXT` / `NO_LEADING_TEXT` | 892 / 271 / 1 / 17 / 1 / 0 / 0（合计 1,182） | 同 |
| `PREFIX_UNLOCALIZED`：同序 | 10,802 / 2,230 / 57 / 79 / 53 / 4 / 0（合计 13,225） | 同 |
| `captures_with_plain_user_text` / `…_only_in_prefix` / `captures_with_observed_plain_user_text` | 1,680 / 1,350 / 330 | 1,680 / 1,350 / 330 |

由已发布注解表统计的白名单标签计数（`leading_tag` 只允许白名单字面量，故可在 Control 侧证据中列出）：`environment_context` 2,222；`in-app-browser-context` 203；`system-reminder` 66；`codex_internal_context` 4；`system-conventions` 2；`available-deferred-tools` 2；`local-command-caveat` 2；`recommended_plugins` 35；`codex_delegation` 23；`turn_aborted` 91；`subagent_notification` 3；`user_interjection` 2——与规格 §6 逐项相等，合计 2,655 = 全体 `HARNESS_CONTEXT` 2,501 + `HARNESS_CAPABILITY` 58 + `CONTROL_SIGNAL` 96。54 条 `UNKNOWN_TAGGED` 行 `leading_tag` 全为 `null`（内容安全门③）。

M1D 绑定：1,182 个 `OBSERVED` 事件 `user_block_id` 全部非空且与 M1D `user_blocks.jsonl` 的 `event_ids` 索引逐条一致；13,225 个 `PREFIX_UNLOCALIZED` 事件 `user_block_id` 全空；M1D 有 UserBlock 的 capture 去重数 342，`captures_with_observed_plain_user_text` 330 ≤ 342（跨模块不变量，规格 §2.4 门④）。

对 M2 的直接含义（规格 §0/§2.5，此处只重述事实）：14,407 个 USER 事件中只有 11,694 条是去空白后不以白名单标签起头的普通文本，其中 10,802 条落在不可定位前缀；1,683 个 capture 中 1,350 个的普通用户文本**只**在前缀、仅 330 个在观测窗口内有普通用户文本。M2 若只用可定位证据，`ObservedTaskDistribution` 的分母就是 330；使用前缀证据必须携带 `intent_locality=PREFIX_ONLY` 单列。

## 5. 验收方法与 validator 信任边界

validator 取三参 `<projection_run> <m1b_run> [--m1d-run <m1d_run>]`，分两层（规格 §3）：

- **篡改检测**：从 M1B（与 M1D）已发布字段重建输入视图（reader 内部先跑各自权威 validator），调用与 pipeline 同一个纯 fold `build_user_text_projection_graph` 重建期望注解，与 `private/user_text_annotations.jsonl` 双向集合相等（canonical 字节比对）。
- **正交不变量（不经 fold、不经 `classify_leading_text`）**：M1B USER 事件 ⇄ 注解一一覆盖；`capture_occurrence_id`/`locality`/`request_boundary_id`/`content_form`/`utf8_byte_length` 逐条等于 M1B 已发布事实；`leading_tag ≠ null ⇔ 带标签类别` 且取值恒在该类别白名单内；`NO_LEADING_TEXT ⇔ 无开头文本`、`EMPTY_TEXT ⇔ 开头窗口为空`；M1D 绑定时观测事件回指与 UserBlock 索引一致且 UserBlock 与注解同属一个 capture、前缀事件恒不回指，未绑定时全表为空；23 项报告计数由已发布表重算；`captures_with_observed_plain_user_text ≤ |{UserBlock.capture_occurrence_id}|`。

反向核对（均按设计 fail-closed，退出码 1）：

| 用例 | 结果 |
| --- | --- |
| 绑定 run 不提供 M1D oracle | `M1D_RUN_REQUIRED` |
| 未绑定 run 却提供 M1D oracle | `M1D_BINDING_MISMATCH` |
| 以 M1B **v3** 冻结 run `519a86d3…` 作 oracle | `M1B_INPUT_INVALID` + `M1D_INPUT_INVALID`（M1D validator 同样拒绝与 v3 run 的绑定） |

validator 不会在错误上游、缺失 oracle 或多余 oracle 上冒充通过。

## 6. 同批 validator 公共原语重构的重新验收

提交 `f70710f` 把 M1B/M1C/M1D 三个 validator 各自复制的物理层原语（manifest 自洽、逐文件摘要/字节数/行数、目录清单、回执与 git provenance、控制文件路径扫描、canonical JSONL 读入、双向 bijection）抽到 `trajectory/run_validation.py`，并把三处重复的 `artifact_entry_dicts` 收拢到 `trajectory/artifacts.py`。它触碰了 `src/traceforge/trajectory/`、`lineage/`、`query_turns/`，因此在同一干净克隆（`1de39ae`）上补做：

| 项目 | 结果 |
| --- | ---: |
| 重构后 M1B validator 重验 `6be45e01…` | `ok=true`，10 files，1,683 lines，175,858 events（140.8 秒） |
| 重构后 M1C validator 重验 `9ff708d9…`（oracle `6be45e01…`） | `ok=true`，5 files，SHARED 1,671 / SUCCESSOR 4,994 / DUPLICATE 0 / request 6,301 / group 956（211.8 秒） |
| 重构后 M1D validator 重验 `84d826b3…`（oracle `6be45e01…`） | `ok=true`，8 files，13 项计数与 [`r01-m1d-validation.md`](r01-m1d-validation.md) §4 一致（216.8 秒） |
| 重构后重建 M1D | 复现 run ID `84d826b3…`；排除 `run_receipt.json` 后与冻结 run 递归 diff 无差异（210.0 秒） |
| 重构后重建 M1C | 复现 run ID `9ff708d9…`；排除 `run_receipt.json` 后与冻结 run 递归 diff 无差异（210.6 秒） |

三条已验收 run 的正式结论不受重构影响；重构对 M1C/M1D 产物字节中性。

## 7. 测试与静态门禁

提交前门禁：`test_source_projection_*` 77 项 + CLI 2 项通过——契约层（开标签文法向量：bare/属性/自闭合三形态、大小写敏感、`</x>`/`<3`/`<<`/`< x`/`<-x>`/裸 `<`、前置 BOM 与零宽字符、名后无终结符、256 码点窗口边界与跨边界命中、分类只读开头；白名单 A7/B2/C3 冻结成员与两两不交；四种 `content_form` 与分段首段为文本/Data URL/全空白；报告键闭合 5+4+2×7）；builder（排序确定性与输入顺序无关、诚实 capture 分母、M1D 绑定回指与前缀恒空、M1D 绑定但事件无归属整批失败、scope/boundary 契约违约 fail-closed、三个分母语义）；pipeline（13 条 USER 事件逐条期望形状、来源事实透传、手算 23 项报告向量、双构建逐字节一致与内容寻址、回执无路径、产物无正文/无 Data URL/无未知标签名、M1D 回指与 run ID 分叉、绑定别的 M1B 的 M1D 拒绝、篡改上游/目录缺失/覆盖拒绝）；validator（合法绑定/未绑定 run 零误报、报告由表重算、oracle 缺失/多余/错误、物理层摘要与清单、删行/幻影/改类别并同步改报告重签、置空 bijection 后不变量层对标签泄漏/跨类标签/类别与结构不相容/来源事实漂移/回指漂移/未绑定回指/报告膨胀 fail-closed、M1D 丢块的跨模块不变量、manifest 绑定漂移）；全量 352 项通过；Ruff lint 与 format 通过；`uv lock --check --offline` 通过。

## 8. 轻量验收证据

正式 run 保存在本地（Git 忽略）：

```text
artifacts/r01/source_projection/47cfac20b89ed066f388da6679cb53f745e6a0392cd5f6e459e869c7076f689c/   # 绑定 M1D
artifacts/r01/source_projection/9dc26f2fc2ae2ce50e002834597142b4accf5c0f7e410ff798c691b4fa05c627/   # 未绑定
```

必要证据保留在 Git 忽略目录：

```text
artifacts/r01/acceptance_utp/47cfac20b89ed066f388da6679cb53f745e6a0392cd5f6e459e869c7076f689c/
├── run_{a,b,c_unbound}_receipt.json / run_{a,b,c_unbound}_artifact_manifest.json
├── build_{a,b,c_unbound}.json                  # 退出码、墙钟、峰值 RSS
├── validation_{a,b,c_unbound}.json             # ok、3 files、23 项计数
├── negative_oracle_validations.json            # 三个反向 oracle 用例
├── determinism.json                            # 双跑 diff、绑定/未绑定对照
├── vector_check.json                           # 与规格 §6 的逐项对照（含标签计数、回指、342/330）
├── revalidation_after_refactor.json            # 重构后三 validator 重验冻结 run
├── rebuild_after_refactor.json                 # 重构后重建 M1C/M1D 的 run ID 与 diff
└── measurements.json
```

真实 run 和验收证据均不进入 Git。

## 9. 环境已知项（非代码缺陷，登记）

与 [`r01-m1d-validation.md`](r01-m1d-validation.md) §9 相同：AFS 共享盘上 `git status` 超过 `provenance.py` 的 5 秒超时，就地 run 会被 validator 拒绝；正式 run 在本地盘干净克隆 `/tmp/tf-utp-1de39ae`（`git status` 0.2 秒）上执行，代码从克隆 `src/` 导入（回执与导入路径均已核对），M1B/M1D 输入 run 只读自 AFS `artifacts/`。

绑定 M1D 的构建与校验各约 6.8 分钟、未绑定约 3.4 分钟；时间几乎全部花在 M1B 权威 validator 对 706 MB 上游 run 的重哈希与重算上，绑定路径因 M1D validator 内部再验一次 M1B 而翻倍。这是"先校验后消费"在每个模块边界独立成立的代价；若日后 M2 需要在同一进程内消费 M1B/M1C/M1D/投影四条 run，可考虑一次校验、多处复用的显式受信句柄，但那是新的信任边界设计，须单独起规格，不在本次范围内。

## 10. 停止线

本次正式结论停止在 M2 前置 `UserTextProjection`。R01 上现有四条 run 以内容寻址身份链式绑定：`47cfac20…` ← (`6be45e01…`, `84d826b3…`)，`84d826b3…` ← `6be45e01…` → `9ff708d9…`。规格 §7 D10 的白名单扩展候选 `skill`（41 次，全 bare）保持 `UNKNOWN_TAGGED`，扩展只能经修订规格 + §8 离线探针；`SourceAnnotationProjection` 按 D3 缓做，在 M2 首次消费 `domain_meta` 前必做。进入 M2 本体前须先起草并评审 M2 规格（`TaskEpisode` / `ObservedTaskDistribution` / `EnvironmentExposureProfile`，[`overall-plan.md`](overall-plan.md) §4.6/§5/§6），其 LLM 输入单元与消费约定以本投影规格 §2.5 为前置约束。
