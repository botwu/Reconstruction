# R01 M1 v2 全量验收报告

日期：2026-08-31

范围：M1A Source Adapter、M1B Structural Compiler

状态：**结论已撤销，不得作为 M1A/M1B 完成证据。**

> 后续审计确认通用 typed payload 自洽性、公共报告 dataset slug 边界和异常一对一 pairing 仍可被重签绕过。现存 run 的正常路径事实继续有效，但必须修复并重新完成双运行留证后才能发布新的正式结论。

## 1. 结论

提交 `9e0c6a5c821a1d648931cab54430f6dc92c3cf48` 已闭合本轮审计要求的语义验收、递归隐私、极端输入、Data URL、数值表示和版本契约。冻结 R01 以 M1 v2 完成两次独立全量编译，两份已发布 run 均通过独立 validator，排除非确定性的 `run_receipt.json` 后所有文件逐字节一致。

本节是已经撤销的历史结论。现存 run 只证明该批正常输入下的来源账本与主要可见结构闭合，不能证明通用 M1A/M1B 契约已经通过验收。

## 2. 输入、版本与运行身份

| 项目 | 验收值 |
| --- | --- |
| 输入 | `return_data/four_batch/by-rubric/R01.jsonl` |
| 输入语义契约 | `traceforge.restored-long-capture.v1` |
| dataset ID | `r01-four-batch-202607-v1` |
| SHA-256 | `3832d8aa4ecd577636ce67b56d8798d9fb6311662a7bbce65814e5e8d260d4d8` |
| 字节数 | 560,481,884 |
| 物理行数 | 1,683 |
| compiler contract | `trajectory-compiler-m1ab-v2` |
| TraceForge | `0.2.0` |
| Git commit | `9e0c6a5c821a1d648931cab54430f6dc92c3cf48` |
| Git tree | `330b5249a10e40f1920cce6a444d5afee5afae03` |
| 内容寻址 run ID | `5525b6a49dd9b4111bcd0e1732bc1b6aa909b55e669c1c13e3c28a1b2c8daac6` |

两次回执均记录 `dirty=false`、`git_provenance_verified_at_completion=true`。输入契约继续为 v1；发生字段或语义变化的输出契约为 v2，稳定 ID 身份公式未变化并继续使用原 namespace。

## 3. 审计阻断闭合

| 原阻断 | v2 闭合方式 |
| --- | --- |
| 重签语义损坏可假绿 | validator 从事件和边本体重算 boundary、ActionBatch、pairing、quality、reason code 与 report |
| 嵌套 `reasoning_content` 泄漏 | 任意层级递归摘要，保留 marker 防碰撞，并从可见指纹递归排除 |
| 深层 JSON/派生结构裸抛 | 两级固定深度门控；逐行 `QUARANTINED`，后续行继续处理 |
| Data URL 规则不一致 | compiler/validator 共用结构扫描器，覆盖大小写、空白、换行、百分号转义和 RFC 2231 参数 |
| 浮点折损与整数环境依赖 | 拒绝不能忠实往返的浮点数，冻结 640 位整数上限 |
| ActionBatch 过度推断 | 无明确并行证据时固定为 `UNKNOWN` |
| duplicate pairing 伪精确匹配 | 重复组保留完整 occurrence 集合，两个 `matched_*` 必须为 `null` |
| event payload 被下游重复解释 | 五类 event 使用唯一 typed payload reader |
| source-attested 事实被过度声称 | validator 明确区分可独立重算事实与只能核对自洽性的来源证明 |

重签后的 boundary ownership、pairing matched/status、quality 枚举与聚合报告破坏均有回归测试。深嵌套、隐私 envelope 碰撞、Data URL 变体、数值折损及 validator 深层 catalog 也均有 fail-closed 回归测试。

## 4. 全量执行门禁

| 验收项 | 结果 |
| --- | --- |
| 第一次编译回执时长 | 42.891591 秒 |
| 第二次编译回执时长 | 43.391533 秒 |
| 第一次编译峰值 RSS | 100,941,824 bytes，约 96.3 MiB |
| 512 MiB 停止线 | 通过 |
| 两次内容寻址 run ID | 完全相同 |
| 排除 `run_receipt.json` 的递归 diff | 无差异 |
| 第一次独立 validator | `ok=true`，退出码 0 |
| 第二次独立 validator | `ok=true`，退出码 0 |
| validator 文件数 | 10 |
| validator 来源行数 | 1,683 |
| validator 事件数 | 175,858 |
| pytest | 123 项通过 |
| Ruff lint / format | 通过 |
| `uv lock --check --offline` | 通过 |
| Git diff check | 通过 |

正式 run 保存在本地 `artifacts/r01/5525b6a49dd9b4111bcd0e1732bc1b6aa909b55e669c1c13e3c28a1b2c8daac6/`，真实产物不进入 Git。

## 5. R01 capture 结构与损耗画像

主要守恒关系在 v2 中继续闭合：事件类型与 scope 分别求和均为 175,858；56,424 个 call 与 50,140 个 result 的差额 6,284，等于 6,474 个未观测 result 减去 190 个额外 result occurrence。

| 结构观察 | 数量或比例 |
| --- | ---: |
| RequestBoundary occurrence | 9,561 |
| 唯一 source request | 6,301 |
| 重复出现的 request ID | 942 |
| 单个 request 最大 occurrence | 20 |
| `thread_id/account_id` 比较分区 | 956 |
| EventOccurrence | 175,858 |
| PRE_FIRST 事件占比 | 72.64% |
| 严格一对一 pairing | 49,800，88.26% |
| 含未观测 result 的 capture | 1,248，74.15% |
| terminal tool-call pending | 1,211，71.95% |
| 任意 schema conflict | 349，20.74% |
| 任意 inferred schema | 280，16.64% |
| compaction / truncation | 119 / 35，7.07% / 2.08% |
| input truncation unknown | 0 |

使用最保守的纯结构筛选：`TEXT_OUTCOME`、无 missing/duplicate result、schema `CONSISTENT`、无 compaction、明确未截断，只剩 353 个 capture，占 20.97%。这些 capture 仍然不是任务，只是进入 lineage、QueryTurn 和重建资格判断的候选。

`COMPLETE=1,683` 只表示可见结构被忠实编译，不表示用户任务完成。TaskEpisode 属于 M2，因此以上结果只能称为 **R01 capture 结构与损耗画像**，不能伪称为 episode distribution、生产任务分布或不可观测用户环境的无偏真值。

## 6. 停止线

本次工作到 M1A/M1B 为止。下一阶段若启动，应按顺序单独审核 M1C 跨 capture lineage 与 M1D 结构型 QueryTurn；进入 M2 前还必须冻结最小、带来源的 `SourceAnnotationProjection` 或只读 `SourceResolver`，不得让 M2 私下按绝对路径重新解析原始 JSONL。
