# R01 M1 全量验收报告

日期：2026-08-31

范围：M1A Source Adapter、M1B Structural Compiler

## 1. 结论

冻结的 R01 回流数据已完成两次独立全量编译。第一次产物通过通用独立 validator；两次确定性业务产物逐字节一致。1,683 条物理记录全部形成明确终态，未出现 `PARTIAL` 或 `QUARANTINED`。

本报告只记录外部验收事实。下列 R01 路径、摘要、条数和统计值均未写入 compiler、validator 或测试分支。

## 2. 输入与运行身份

| 项目 | 验收值 |
| --- | --- |
| 物理格式 | `jsonl` |
| 语义契约 | `traceforge.restored-long-capture.v1` |
| dataset ID | `r01-four-batch-202607-v1` |
| SHA-256 | `3832d8aa4ecd577636ce67b56d8798d9fb6311662a7bbce65814e5e8d260d4d8` |
| 字节数 | 560,481,884 |
| 物理行数 | 1,683 |
| 内容寻址 run ID | `024eb6b9909b1dc9435150991c7616b037a850d217b0a50d89f68fdbcbf1b678` |
| Python | 3.12.13 |

`source_schema` 同时进入 source manifest、artifact manifest 和 run ID。相同来源字节若按不同语义契约解释，不会得到同一个运行身份。

## 3. 全量结构统计

| 结构事实 | 数量 |
| --- | ---: |
| NormalizedCapture | 1,683 |
| RequestBoundary | 9,561 |
| 唯一 source request | 6,301 |
| EventOccurrence | 175,858 |
| System event | 6,220 |
| User event | 14,407 |
| Assistant message event | 48,667 |
| Tool call event | 56,424 |
| Tool result event | 50,140 |
| ActionBatch | 42,318 |
| ToolPairingRecord | 56,424 |
| 严格一对一 pairing | 49,800 |
| 未观测 result 的 call ID | 6,474 |
| 含未观测 result 的 capture | 1,248 |
| 重复 result group | 150 |
| 额外 result occurrence | 190 |
| Tool definition | 18,566 |
| Compaction capture | 119 |
| Input-truncated capture | 35 |
| Terminal text outcome | 472 |
| Terminal tool-call pending | 1,211 |
| `COMPLETE` | 1,683 |
| `PARTIAL` | 0 |
| `QUARANTINED` | 0 |

所有数值均与 [`r01-processing-spec.md`](r01-processing-spec.md) 中冻结的外部 oracle 一致。

## 4. 独立验证结果

独立 validator 不调用 compiler 重建期望输出，也不读取 R01 expectation。它从已发布目录重新校验：

- 十个确定性 artifact 的 canonical 编码、摘要、字节数和记录数；
- source、artifact manifest、逐行来源账本及 run ID 的闭合关系；
- capture、boundary、event、ActionBatch、pairing 和 catalog 的稳定 ID 与外键；
- event 顺序、来源 pointer、可见 payload 长度和摘要；
- reasoning 只保留摘要、Data URL 不以原始内容落盘；
- 公共 attrition report 的字段白名单和重算计数；
- `run_receipt.json` 对 artifact manifest 的反向绑定。

真实全量结果：

```json
{"checked_file_count":10,"event_occurrence_count":175858,"ok":true,"physical_line_count":1683}
```

## 5. 确定性、资源与隐私

| 验收项 | 结果 |
| --- | --- |
| 第一次编译业务耗时 | 28.31 秒 |
| 第一次编译峰值 RSS | 127,451,136 bytes，约 121.5 MiB |
| validator 峰值 RSS | 203,898,880 bytes，约 194.5 MiB |
| 512 MiB 内存停止线 | 通过 |
| 第二次独立编译 | 通过 |
| 排除 `run_receipt.json` 后目录递归 diff | 无差异 |
| 原始 Base64 Data URL header 扫描 | 无命中 |
| reasoning 原文形态扫描与结构校验 | 无命中 |

`run_receipt.json` 只包含运行时间、路径、Python 和版本等非确定性元数据，因此不属于确定性业务文件。

## 6. 代码质量门

- 单元与对抗性回归测试：56 项通过；
- Ruff lint：通过；
- Ruff format check：通过；
- Git whitespace/diff check：通过；
- 核心代码、CLI、validator 与测试中的 R01 路径、摘要、统计、模型名和工具名扫描：无命中。

## 7. 当前停止线

M1A、M1B 已达到实现和全量验收条件。本阶段没有实现跨 capture 的 M1C lineage graph、M1D QueryTurn，也没有提前进入任务/环境重建、可解性认证、难度或 Harbor 执行层。
