# TraceForge v4 完成链交接文档（2026-09-02）

> **完成状态（2026-09-03）：①②③ 全链已按序执行并正式验收，本文归档只读；当前权威停点见 [`session-handoff.md`](session-handoff.md)。** 结果对照本文 §8 DoD：
>
> | 阶段 | 结果 | 证据 |
> | --- | --- | --- |
> | ① M1B v4 | run `6be45e01…` 干净克隆双跑复现、validator 10 files / 1,683 lines / 175,858 events、v4 validator 对 v3 run 按预期 fail-closed | [`r01-m1b-v4-validation.md`](r01-m1b-v4-validation.md)，`artifacts/r01/acceptance/6be45e01…/` |
> | ② M1C v2 重跑 | 新 lineage run `9ff708d9…`（绑定 `6be45e01…`）双跑一致、validator 5 files、九项计数与 §8.1 逐项吻合；字节相对 `978c0347…` 全变但剥离 run 绑定 ID 后集合级零变化（本文 §6 的"字节需实测"已实测） | [`r01-m1c-validation.md`](r01-m1c-validation.md)，`artifacts/r01/acceptance_m1c/9ff708d9…/` |
> | ③ M1D | D1–D5/§3/§4 待办全部处置（[`m1d-review-20260902.md`](m1d-review-20260902.md) 头部处置表）；规格升 v0.3；单独提交 `1c588be`；run `84d826b3…` 双跑复现、validator 8 files、13 项计数与 §4 审核向量逐项吻合 | [`r01-m1d-validation.md`](r01-m1d-validation.md)，`artifacts/r01/acceptance_m1d/84d826b3…/` |
> | 停点 / 已知项 | `session-handoff.md` 推进到"M1D 验收通过"；`m1ab-v3-known-items.md` v1.3 关闭 R4/R5/R7/R8、登记并（随 `1c588be`）自愈 R9 | 干净克隆全量 pytest 273 项绿、`ruff check`/`format --check` clean、`uv lock --check --offline` 通过 |
>
> 与 §1「提交仅在用户明确要求时」的偏差说明：用户已授权本会话对项目做整体开发与验收，且验收报告须绑定 `dirty=false` 的提交（run 回执记录 commit/tree），故按 §7b/[`session-handoff.md`](session-handoff.md) §13 纪律做了**本地**提交（M1D 代码与文档分开），**未推送**，未改任何冻结 run 字节。分支 `m1c-lineage` 现领先 `origin/main` 14+ 个提交。
>
> 本文 §4 审核向量中的 `eligible=1683 quarantined=0` 与 `processing_status 全 COMPLETE` 三项在 M1D v0.3 已随 D4/D5 删除（无生产者状态），其余计数不变。

> 状态：**交接稿**，供接手模型执行。本文只描述「如何把当前 v4 工作收尾并正式验收」，不改任何模块契约；与模块规格冲突时以 `r01-processing-spec.md`（M1A/B）、`m1c-processing-spec.md`（M1C）、`m1d-processing-spec.md`（M1D）为准，与开发纪律冲突时以 [`../AGENTS.md`](../AGENTS.md) 为准。本文由「审核方会话」在单跑审核 v4 后写成，未提交、未推送。

## 0. 一句话现状与临界路径

三块工作被一次 **compiler contract 版本跳变（m1ab-v3 → v4）**绑成一条**必须按序执行**的链：

```
① M1B v4 正式验收（代码已落 8f6f65c，未验收）
   └─必须先做，因为 v4 改了契约版本 + 四张事件表 schema
        ↓ 产出新的 M1B v4 run（内容寻址 id 与 v3 不同）
② M1C v2 用新 M1B v4 run 重跑 + 补验收（M1C 代码不变，仅输入身份变）
        ↓
③ M1D 完成（补 review 待办 → 干净提交 → 正式验收）
   └─M1D reader 会对上游 M1B run 跑 validate_compiled_run；
     v4 代码遇 v3 run 必报版本不匹配，故 M1D 正式验收只能排在 ① 之后
```

**为什么不能跳序**：v4 validator 对 v3 冻结 run（`519a86d3…`）按设计 fail-closed（`SCHEMA_VERSION_MISMATCH` / `COMPILER_CONTRACT_VERSION_MISMATCH`）——这是预期行为，不是回归。任何消费 M1B 的下游（M1C/M1D）在 v4 代码下都只能吃 v4 run。

---

## 1. 环境与铁律（开工前必读，违反会白跑或污染冻结产物）

| 项 | 事实 / 做法 |
|---|---|
| Python | 用 `.venv/bin/python`（实测 3.12.13，能 import traceforge）；代码用 PEP695 `type` 语句必须 3.12 |
| **SSL_CERT_FILE 毒化** | **所有** `uv`/`python`/`pytest`/`ruff` 命令前置 `env -u SSL_CERT_FILE`；否则 `SSL_CERT_FILE=~/.local/share/mkcert/rootCA.pem` 会毒化 TLS |
| **provenance 5s 超时** | 本 AFS 网络盘 `git status --porcelain` 要 ~6–12s，超过 `provenance.py::_run_git` 硬编码 `timeout=5` → `available=False` → validator 报 `RUN_RECEIPT_GIT_PROVENANCE_UNVERIFIED`。**正式验收 run 必须在本地盘干净克隆上执行**（git status 快 < 5s，provenance 自然 verified=True），**不要**用 monkeypatch 绕过（monkeypatch 仅允许用于测试与非正式审核）|
| **全量真源 R01** | 在 `/mnt/afs_toolcall/juxiaolong1/Projects/DataFilter_v2/gpt56sol/domain/by-rubric/R01.jsonl`（1683 行 / 560,481,884 字节 / SHA-256 `3832d8aa4ecd577636ce67b56d8798d9fb6311662a7bbce65814e5e8d260d4d8`）。**本仓 `return_data/.../R01.jsonl` 是 368 行截断副本，禁止用作输入。** |
| 冻结字节 | 绝不改冻结 M1B run `519a86d3…`、M1C v2 run `978c0347…` 的字节；所有新 build 写新 artifacts 根或 /tmp |
| 无硬编码 | 生产码（compiler/validator）不硬编码 R01 路径/摘要/统计；源 SHA 只作 CLI `--expected-sha256` 或验收证据出现 |
| 不透明外键 | 不 import M1B/M1C 私有实现（compiler/pipeline/source_adapter/私有 ID 公式）；上游 ID 当不透明外键 |
| 语言/提交 | 中文文档/注释/提交，格式 `<类型>: <中文摘要>`，尾行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`；**提交仅在用户明确要求时**；M1D 不与 M1C/文档改动混入同一提交（[`session-handoff.md`](session-handoff.md) §13）|
| 无模型调用 | M1 任何阶段（含 M1C/M1D）零 LLM |

---

## 2. 冻结事实与身份表（所有 hash 以此为准）

**代码冻结点 / 提交**
- M1B v3 冻结点：commit `c3c0a8fb6ed9927d5bebba55c616e1ef524c653b`，tree `5438c8d3…`，contract `trajectory-compiler-m1ab-v3`
- M1C v2 冻结点：commit `fcff8cf88e7edcd645484318fd8bd12de50b7af7`，tree `482b2a72…`，contract `lineage-compiler-m1c-v2`（`src/traceforge/trajectory/` 相对 `c3c0a8f` 无变化）
- **M1B v4（本次收尾对象）**：commit `8f6f65c`（2026-09-02 17:45，重构代码），文档 `943ba92`（17:47）= 当前 HEAD，contract `trajectory-compiler-m1ab-v4`
- 分支 `m1c-lineage`，领先 `origin/main` **12** 个提交，**未推送**

**已发布 run（本地在，只读）**
- M1B v3 run：`519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d`，manifest SHA `9dcdb083…9876`（`artifacts/r01/519a86d3…/`；证据 `artifacts/r01/acceptance/519a86d3…/`）
- M1C v2 run：`978c0347b4198223cada09ccfd7bf55cf50a42a0ee7a049d55feedf793c7e12e`，manifest SHA `837c9da7…`，绑定 M1B v3（`artifacts/r01/lineage/978c0347…/`；证据 `artifacts/r01/acceptance_m1c/978c0347…/`）

**M1D 现状：整份未提交（工作树未跟踪）**
- `src/traceforge/query_turns/`（builder/contracts/view/reader/pipeline/validation）
- `scripts/validate_m1d_run.py`
- `tests/test_m1d_{builder,pipeline,reader,validation}.py`
- `docs/m1d-processing-spec.md`（v0.2 评审稿）、`docs/m1d-review-20260902.md`（评审意见 D1–D5）
- 已修改（未提交）：`src/traceforge/cli.py`、`tests/test_cli.py`（加 `query-turns build` 子命令）

**数据集参数**
- `dataset_id = r01-four-batch-202607-v1`
- `source_schema = traceforge.restored-long-capture.v1`

---

## 3. v4 到底改了什么（M1B v4 变更清单）

来自 commit `8f6f65c`，对应已知项 [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md) R4/R5/R7/R8，归两个根因（① 上游自报值不得渲染成观测；② 契约不保留无生产者状态、业务规则只一个定义）：

| 项 | 改动 |
|---|---|
| R4 | `InputTruncationStatus` 枚举值改为 `SOURCE_REPORTS_TRUNCATED` / `SOURCE_REPORTS_NOT_TRUNCATED` / `UNKNOWN`（不再把源自报渲染成 `OBSERVED_*`）|
| R7 | 删除 `ProcessingStatus.PARTIAL` 与 `processing_partial_count`（M1B 处理终态二态：`COMPLETE`/`QUARANTINED`；三态 eligibility 属 M2）|
| R5 | 严格 1:1 匹配谓词抽为 `contracts.is_strict_one_to_one_match` 纯函数，compiler 与 validator 共用规则、各自重建输入 |
| R8 | `visible_payload_utf8_byte_length` → `visible_payload_envelope_utf8_byte_length` |
| — | 四张事件表 schema 升 `V3`；`COMPILER_CONTRACT_VERSION = trajectory-compiler-m1ab-v4` |

改判备注（K3）：`m1c-known-items.md` 中「几千 capture 共享一个 `source_request_id」不在 M1C 用常数兜底，改为「应在 M1A 输入契约拒收」；K1/K2 已由 M1C v2 关闭（IDENTICAL 关系及门④已删，「上游不透明摘要不入任何关系证据」为通用原则）。

---

## 4. 审核方已做的事 + 可复用证据（强烈建议接手模型先读）

审核方（本文作者会话）在收尾前做了一次 **v4 全量真源审核单跑**（非正式验收），关键产出可直接当验收阶段的**预期值 checkpoint**：

- 用 `.venv/bin/python`、注入真实 HEAD provenance（60s 超时 runner，规避 5s 坑）、`--expected-sha256 3832d8aa…`，从全量真源现建 M1B v4 → M1D → 双参 validator。脚本留在 `/tmp/tf_v4_audit.py`（参考实现，非生产码；产物在 `/tmp/tf_v4_audit/{m1b,m1d}`，m1b≈706M/m1d≈14M，可删可重建）。
- **M1B v4 run_id（内容寻址，与机器/provenance 无关）= `6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed`。** 因 run_id = `stable_id(contract_version, dataset_id, dataset_sha256, source_schema)`，**干净克隆上的正式验收 build 必须复现这个 run_id**；不一致即说明源或代码不对。M1B manifest 10 文件。
- **M1D run_id = `84d826b3d7abddffb5f8fde28d7e5592590720bd6909c24a8b17b7f0c70b66ea`**（绑定 M1B v4 run_id + 其 artifact_manifest SHA，均 provenance 无关 → 正式验收应复现）。
- **v4 的 M1D `observed_counts` 与 v3 冻结 run 逐项完全一致**（v4 对 R01 的 M1D 产物字节中性——因 M1D 只消费 typed reader 冻结标量 + 配对记录 + terminal_status，未碰被改名/删除的字段）。审核向量（验收时应逐项吻合）：

  ```
  capture=eligible=1683  quarantined=0
  query_turn=2610 = prefix_rooted 1683 + observed_rooted 927
  complete_turn=1200  incomplete_turn=1410
  agent_step=14375（含工具 13175 / 纯文本 1200）
  user_block=927  thread_turn_edge=927  orphan=324（集中在 9 capture）
  has_compaction=119  processing_status 全 COMPLETE
  ```
  validator `ok=True`，8 文件。

- **结论**：M1B v4 对 M1D 无正确性影响；M1D 在 v4 上唯一债务是**文档漂移**（见 §7 D5）。这为「② M1C 重跑、③ M1D 验收」提供了强预期：产物计数不应变。

---

## 5. 阶段① — M1B v4 正式验收

**前置**：完整 R01（先 `sha256sum` 核对 `3832d8aa…`）；在**本地盘干净克隆**上、checkout 到 `8f6f65c`（或确认 HEAD `trajectory/` 相对 `c3c0a8f` 只含 `8f6f65c` 的变化：`git diff c3c0a8f..HEAD -- src/traceforge/trajectory pyproject.toml uv.lock`）。

**步骤（两次全量重编，A/B 独立根）**：
```bash
env -u SSL_CERT_FILE .venv/bin/python -m traceforge trajectory compile \
  --input <完整R01路径> \
  --dataset-id r01-four-batch-202607-v1 \
  --source-schema traceforge.restored-long-capture.v1 \
  --expected-sha256 3832d8aa4ecd577636ce67b56d8798d9fb6311662a7bbce65814e5e8d260d4d8 \
  --output <root_A>
# 重复到 <root_B>
```

**验收门**（全绿才算过）：
- 两次都产 run_id `6be45e01…`（§4）；
- 递归 diff（排除 `run_receipt.json`）A vs B **无差异**；
- `env -u SSL_CERT_FILE .venv/bin/python scripts/validate_m1_run.py <run>` → `ok=true`、**10 files / 1,683 lines / 175,858 events**（沿用 §8 基线，v4 不改事件数；以实跑为准）；
- `pytest`（M1A/B ≈153 项）+ lineage 回归全绿、`ruff check`/`format --check` clean、`uv lock --check --offline --no-cache` 通过；
- 记录两次 RSS；
- **反向 sanity**：v4 validator 对 v3 冻结 run `519a86d3…` **预期失败**于 schema/contract 版本——确认失败是「预期」而非新 bug。

**产出**：证据落 `artifacts/r01/acceptance/6be45e01…/`（两次 receipt/manifest/validator 输出/diff 结论/RSS 日志）；写 `docs/r01-m1b-v4-validation.md`（或把 `r01-m1-v3-validation.md` 升为 v4 并保留 v3 历史）；更新 [`session-handoff.md`](session-handoff.md) §5/§8 的冻结点与证据。

---

## 6. 阶段② — M1C v2 在新 M1B v4 run 上重跑 + 补验收

**前置**：① 完成、M1B v4 run `6be45e01…` 已发布。先确认 **M1C 代码零变化**：`git diff fcff8cf..HEAD -- src/traceforge/lineage` 为空（只输入身份变）。

**步骤（干净克隆、两次）**：
```bash
env -u SSL_CERT_FILE .venv/bin/python -m traceforge lineage build \
  --m1b-run <6be45e01… run 目录> --output <root_A>   # 再到 root_B
env -u SSL_CERT_FILE .venv/bin/python scripts/validate_m1c_run.py \
  <lineage_run> <6be45e01… run 目录>
```

**验收门**：
- 产出**新的 lineage run_id**（≠ `978c0347…`，因绑定 m1b_run_id 变）——计算并记录；
- validator `ok=true`、**5 files**；
- **业务边计数与 `m1c-processing-spec.md` §4.4 / [`session-handoff.md`](session-handoff.md) §8.1 逐项一致**：`SHARED_SOURCE_REQUEST` 1671、`EXPLICIT_REQUEST_SUCCESSOR` 4994、`IDENTICAL_RAW_REQUEST_HASH` **0（v2 已删该关系）**、`COMPLETE_DUPLICATE_CAPTURE` 0、candidate_group 956、request_node 6301、qualified 1675、unknown 8；
- 递归 diff A vs B（排除 receipt）无差异；
- **注意（不要假设）**：不要断言 `request_nodes.jsonl`/`request_successor_edges.jsonl` 与 `978c0347…` **逐字节**一致——v4 改了 `visible_payload_*` 字段名，若 M1C 有透传会致字节变。**计数是契约，字节需实测**；若字节变，确认仅因字段改名、计数不变即可接受。

**产出**：证据落 `artifacts/r01/acceptance_m1c/<新 run>/`；更新 `r01-m1c-validation.md` 与 [`session-handoff.md`](session-handoff.md) §8.1 的 run_id/绑定。

---

## 7. 阶段③ — M1D 完成（review 待办 → 提交 → 正式验收）

**前置**：①② 完成，M1B v4 run 可用（M1D reader 先 `validate_compiled_run` 上游，v3 run 会被拒）。

### 7a. 先处理 [`m1d-review-20260902.md`](m1d-review-20260902.md) 的待办

| 项 | 动作 | 备注（含审核方实证） |
|---|---|---|
| **D1**（必做） | 在 `validation.py` 加**分区守恒断言**：每个 eligible capture 的观测事件 ID 集合 == `UserBlock.event_ids ∪ AgentStep.assistant_event_id ∪ AgentStep.tool_observation_event_ids ∪ 孤儿 ∪ SYSTEM ∪ TOOL_CALL(经 batch)`，且各子集两两不交；配一个篡改测试 | 当前只有 bijection + 记账字段复算，缺此正交检查。这是审核方之前独立提的同一条 |
| **D2** | `parallel_semantics` 无 batch 时写 `None`（规格 §2 却写「缺省 UNKNOWN」）——二选一并同步规格 | 实测 `{UNKNOWN:13175, None:1200}`，`None` 恰对应纯文本终止步；且有 batch 时恒 `UNKNOWN`（M1B 把 `execution_semantics` 固定成 UNKNOWN），此字段对「并行/串行」零信息量。建议**保留 None、改规格措辞** |
| **D3** | `CaptureTurnAccountingV1` 字段以实现为准修订规格 §2（`observed_event_counts_by_kind`/`agent_step_count`/`processing_status`；`observed_user_event_count` 可派生不物化）| 规格 §2 应已同步，复核一遍 |
| **D4** | `QUARANTINED` 分支可达性：M1B 的 QUARANTINED capture **无 `captures.jsonl` 行**，reader 读不到 → 分支不可达。决定**删分支 + 删规格 §6 该句**，或保留并注明 | 实测 R01 `quarantined=0` |
| **D5**（v4 连带，必做） | 删死表述 `processing_status`「显式暴露 PARTIAL」（`query_turns/contracts.py:165`）；订正旧字段名 `visible_payload_utf8_byte_length`（`contracts.py:311` 注释 + 规格 §1 勘误段）为 `visible_payload_envelope_utf8_byte_length`；规格 §6「COMPLETE/PARTIAL→产回合」散文删 PARTIAL | v4 已删 PARTIAL；这几处纯文档漂移 |
| §3 | 终态交叉核对对象措辞（末 turn vs 观测窗末 ASSISTANT，实现与 M1B oracle 一致，改规格措辞）；配对索引末写覆盖可选加 tripwire；空观测流加防御性测试 | |
| §4 测试缺口 | 补：D1 分区守恒篡改用例；「末步有 batch 且结果全到仍 INCOMPLETE」（规格 D-a）单独用例；配对重复 tripwire；空观测流 | |

### 7b. 干净提交 M1D（与 M1C/文档分开，[`session-handoff.md`](session-handoff.md) §13）
提交 `query_turns/`、`validate_m1d_run.py`、`test_m1d_*.py`、`m1d-processing-spec.md`、`cli.py`/`test_cli.py` 改动。**仅在用户要求时提交。**

### 7c. 正式验收（与 M1C 同构，干净克隆、两次）
```bash
env -u SSL_CERT_FILE .venv/bin/python -m traceforge query-turns build \
  --m1b-run <6be45e01… run 目录> --output <root_A>   # 再到 root_B
env -u SSL_CERT_FILE .venv/bin/python scripts/validate_m1d_run.py \
  <m1d_run> <6be45e01… run 目录>
```
**验收门**：
- 两次产 M1D run_id `84d826b3…`（§4）；递归 diff（排除 receipt）无差异；
- validator `ok=true`、**8 files**；`observed_counts` 与 §4 审核向量逐项吻合；
- 全测试套件绿（M1C 检查点 230 + M1D 未提交约 271，加新增 D1/D-a/tripwire/空流用例，以实跑为准）、ruff clean、lock 通过；
- RSS 记录。

**产出**：写 `docs/r01-m1d-validation.md`；更新 [`session-handoff.md`](session-handoff.md) §3/§7/§11 停点为「M1D 验收通过」，并把 R7/PARTIAL 对 M1D 的连带、D1 关闭登记进 known-items。

---

## 8. 完成定义（DoD — 接手模型据此自证整条链完成）

- [x] `git diff c3c0a8f..HEAD -- src/traceforge/trajectory` 只含 `8f6f65c`（M1B v4）变化，无其它未验收改动（`git log c3c0a8f..HEAD -- src/traceforge/trajectory` 仅 `8f6f65c`；`lineage/` 相对 `fcff8cf` 仅 reader 模块 docstring `V2→V3` 两行）；
- [x] **①** M1B v4 run `6be45e01…` 双跑一致 + validator 10 files/175,858 events，证据入 `acceptance/6be45e01…/`，[`r01-m1b-v4-validation.md`](r01-m1b-v4-validation.md) 已写；
- [x] **②** M1C 在 `6be45e01…` 上的新 lineage run `9ff708d9…` 双跑一致 + validator 5 files + 计数吻合 §8.1，证据入 `acceptance_m1c/9ff708d9…/`，[`r01-m1c-validation.md`](r01-m1c-validation.md) 已更新；
- [x] **③** M1D 已提交（`1c588be`）+ run `84d826b3…` 双跑一致 + validator 8 files + 计数吻合 §4 向量，D1–D5/§3/§4 待办已处理，[`r01-m1d-validation.md`](r01-m1d-validation.md) 已写；
- [x] `session-handoff.md` 停点推进到「M1D 验收通过」，`m1ab-v3-known-items.md` v1.3 已更新；
- [x] 未推送任何东西、未改冻结字节；提交为本地提交（见文首偏差说明）。

---

## 9. 已知项 / 不做项（承接现有登记，勿擅自扩张）

- **接受、不改冻结代码**：R1（隐私脱敏续行只认 CR/LF，R01 未触发、产物 0 base64）；R6（validator 能力边界陈述，非缺陷）。
- **不做**：Grade-B `NORMALIZED_VISIBLE_PREFIX_OF`、已删的 `IDENTICAL_RAW_REQUEST_HASH`、任何以上游不透明摘要为依据的关系/状态；以 M1C 边做 capture 硬去重/合并；TaskEpisode、ObservedTaskDistribution、World/Truth/Reference/Verifier、可解性/难度、Harbor/AGS/Hermes 集成——全部 M2 及以后，进 M2 前须先冻结带来源的 `SourceAnnotationProjection`（[`m2-source-projection-spec.md`](m2-source-projection-spec.md)）。
- 权威索引与更细背景见 [`session-handoff.md`](session-handoff.md)（§10 硬停止线、§13 开工检查表、§14 明确禁止项）。

---

## 10. 交接给接手模型的最短开工序列

```bash
cd <干净本地盘克隆>/traceforge   # 不要在 AFS 盘做正式 run
env -u SSL_CERT_FILE .venv/bin/python -m pytest -p no:cacheprovider -q
env -u SSL_CERT_FILE .venv/bin/ruff check --no-cache . && \
env -u SSL_CERT_FILE .venv/bin/ruff format --no-cache --check .
git diff c3c0a8fb6ed9927d5bebba55c616e1ef524c653b..HEAD -- src/traceforge/trajectory
git diff fcff8cf88e7edcd645484318fd8bd12de50b7af7..HEAD -- src/traceforge/lineage   # 应为空
sha256sum <完整R01路径>   # 必须 3832d8aa…
# 然后按 §5 → §6 → §7 顺序执行
```
