# R01 M1 v3 全量验收报告

日期：2026-09-01

范围：M1A Source Adapter、M1B Structural Compiler

状态：**正式通过。M1C 尚未启动。**

## 1. 结论

代码冻结提交 `c3c0a8fb6ed9927d5bebba55c616e1ef524c653b` 已闭合 typed payload 重签、公共报告 dataset slug 和异常一对一 pairing 三项 P1，并关闭审计期间发现的脱敏 Data URL 终态同步重签变体。冻结 R01 随后完成两次独立全量编译；两份 run 均通过独立 validator，内容寻址 run ID 与 manifest 摘要一致，排除非确定性的 `run_receipt.json` 后递归 diff 无差异。

因此，`trajectory-compiler-m1ab-v3` 可以认定为 M1A/M1B 正式通过。该结论只覆盖来源摄取和 capture 内结构编译，不包含跨 capture lineage、QueryTurn、TaskEpisode、任务或环境画像。

2026-09-01 另有一次会话现场 live 复核，把此前部分自报门（gate 4/6 及 gate 1/5/7）升级为现跑亲证，结果与本报告一致，详见 §8。对抗审计带来源接受但暂不修改的两项已知项（R1 隐私脱敏潜伏 fail-open、R4 截断轴先验渲染成观测）登记在 [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md)，均未使任一验收门变红。

## 2. 输入、版本与运行身份

| 项目 | 验收值 |
| --- | --- |
| 输入 | `return_data/four_batch/by-rubric/R01.jsonl` |
| 输入语义契约 | `traceforge.restored-long-capture.v1` |
| dataset ID | `r01-four-batch-202607-v1` |
| SHA-256 | `3832d8aa4ecd577636ce67b56d8798d9fb6311662a7bbce65814e5e8d260d4d8` |
| 字节数 | 560,481,884 |
| 物理行数 | 1,683 |
| compiler contract | `trajectory-compiler-m1ab-v3` |
| ToolPairing schema | `traceforge.tool-pairing.v3` |
| TraceForge | `0.3.0` |
| Git commit | `c3c0a8fb6ed9927d5bebba55c616e1ef524c653b` |
| Git tree | `5438c8d3e421007bcc400c30c47862945caaae55` |
| 内容寻址 run ID | `519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d` |
| artifact manifest SHA-256 | `9dcdb083bd202157ab8c9e90b8c660a0e18130c62d65cafe743a7c27a5549876` |

两次回执均记录 `dirty=false`、`git_provenance_verified_at_completion=true`，并绑定同一 commit、tree、run ID 和 manifest 摘要。

## 3. 三项 P1 闭合

| 阻断 | v3 闭合方式 | 对抗验收 |
| --- | --- | --- |
| typed payload 与 quality 可同步重签假绿 | 普通 TEXT 独立重算长度与摘要；Data URL envelope 必须为严格正长度，terminal 按 value 形态而非审计长度重算；arguments pointer 的 message/sub index 必须与 event 位置一致 | 普通文本、纯 Data URL、混合 Data URL 的完整 quality/report 同步重签，以及合法格式错误位置的 arguments pointer 重签均 fail-closed |
| 普通 URL 可经 dataset ID 泄入公共报告 | compiler 与 validator 共用 1–128 字符小写 ASCII slug 契约；拒绝 URL、路径、query、fragment、凭据和非法字符，错误不得回显输入 | producer 侧非法 ID 在发布前失败；三处一致伪造并重签后 validator 仍失败 |
| 异常一对一 pairing 生成伪精确边 | 只有数量 1:1、名称一致、result 不早于 call 且 arguments 有效时才能写 `matched_*`；任一异常均置空 | `NAME_MISMATCH`、`RESULT_BEFORE_CALL`、`INVALID_CALL_ARGUMENTS` 的正常生成与重签伪造边均 fail-closed |

当前 R01 的上述异常 pairing 数量为 0，因此旧 run 未被这些伪精确边实际污染；正式通过依据是通用契约及其对抗测试闭合，而不是该批数据恰好未触发异常。

## 4. 双全量运行门禁

| 验收项 | 第一次 | 第二次 |
| --- | ---: | ---: |
| 编译回执时长 | 42.648523 秒 | 42.540646 秒 |
| 峰值 RSS | 116,637,696 bytes | 114,098,176 bytes |
| 512 MiB 停止线 | 通过 | 通过 |
| 独立 validator | `ok=true`，退出码 0 | `ok=true`，退出码 0 |
| validator 文件数 | 10 | 10 |
| validator 来源行数 | 1,683 | 1,683 |
| validator 事件数 | 175,858 | 175,858 |

两次内容寻址 run ID 完全相同；两份 `artifact_manifest.json` 的 SHA-256 均为 `9dcdb083bd202157ab8c9e90b8c660a0e18130c62d65cafe743a7c27a5549876`。排除只含时间与运行环境差异的 `run_receipt.json` 后，两个 run 的递归 diff 退出码为 0 且无输出。

代码冻结点的门禁结果：pytest 160 项通过，Ruff lint、Ruff format、`uv lock --check --offline --no-cache` 和 Git diff check 全部通过。独立红队再次执行零长度与正长度 Data URL 同步重签攻击，均被 fail-closed。

## 5. 轻量验收证据

正式 run 保存在本地：

```text
artifacts/r01/519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d/
```

第二份完整 run 只用于独立验证和递归 diff；以下必要证据保留在 Git 忽略目录：

```text
artifacts/r01/acceptance/519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d/
├── run_a_receipt.json
├── run_b_receipt.json
├── run_a_artifact_manifest.json
├── run_b_artifact_manifest.json
├── validation_a.json
├── validation_b.json
├── determinism.json
├── measurements.json
├── compile_a.time.txt
└── compile_b.time.txt
```

真实 run 和验收证据均不进入 Git。

## 6. R01 capture 结构与损耗画像

v3 保持 R01 的确定性结构事实：1,683 个 capture、9,561 个 boundary、6,301 个唯一 request、175,858 个 event、42,318 个 ActionBatch、56,424 个 pairing record 和 49,800 个严格一对一 pairing。1,248 个 capture 存在未观测 result，119 个存在 compaction，35 个明确截断。

使用 `TEXT_OUTCOME`、无 missing/duplicate result、schema `CONSISTENT`、无 compaction、明确未截断的保守结构筛选，仍剩 353 个 capture，占 20.97%。这些对象仍然不是任务。

本节只能称为 **R01 capture 结构与损耗画像**。TaskEpisode 属于 M2，不能将这些统计称为 episode distribution、生产任务分布或用户真实环境的无偏真值。

## 7. 停止线

本次正式结论停止在 M1B。启动 M1C 前必须继续满足四个硬门：

- `candidate_group_id` 只能是 `BLOCKING_HINT_ONLY`；
- Grade-A 显式关系不得受候选组限制；
- M1C validator 必须独立重算候选组公式；
- `raw_request_hash` 未满足届时冻结的格式契约时只能是 `UNKNOWN`，不得作为关系证据。

本次未实现 M1C、M1D 或 M2。

## 8. 本会话 live 复核（2026-09-01）

本报告 §3–§5 的门禁此前有部分为自报。2026-09-01 会话在现装的 Python 3.12.13 环境（`uv sync --dev --python 3.12`）下现场复跑，把 gate 4/6 及 gate 1/5/7 升级为 live 亲证。所有输出均写入独立临时根，未覆盖冻结 run；复核后临时根已清理。

| 门 | live 复核方式 | 结果 |
| --- | --- | --- |
| 4 独立验证 | 对冻结 run `519a86d3…e06d1d` 现跑 `scripts/validate_m1_run.py` | `{"checked_file_count":10,"event_occurrence_count":175858,"ok":true,"physical_line_count":1683}`，退出码 0 |
| 6 鲁棒性 | 现场 `pytest -q` + `ruff check` + `ruff format --check` | 160 项测试通过，退出码 0；lint 与 format 均 clean |
| 5 确定性 | 两次独立全量重编译 A、B | run ID 均为 `519a86d3…e06d1d`，与冻结 run 相同 |
| 1 来源守恒 | A 对冻结 run 递归 diff（排除 `run_receipt.json`）；A 对 B 递归 diff | 两组均**零差异**；A vs B 全量仅 `run_receipt.json` 的 `completed_at`/`duration_seconds` 不同，`artifact_manifest_sha256`（`9dcdb083…`）、run ID、git tree 全同 |
| 7 资源约束 | 进程内 `resource.getrusage(RUSAGE_SELF)` 采峰值 RSS | A ≈ 45.6 MiB、B ≈ 45.3 MiB，远低于 512 MiB 停止线 |

补充：对新鲜 run A 再跑一次独立 validator，同样 `ok=true`、10 files、1,683 lines、175,858 events。live 复核使用的输入为 560,481,884 字节、SHA-256 `3832d8aa…d260d4d8` 的完整 R01，与冻结值一致。

现场耗时（约 57 秒/次）高于 §4 表中回执时长（约 42 秒/次），因本会话运行于不同硬件与共享盘环境；耗时非确定性业务门，确定性由逐字节 diff 保证，两者不矛盾。

本节把此前自报门升级为 live 亲证，未改变任何冻结字节，也未改变「M1A/M1B v3 正式通过」结论。两项已知项 R1/R4 见 [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md)。
