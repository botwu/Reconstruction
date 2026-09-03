# M2 ① `TurnEvidence` 处理规格（v0.1 草案，待评审冻结）

状态：草案。依 [`../AGENTS.md`](../AGENTS.md) 规格先行，评审冻结前不写代码。位置见 [`session-handoff.md`](session-handoff.md) §11 的 M2 拆解：① 是 ②③④ 的唯一输入，本身确定性、零模型。

目标一句话：把已验收的 M1B + M1D + `UserTextProjection` 三个 run **按 `QueryTurn` 逐条 join** 成一张闭合值域的"回合证据包"表，并按用途给出 eligibility（[`overall-plan.md`](overall-plan.md) §5），使后续模型层（③）只看证据包、不看原始 run，聚合层（②④）只做计数。不新造单元、不读正文、不做任何语义判断。

---

## 0. 三条实测地基（只读探针，冻结 run `6be45e01…`/`84d826b3…`/`47cfac20…`，数字为人工 sanity 观测，不进代码）

1. **回合全集 2,610 = 1,683 `PREFIX_ROOTED`（每 capture 恰一个）+ 927 `OBSERVED_ROOTED`。** 927 个观测回合的 UserBlock 按 `text_class` 组成：638 只有 1 条 `PLAIN_USER_TEXT`，238 是 `HARNESS_CONTEXT`+`PLAIN_USER_TEXT`（Harness 注入紧贴用户文本），885 个含 ≥1 条 `PLAIN_USER_TEXT`；**32 个只有 Harness 注入、10 个只有控制信号**（`turn_aborted` 13 次、`subagent_notification` 2、`user_interjection` 2）——这 42 个"回合"结构上存在但没有任何用户意图证据，必须显式 INELIGIBLE 而不是当作 query。
2. **前缀是 80% capture 的唯一意图来源。** 1,680 个 capture 的不可定位前缀里共有 10,802 条 `PLAIN_USER_TEXT`（每 capture 1–15+ 条，中位 3–4），只有 330 个 capture 在观测窗口内有用户文本，且这 330 个的前缀里也都有。3 个 capture 完全没有用户文本。前缀事件属于 capture 但不能定位到 boundary（M1B §183），所以前缀意图证据只能以**整组有序事件 ID**交给 ③ 并标 `PREFIX_ONLY`（[`m2-source-projection-spec.md`](m2-source-projection-spec.md) §2.5），① 不得挑"最后一条"当根 query——那是语义推断。
3. **终态缺失是系统性的，不是脏数据。** 1,211/1,683 capture 以 `TOOL_CALL_PENDING` 结束，885 个有意图的观测回合里 337 个 INCOMPLETE；22 个回合 0 个 AgentStep（UserBlock 后无任何 assistant）；有意图回合的 AgentStep 里 469 步带未观测结果的调用。原因是回流按请求观测，末次请求的工具结果天然不在本 capture 内（可能在 M1C 同组的后继 capture）。因此 eligibility 必须**按用途分开**：outcome 缺失只降 task_profile 到 PARTIAL，不影响 environment_profile；反之工具结果未观测只降 environment_profile。

此外：`ActionBatchV2.execution_semantics` 在 R01 恒为 `UNKNOWN`（无信息量，① 不透传）；`CaptureQualityV3.privacy_status` 恒为 `DERIVATION_POLICY_APPLIED`（无变异，① 不设隐私维度——契约不保留无生产者状态）；工具名是 M1B 公开 payload 标量（调用 217 个 distinct、声明 1,233 个 distinct、跨多种 harness），但**不是闭合值域**，① 只存事件 ID，工具名由 ② 经 ID 解引用。

---

## 1. 输入契约（只读，先校验后消费）

三个已发布 run，各自先跑权威 validator（M1B `validate_compiled_run`、M1D `validate_query_turn_run`、投影 `validate_user_text_projection_run`），且 M1D 与投影 manifest 绑定的 M1B run ID/manifest SHA 必须与所给 M1B run 一致；投影 run **必须**绑定所给 M1D run（未绑定则拒绝：① 依赖 `user_block_id` 回指）。任何一项失败 → fail-closed。

| 来源 | 消费字段 | 用途 |
|---|---|---|
| M1D `QueryTurnV1` | `query_turn_id`、`capture_occurrence_id`、`turn_ordinal`、`root_status`、`user_block_id`、`agent_step_ids`、`assistant_outcome_id`、`turn_status` | 单元全集与主键；终态 |
| M1D `UserBlockV1` | `user_block_id`、`event_ids`、`has_non_text_blocks` | 观测回合的 USER 事件集合、附件位 |
| M1D `AgentStepV1` | `agent_step_id`、`tool_observation_event_ids`、`unresolved_tool_call_event_ids` | 调用/观测/未解析计数 |
| 投影 `UserTextAnnotationV1` | `event_occurrence_id`、`capture_occurrence_id`、`locality`、`user_block_id`、`text_class`、`utf8_byte_length` | 意图证据筛选（仅 `PLAIN_USER_TEXT`）、Harness/控制信号计数、证据字节数 |
| M1B `EventOccurrenceV3` | `event_occurrence_id`、`sequence_number`（仅 USER 事件） | 前缀意图证据的稳定排序 |
| M1B `NormalizedCaptureV3` | `capture_occurrence_id`、`input_truncation_status`、`has_compaction` | capture 级原因码 |
| M1B `CaptureQualityV3` | `capture_occurrence_id`、`tool_schema_status` | capture 级原因码 |

**一律不读**：任何正文、`reasoning_content`、工具名、参数、URL、路径、`domain_meta`。M1C 不消费（跨 capture 留 ④）。

---

## 2. 输出契约（不可变、闭合值域、逐字节可复现）

**`TurnEvidenceV1`**（每个 M1D `QueryTurn` 恰一条，主键沿用 `query_turn_id`，不另铸 ID——① 是既有单元的装饰，不是新实体）：

| 字段 | 值域 | 来源/派生 |
|---|---|---|
| `schema_version` | 常量 | |
| `query_turn_id`、`capture_occurrence_id`、`turn_ordinal`、`root_status`、`turn_status` | 透传 M1D | |
| `intent_locality` | `OBSERVED` \| `PREFIX_ONLY` | `root_status` 的固定函数（OBSERVED_ROOTED→OBSERVED；PREFIX_ROOTED→PREFIX_ONLY） |
| `intent_evidence_event_ids` | 有序事件 ID 元组 | OBSERVED：UserBlock 内 `text_class=PLAIN_USER_TEXT` 的事件，按 UserBlock `event_ids` 原序；PREFIX_ONLY：该 capture 内 `locality=PREFIX_UNLOCALIZED ∧ text_class=PLAIN_USER_TEXT` 的全部事件，按 M1B `sequence_number` 升序 |
| `intent_evidence_utf8_byte_length` | 计数 | 上述事件 `utf8_byte_length` 之和（③ 的预算依据） |
| `harness_event_count`、`control_signal_event_count`、`other_user_event_count` | 计数 | UserBlock 内 `HARNESS_*` / `CONTROL_SIGNAL` / 其余（`EMPTY_TEXT`、`UNKNOWN_TAGGED`、`NO_LEADING_TEXT`）事件数；PREFIX_ROOTED 恒 0（前缀不细分到回合，只在投影报告里全局计数） |
| `has_non_text_blocks` | bool | 透传 UserBlock；PREFIX_ROOTED 恒 `false` |
| `agent_step_count`、`tool_observation_count`、`unresolved_tool_call_count` | 计数 | 对 `agent_step_ids` 求和 |
| `task_profile`、`task_profile_reasons` | `ELIGIBLE` \| `PARTIAL` \| `INELIGIBLE`；原因码有序元组 | §4 固定函数 |
| `environment_profile`、`environment_profile_reasons` | 同上 | §4 固定函数 |

原因码闭合全集（每个都必须有生产者，测试向量逐个覆盖）：`NO_INTENT_EVIDENCE`、`INTENT_PREFIX_ONLY`、`OUTCOME_NOT_OBSERVED`、`NO_AGENT_STEP`、`TOOL_RESULT_NOT_OBSERVED`、`TOOL_SCHEMA_UNRELIABLE`、`SOURCE_REPORTS_INPUT_TRUNCATED`、`UNLOCALIZED_COMPACTION_EVIDENCE`。

**不产出** `reconstruction` 用途：在 ① 可用的结构事实下它恒等于 `task_profile=ELIGIBLE`（COMPLETE 蕴含 ≥1 AgentStep），无独立信息量；plan §7.1 的重建准入门需要语义判断（闭合 TaskIntent、Truth 可构造），留 M2/M3 边界。

**报告**（`reports/turn_evidence_report.json`，只有计数，键闭合）：`query_turn_count`；`{root_status}×{task_profile}` 6 键；`{root_status}×{environment_profile}` 6 键；8 个原因码各一计数；`intent_evidence_event_count_observed`、`intent_evidence_event_count_prefix_only`；`captures_with_eligible_task_turn_count`、`captures_with_any_intent_evidence_count`。

---

## 3. 硬门

- **门① 纯确定性、零模型、不读正文。** 全部字段只依 §1 标量；同输入逐字节一致。
- **门② 不新造单元、不挑根 query。** 单元 = M1D `QueryTurn` 全集（含 PREFIX_ROOTED），一条不多一条不少；前缀意图证据整组交付，不选"最后一条"。
- **门③ 闭合值域。** 私有表只有 ID/序数/枚举/bool/计数；工具名、标签名、正文一律不落盘。
- **门④ 按用途分离的 eligibility，每个非 ELIGIBLE 都有原因码。** 无全局 `valid`；原因码全集中不得有 fold 永不产出的码。
- **门⑤ 先校验后消费 + 身份绑定。** 三个上游 validator 全过且身份链一致才消费；manifest 记录三方 run ID 与 manifest SHA。

---

## 4. 固定函数（`contracts.py` 单一定义，builder 与 validator 共用）

```text
intent_locality(root_status) = OBSERVED if OBSERVED_ROOTED else PREFIX_ONLY

task_profile(intent_evidence_count, intent_locality, turn_status):
  count == 0                          → INELIGIBLE, [NO_INTENT_EVIDENCE]
  else reasons = [INTENT_PREFIX_ONLY if PREFIX_ONLY] + [OUTCOME_NOT_OBSERVED if INCOMPLETE]
       → ELIGIBLE if reasons 为空 else PARTIAL

environment_profile(agent_step_count, unresolved_tool_call_count,
                    tool_schema_status, input_truncation_status, has_compaction):
  agent_step_count == 0               → INELIGIBLE, [NO_AGENT_STEP]
  else reasons = [TOOL_RESULT_NOT_OBSERVED if unresolved > 0]
               + [TOOL_SCHEMA_UNRELIABLE if tool_schema_status != CONSISTENT]
               + [SOURCE_REPORTS_INPUT_TRUNCATED if input_truncation_status == SOURCE_REPORTS_TRUNCATED]
               + [UNLOCALIZED_COMPACTION_EVIDENCE if has_compaction]
       → ELIGIBLE if reasons 为空 else PARTIAL
```

原因码顺序即上文列出顺序（稳定、可比较）。`tool_schema_status` 的非 `CONSISTENT` 三值（`CONFLICT_REPORTED`/`INFERRED`/`INFERRED_AND_CONFLICT`）合并为一个原因码：plan §5 的处理只有一种——"只记录冲突，不生成硬工具契约"，细分对 ②③④ 无消费者（YAGNI；② 需要细分时直接读 M1B）。

---

## 5. 处理逻辑（纯函数 fold，逐 capture，无 IO）

1. 读三个 run 的 §1 视图；按 `capture_occurrence_id` 分组。
2. 对每个 capture：建 `event_id → annotation` 索引；前缀意图证据 = 该 capture 的 `PREFIX_UNLOCALIZED ∧ PLAIN_USER_TEXT` 事件按 `sequence_number` 排序（缺 `sequence_number` → 输入违约，fail-closed）。
3. 对每个 `QueryTurn`：OBSERVED_ROOTED 取 UserBlock 事件逐条查注解并计数；PREFIX_ROOTED 用步 2 的前缀证据。对 `agent_step_ids` 求三项计数。套 §4 固定函数。
4. 输出按 `query_turn_id` 排序写 `private/turn_evidence.jsonl`；报告计数由同一批记录聚合。

输入前提违约（fail-closed，`TurnEvidenceInputError`）：QueryTurn 引用的 UserBlock/AgentStep 不存在；OBSERVED 回合 UserBlock 内事件缺注解或注解 `user_block_id` 不回指该 block；注解的 capture 与回合不一致；capture 缺 M1B capture/quality 行。

---

## 6. 验收向量（§0 探针按 §4 函数预演的结果；实现后必须逐项相等，差异即 fold 或规格有误）

```text
query_turn_count 2,610
task_profile      OBSERVED_ROOTED: ELIGIBLE 548 / PARTIAL 337 / INELIGIBLE 42
                  PREFIX_ROOTED:   ELIGIBLE 0   / PARTIAL 1,680 / INELIGIBLE 3
task reasons      INTENT_PREFIX_ONLY 1,680；OUTCOME_NOT_OBSERVED 1,377；NO_INTENT_EVIDENCE 45
environment       OBSERVED_ROOTED: ELIGIBLE 230 / PARTIAL 675 / INELIGIBLE 22
                  PREFIX_ROOTED:   ELIGIBLE 416 / PARTIAL 1,267 / INELIGIBLE 0
env reasons       TOOL_RESULT_NOT_OBSERVED 1,307；TOOL_SCHEMA_UNRELIABLE 1,124；
                  UNLOCALIZED_COMPACTION_EVIDENCE 163；SOURCE_REPORTS_INPUT_TRUNCATED 46；NO_AGENT_STEP 22
intent evidence   intent_evidence_event_count_observed 892；intent_evidence_event_count_prefix_only 10,802
captures          有 ELIGIBLE task 回合 193；有任何意图证据 1,680
```

跨模块不变量（validator 正交层直接断言）：`intent_evidence_event_count_observed` = 投影报告 `observed_plain_user_text_count`；`intent_evidence_event_count_prefix_only` = 投影报告 `prefix_unlocalized_plain_user_text_count`；`captures_with_any_intent_evidence_count` = 投影报告 `captures_with_plain_user_text`；`query_turn_count` = M1D 报告 `query_turn_count`；每个 `PREFIX_ROOTED` 回合的前缀证据数 = 该 capture 在投影中的前缀 `PLAIN_USER_TEXT` 数。

---

## 7. 独立 validator（两层信任边界，与 M1D §8 / 投影 §3 同构）

- **物理层**：复用 `trajectory/run_validation.py` 公共原语（manifest 自洽、SHA、inventory、canonical JSON、闭合值域/隐私扫描、回执、Git 来源）。
- **篡改检测层**：三个上游 validator 重跑 → 同一纯 fold 复算 → 与落盘记录按 `query_turn_id` 双射逐字节比对（`TURN_EVIDENCE_ROW_*`）。
- **正交不变量层**（不经 fold）：覆盖（行集 = M1D QueryTurn 集）；每行 `task_profile`/`environment_profile` 用 §4 固定函数从**行内自有计数与 capture 事实**重算相等（`ELIGIBILITY_INCONSISTENT`）；OBSERVED 行的证据 ID ⊆ 其 UserBlock 事件且注解类为 `PLAIN_USER_TEXT`；PREFIX_ONLY 行 `harness/control/other` 三计数恒 0、`has_non_text_blocks=false`；§6 跨模块守恒。
- **能核验 / 不能核验**：全部字段均为结构派生，可机械核验到位；① 没有语义结论，不设人工评审门。

---

## 8. 输出布局

```text
<run_id>/
  turn_evidence_manifest.json   # 三方上游 run ID + manifest SHA、契约版本 turn-evidence-v1
  private/turn_evidence.jsonl   # 每 QueryTurn 一行，按 query_turn_id 排序
  reports/turn_evidence_report.json
  artifact_manifest.json
  run_receipt.json
```

CLI：`traceforge turn-evidence build --m1b-run … --m1d-run … --projection-run … --output …`；脚本 `scripts/validate_turn_evidence_run.py <run> <m1b> <m1d> <projection>`。包 `src/traceforge/turn_evidence/`，沿用 `contracts/view/reader/builder/pipeline/validation` 六件式。

---

## 9. 完成条件

- 测试先行覆盖：每个原因码至少一例；42 个无意图观测回合的两类（Harness-only、Control-only）；0 AgentStep 回合；PREFIX_ROOTED 前缀证据排序与守恒；输入违约各一例 fail-closed；validator 每个错误码一例；双跑逐字节一致。
- 干净克隆双跑复现同 run ID；validator 四参 `ok=true`；§6 向量逐项相等。
- 验收报告 `r01-turn-evidence-validation.md`；交接 §8/§9/§11 推进。

---

## 10. 缓做 / 边界

- 工具名、canonical tool role、观测 empty/error/truncated 分类：② `EnvironmentExposureProfile`（工具结果 payload 无错误标量，需在 ② 规格里决定是否读内容形态或声明不可观测）。
- 根 query 选择、回合间语义关系、task family：③。
- 跨 capture（M1C 同组）合并与去重分母：④。
- 重建准入（plan §7）：M2/M3 边界。

## 11. 待定小决策（评审时拍板）

| 编号 | 问题 | 建议 |
|---|---|---|
| E-a | PREFIX_ROOTED 回合是否也记 capture 前缀内 Harness/控制信号计数？ | **否**。前缀不细分到回合（M1D 门②）；全局计数投影报告已有。 |
| E-b | `intent_evidence_utf8_byte_length` 是否必要？ | **是**，③ 的调用预算与截断策略需要；一个求和，无额外读取。 |
| E-c | 原因码是否细分 `tool_schema_status` 三值？ | **否**（§4 理由）。 |
| E-d | 是否为 ① 铸新 ID？ | **否**，主键沿用 `query_turn_id`。 |
