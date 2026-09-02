# R01 M1B v4 全量验收报告

日期：2026-09-03

范围：M1A Source Adapter、M1B Structural Compiler（compiler contract `trajectory-compiler-m1ab-v4`）

状态：**正式通过。取代 [`r01-m1-v3-validation.md`](r01-m1-v3-validation.md) 的 v3 结论；v3 报告与 v3 run `519a86d3…` 保留为历史，不再被 v4 代码消费。**

## 1. 结论

代码提交 `8f6f65c`（文档提交 `943ba92` 为验收时 HEAD）把 [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md) 中 R4/R5/R7/R8 四项按两个根因一次处理完：上游自报值不再渲染成观测（R4），契约不再保留无生产者状态、业务规则只保留一个定义（R5/R7），误导性字段名改正（R8）。冻结的全量 R01 在本地盘干净克隆上完成两次独立全量编译，两份 run 均通过独立 validator，内容寻址 run ID 与 manifest 摘要一致，排除 `run_receipt.json` 后递归 diff 无差异。

v4 相对 v3 冻结 run 的产物差异被**逐文件、逐字段归因**到上述四项改名/删键与 schema 版本号，10 个产物文件中 6 个逐字节相同，其余 4 个的共同键值全等（§5）。因此 v4 是零业务值变化的纯契约重构，`trajectory-compiler-m1ab-v4` 可以认定为 M1A/M1B 正式通过。结论范围与 v3 相同：只覆盖来源摄取与 capture 内结构编译，不包含跨 capture lineage、QueryTurn、TaskEpisode、任务或环境画像。

## 2. 输入、版本与运行身份

| 项目 | 验收值 |
| --- | --- |
| 输入 | 全量 R01（1,683 行；本仓 `return_data/.../R01.jsonl` 为 368 行截断副本，禁止用作输入） |
| 输入语义契约 | `traceforge.restored-long-capture.v1` |
| dataset ID | `r01-four-batch-202607-v1` |
| SHA-256 | `3832d8aa4ecd577636ce67b56d8798d9fb6311662a7bbce65814e5e8d260d4d8`（编译前 `sha256sum` 现场核对，并经 `--expected-sha256` 由 compiler 再核） |
| 字节数 | 560,481,884 |
| 物理行数 | 1,683 |
| compiler contract | `trajectory-compiler-m1ab-v4` |
| 事件表 schema | `normalized-capture.v3` / `capture-quality.v3` / `event-occurrence.v3` / `attrition-report.v3`（其余表版本不变） |
| TraceForge | `0.3.0` |
| Python | `3.12.13` |
| Git commit | `943ba922270a368440ad5a81028da4beda58508b`（`src/traceforge/trajectory/` 相对 `8f6f65c` 无变化） |
| Git tree | `aa083fbe9d46fba3b786e2d4d375967034b78576` |
| 内容寻址 run ID | `6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed` |
| artifact manifest SHA-256 | `179b82efca85fc6132bfa806d45b081729b0d5627878b0d517e978070fe99eb1` |

两次回执均记录 `available=true`、`dirty=false`、`git_provenance_verified_at_completion=true`，并绑定同一 commit、tree、run ID 和 manifest 摘要。run ID 由 `(contract_version, dataset_id, dataset_sha256, source_schema)` 内容寻址得出，与机器和 provenance 无关；审核方会话此前非正式单跑（见 §6）得到同一 run ID。

## 3. v4 变更与根因

| 项 | 根因 | 改动 | 对 R01 产物的影响 |
| --- | --- | --- | --- |
| R4 | 上游自报值不得渲染成观测 | `InputTruncationStatus` 枚举值改为 `SOURCE_REPORTS_TRUNCATED` / `SOURCE_REPORTS_NOT_TRUNCATED` / `UNKNOWN`；quality reason code `INPUT_TRUNCATED` → `SOURCE_REPORTS_INPUT_TRUNCATED`；attrition 键 `input_truncated_capture_count` → `source_reports_truncated_capture_count` | 仅字符串改名，35 个自报截断 capture 计数不变 |
| R7 | 契约不保留无生产者状态 | 删除 `ProcessingStatus.PARTIAL` 与 `processing_partial_count`（处理终态二态 `COMPLETE` / `QUARANTINED`；三态 eligibility 属 M2） | attrition 少一个恒为 0 的键 |
| R5 | 业务规则只保留一个定义 | 严格 1:1 匹配谓词抽为 `contracts.is_strict_one_to_one_match` 纯函数，compiler 与 validator 共用规则、各自重建输入 | `tool_pairings.jsonl` 逐字节不变（49,800 个严格一对一 pairing 不变） |
| R8 | 字段名须与所测语义一致 | `visible_payload_utf8_byte_length` → `visible_payload_envelope_utf8_byte_length` | 175,858 条事件仅键名变、值全等 |

前三项改产物字节或契约版本，因此 compiler contract 升为 v4，四张受影响事件表 schema 升为 V3。M1C（`src/traceforge/lineage/`）相对其冻结点 `fcff8cf` 仅 `reader.py` 模块文档字符串中的契约名 `V2→V3` 两行随之更新，无运行时代码变化。

## 4. 双全量运行门禁

| 验收项 | 第一次 | 第二次 |
| --- | ---: | ---: |
| 编译回执时长 | 82.359419 秒 | 83.436668 秒 |
| 现场墙钟（含解释器启动） | 95.729856 秒 | 95.272717 秒 |
| 峰值 RSS（`getrusage(RUSAGE_CHILDREN)`） | 37,945,344 bytes | 37,851,136 bytes |
| 512 MiB 停止线 | 通过 | 通过 |
| 独立 validator | `ok=true`，退出码 0 | `ok=true`，退出码 0 |
| validator 文件数 | 10 | 10 |
| validator 来源行数 | 1,683 | 1,683 |
| validator 事件数 | 175,858 | 175,858 |

两次内容寻址 run ID 完全相同；两份 `artifact_manifest.json` 的 SHA-256 均为 `179b82ef…9eb1`。排除只含时间与运行环境差异的 `run_receipt.json` 后，两个 run 的递归 diff 退出码为 0 且无输出。

耗时与 RSS 与 v3 报告（42 秒 / 116 MB）不可直接比较：本次输入位于 AFS 网络盘、解释器与计量方式不同；耗时与内存非确定性业务门，确定性由逐字节 diff 保证。

## 5. v3 → v4 产物逐文件归因

以 v3 冻结 run `519a86d3…` 与本次 run A 的 `artifact_manifest.json` 逐文件比对，再对有差异的文件逐记录比对键集与值：

| 产物 | 记录数 | v3 → v4 | 归因 |
| --- | ---: | --- | --- |
| `private/source_records.jsonl` | 1,683 | 逐字节相同 | — |
| `private/request_boundaries.jsonl` | 9,561 | 逐字节相同 | — |
| `private/action_batches.jsonl` | 42,318 | 逐字节相同 | — |
| `private/tool_pairings.jsonl` | 56,424 | 逐字节相同 | R5 谓词统一零输出影响 |
| `private/tool_catalogs.jsonl` | 18,566 | 逐字节相同 | — |
| `source_manifest.json` | — | 逐字节相同 | — |
| `private/captures.jsonl` | 1,683 | 3,439,217 → 3,449,315 字节 | 仅 `input_truncation_status` 值改名（R4）与 `schema_version` v2→v3；键集与其余值全等 |
| `private/capture_quality.jsonl` | 1,683 | 1,325,154 → 1,325,679 字节 | 仅 reason code `INPUT_TRUNCATED`→`SOURCE_REPORTS_INPUT_TRUNCATED`（R4）与 `schema_version` v2→v3 |
| `private/event_occurrences.jsonl` | 175,858 | 628,581,892 → 630,164,614 字节 | 仅字段改名（R8）与 `schema_version` v2→v3；值全等；字节差 1,582,722 = 175,858 × 9（`_envelope`） |
| `reports/attrition_report.json` | — | 1,713 → 1,693 字节 | 删 `processing_partial_count`（R7）、键改名（R4）、`schema_version` v2→v3；共同键值全等 |

R01 的确定性结构事实因此保持 v3 报告 §6 的数值：1,683 个 capture、9,561 个 boundary、6,301 个唯一 request、175,858 个 event、42,318 个 ActionBatch、56,424 个 pairing record、49,800 个严格一对一 pairing；1,248 个 capture 存在未观测 result，119 个存在 compaction，35 个**源自报**截断（v4 起不再表述为「明确截断」）。

## 6. 反向 sanity 与跨会话复现

- **反向 sanity**：v4 validator 对 v3 冻结 run `519a86d3…` 按设计 fail-closed，`ok=false`，错误全部为 `SCHEMA_VERSION_MISMATCH`（逐记录）与 `COMPILER_CONTRACT_VERSION_MISMATCH`（manifest），共 497,914 条（展示 300 条后 `ERROR_LIMIT_REACHED`），无其它错误码。这是版本跳变的预期行为，不是回归；也说明 v4 代码下任何 M1B 下游只能消费 v4 run。
- **跨会话复现**：审核方会话在 2026-09-02 以 monkeypatch 注入 60s provenance 的方式从同一全量真源非正式单跑得到 run `6be45e01…`（产物留在 `/tmp/tf_v4_audit/m1b`）。本次正式 run A 与之排除 `run_receipt.json` 后递归 diff 无差异。两种 provenance 采集路径、两台会话上下文，业务产物逐字节一致。

## 7. 测试与静态门禁

- 干净克隆 HEAD `943ba92`：`ruff check` 与 `ruff format --check` 通过（59 文件），`uv lock --check --offline --no-cache` 通过，`git status` 为空。
- **完整工作树**（HEAD + 未提交 M1D）：pytest 结果见 §7.1。
- **登记的提交纪律缺口（非 v4 缺陷）**：`tests/conftest.py` 的 `stable_git_provenance` fixture 自提交 `e50e999` 起 monkeypatch `traceforge.query_turns.pipeline`，而 `query_turns/` 至本次验收仍未提交；因此在**干净检出**上运行 pytest，`test_lineage_validation.py` 依赖该 fixture 的 e2e 用例会在 setup 阶段 `ModuleNotFoundError`。该耦合在 M1D 提交后自愈；已登记为 [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md) R9。v4 编译与 validator 本身不依赖测试 fixture，不影响本报告的 run 级结论。

### 7.1 pytest

完整工作树（HEAD `943ba92` + 未提交 M1D）上 `pytest -p no:cacheprovider -q`：验收当日 **259 项全部通过**，其中 M1A/B 及公共模块 162 项、M1C 56 项（`test_lineage_*`）、M1D 41 项（`test_m1d_*`）；M1D 评审整改（规格 v0.3）完成后、提交前复跑 **273 项全部通过**（M1D 增至 55 项，M1A/B、M1C 用例不变）。M1D 用例在 v4 代码上通过，与 §9 所述「v4 对 M1D 产物字节中性」一致；正式 M1D 验收另见其验收报告。

## 8. 证据位置

正式 run 保存在本地：

```text
artifacts/r01/6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed/
```

第二份完整 run 只用于独立验证和递归 diff；以下必要证据保留在 Git 忽略目录：

```text
artifacts/r01/acceptance/6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed/
├── run_a_receipt.json / run_b_receipt.json
├── run_a_artifact_manifest.json / run_b_artifact_manifest.json
├── validation_a.json / validation_b.json
├── compile_a.json / compile_b.json            # 墙钟与峰值 RSS 计量
├── determinism.json                           # A/B 递归 diff、跨会话复现、v3→v4 逐文件归因
├── measurements.json
└── reverse_sanity_v4_validator_on_v3_run.txt  # v4 validator 对 v3 run 的完整输出
```

真实 run 和验收证据均不进入 Git。v3 run `519a86d3…` 及其证据目录保留、只读。

## 9. 连带与停止线

- M1C v2 正式 run `978c0347…` 绑定的是 v3 M1B run；v4 通过后必须用 `6be45e01…` 重跑 M1C（代码零变化，输入身份变化 → 新 lineage run ID）并补验收，记录在 [`r01-m1c-validation.md`](r01-m1c-validation.md)。
- M1D reader 对上游 M1B run 先执行 `validate_compiled_run`，因此 M1D 正式验收只能建立在 `6be45e01…` 之上。
- 本次未实现或改动 M1C、M1D 或 M2 的任何运行时代码。
