# M2 ① `TurnEvidence` 处理规格（v0.2 草案，待评审冻结）

状态：草案 v0.2。v0.1 → v0.2 采纳"前缀消息分段"（2026-09-03 决定，见 [`session-handoff.md`](session-handoff.md) §11）：单元不再只是 M1D `QueryTurn`，而是 `QueryTurn` ∪ 前缀分段，使 80% 只在前缀里有意图的 capture 得到确定性的回合级结构。依 [`../AGENTS.md`](../AGENTS.md) 规格先行，评审冻结前不写代码。① 是 M2 ②③④ 的唯一输入，本身确定性、零模型。

目标一句话：把已验收的 M1B + M1D + `UserTextProjection` 三个 run **按意图单元逐条 join** 成一张闭合值域的"证据包"表——观测窗口内的单元直接沿用 M1D `QueryTurn`，不可定位前缀内的单元按 M1D 同一 UserBlock 规则在消息顺序上分段——并按用途给出 eligibility（[`overall-plan.md`](overall-plan.md) §5）。后续模型层（③）只看证据包、不看原始 run；聚合层（②④）只做计数。不读正文、不做任何语义判断、不伪造 request 边界。

---

## 0. 四条实测地基（只读探针，冻结 run `6be45e01…`/`84d826b3…`/`47cfac20…`；数字为人工 sanity 观测，不进代码）

1. **观测窗口回合 927 个，其中 42 个没有任何用户意图证据。** UserBlock 按 `text_class` 组成：638 只有 1 条 `PLAIN_USER_TEXT`，238 是 `HARNESS_CONTEXT`+`PLAIN_USER_TEXT`（Harness 注入紧贴用户文本），885 含 ≥1 条 `PLAIN_USER_TEXT`；32 个只有 Harness 注入、10 个只有控制信号（`turn_aborted` 13 次、`subagent_notification` 2、`user_interjection` 2）。这 42 个结构上存在但必须显式 INELIGIBLE，不能当 query。
2. **前缀是 80% capture 的唯一意图来源，且内部有完整的消息顺序结构。** 1,680 个 capture 的前缀里共 10,802 条 `PLAIN_USER_TEXT`（16.2 MB），前缀还含 34,292 条 assistant、38,688 条 tool_call、35,664 条 tool_result（各占全体的 70% 左右）。前缀缺的是 **request 边界**（M1B §183：不能为更早的可见 assistant 伪造 request ID），不是消息顺序——EventLog 的 `sequence_number` 是 M1B 冻结事实。把 M1D 的 UserBlock 规则（USER 连续段 = 一个 block；block 后直到下一 block 的非 USER 事件归该 block）原样施加在前缀事件上，得到 8,341 个前缀 block（每 capture ≥1；其中每 capture 最后一个是观测首回合的结构根），**不需要任何 request 边界**。
3. **前缀最后一个 block 与 M1D 的 `PREFIX_ROOTED` 回合是同一次交互的两半。** 观测窗口从首个观测 terminal assistant 开始，它响应的用户消息必然在前缀里，且按消息顺序就是前缀最后一个 block。因此 ① 把该 block 并入 `PREFIX_ROOTED` 回合作为其根（1,683 个；21 个的根 block 只有 Harness 注入、无用户文本——与观测窗口的 32 个 Harness-only 回合同构，显式 INELIGIBLE）。前缀其余 6,658 个 block 各成一个 `PREFIX_SEGMENT` 单元。
4. **终态缺失是系统性的。** 1,211/1,683 capture 以 `TOOL_CALL_PENDING` 结束；885 个有意图的观测回合里 337 个 INCOMPLETE；前缀分段里 2,434/6,658 末尾不是有正文的 assistant。原因是回流按请求快照。故 eligibility 必须**按用途分开**：outcome 缺失只降 task_profile，工具结果未观测只降 environment_profile。

此外：`ActionBatchV2.execution_semantics` 在 R01 恒 `UNKNOWN`（无信息量，不透传）；`CaptureQualityV3.privacy_status` 恒 `DERIVATION_POLICY_APPLIED`（无变异，不设隐私维度——契约不保留无生产者状态）；工具名是 M1B 公开 payload 标量但不是闭合值域，① 只存事件 ID，工具名由 ② 经 ID 解引用；SYSTEM 事件（前缀 5,877 条）不切分 block、不属任何单元的证据，只在守恒记账中出现。

---

## 1. 输入契约（只读，先校验后消费）

三个已发布 run，各自先跑权威 validator（M1B `validate_compiled_run`、M1D `validate_query_turn_run`、投影 `validate_user_text_projection_run`），且 M1D 与投影 manifest 绑定的 M1B run ID/manifest SHA 必须与所给 M1B run 一致；投影 run **必须**绑定所给 M1D run（① 依赖 `user_block_id` 回指）。任何一项失败 → fail-closed。

| 来源 | 消费字段 | 用途 |
|---|---|---|
| M1D `QueryTurnV1` | `query_turn_id`、`capture_occurrence_id`、`turn_ordinal`、`root_status`、`user_block_id`、`agent_step_ids`、`turn_status` | 观测单元全集；终态 |
| M1D `UserBlockV1` | `user_block_id`、`event_ids` | 观测回合的 USER 事件集合 |
| M1D `AgentStepV1` | `agent_step_id`、`tool_observation_event_ids`、`unresolved_tool_call_event_ids` | 观测单元的调用/观测/未解析计数 |
| M1D `CaptureTurnAccountingV1` | `capture_occurrence_id`、`prefix_event_counts_by_kind` | **只供 validator** 做前缀守恒（§7），builder 不读 |
| 投影 `UserTextAnnotationV1` | `event_occurrence_id`、`capture_occurrence_id`、`locality`、`user_block_id`、`text_class`、`content_form`、`utf8_byte_length` | 意图证据筛选（仅 `PLAIN_USER_TEXT`）、Harness/控制信号/其他计数、附件位、证据字节数 |
| M1B `EventOccurrenceV3` | 前缀事件（`event_scope=PRE_FIRST_OBSERVED_TERMINAL`）的 `event_occurrence_id`、`capture_occurrence_id`、`sequence_number`、`event_kind`；assistant 事件经 `event_payload.py` typed reader 取 `TextContent.utf8_byte_length` / `ContentBlocks.block_count`（与 M1D 同一冻结标量） | 前缀分段；分段内计数；分段末尾"有正文 assistant"判定 |
| M1B `ToolPairingRecordV3` | `matched_call_event_id`、`matched_result_event_id` | 前缀分段内未解析调用计数（按配对，不按位置） |
| M1B `NormalizedCaptureV3` | `capture_occurrence_id`、`input_truncation_status`、`has_compaction` | capture 级原因码 |
| M1B `CaptureQualityV3` | `capture_occurrence_id`、`tool_schema_status` | capture 级原因码 |

**一律不读**：任何正文、`reasoning_content`、工具名、参数、URL、路径、`domain_meta`。M1C 不消费（跨 capture 留 ④）。

---

## 2. 输出契约（不可变、闭合值域、逐字节可复现）

**`EvidenceUnitV1`**（一张表，三种 `unit_kind`；主键 `unit_id`）：

| 字段 | 值域 | 来源/派生 |
|---|---|---|
| `schema_version` | 常量 | |
| `unit_id` | 64-hex | `sha256(contract, unit_kind, 锚 ID)`；锚 = `query_turn_id`（两种回合）或分段首个 USER 事件 ID（分段）。v0.1 不铸 ID，v0.2 因出现新实体（分段）而必须铸，且三种单元共用一个命名空间以便 ④ 统一引用 |
| `unit_kind` | `OBSERVED_ROOTED_TURN` \| `PREFIX_ROOTED_TURN` \| `PREFIX_SEGMENT` | |
| `capture_occurrence_id` | 透传 | |
| `unit_ordinal` | 计数 | capture 内线性序：前缀分段按消息顺序 0…k−1，`PREFIX_ROOTED_TURN` 为 k，观测回合按 `turn_ordinal` 续排。④ 的结构邻接边由此派生 |
| `query_turn_id` | ID 或 null | 分段为 null |
| `intent_locality` | `OBSERVED` \| `PREFIX_ONLY` | `unit_kind` 的固定函数 |
| `intent_evidence_event_ids` | 有序事件 ID 元组 | 单元 USER 事件中 `text_class=PLAIN_USER_TEXT` 者，按 `sequence_number` 升序。观测回合取 UserBlock 事件；`PREFIX_ROOTED_TURN` 取其根 block；分段取自身 block |
| `intent_evidence_utf8_byte_length` | 计数 | 上述事件 `utf8_byte_length` 之和（③ 的预算依据） |
| `harness_event_count`、`control_signal_event_count`、`other_user_event_count` | 计数 | 单元 USER 事件中 `HARNESS_*` / `CONTROL_SIGNAL` / 其余（`EMPTY_TEXT`、`UNKNOWN_TAGGED`、`NO_LEADING_TEXT`）数 |
| `has_non_text_blocks` | bool | 单元任一 USER 事件 `content_form=CONTENT_BLOCKS`（投影字段；观测回合与 M1D `UserBlockV1.has_non_text_blocks` 由 validator 交叉核对） |
| `agent_activity_count` | 计数 | 观测回合 = `agent_step_count`；分段 = 分段内 `ASSISTANT_MESSAGE` 数；`PREFIX_ROOTED_TURN` = 两者之和 |
| `tool_call_count`、`tool_observation_count`、`unresolved_tool_call_count` | 计数 | 观测部分对 `agent_step_ids` 求和；前缀部分按分段内 `TOOL_CALL`/`TOOL_RESULT` 事件计数，未解析 = 分段内无配对结果的调用 |
| `outcome_observed` | bool | 回合：`turn_status=COMPLETE`；分段：分段最后一个非 USER 事件是有正文的 `ASSISTANT_MESSAGE`（与 M1D `TEXT_OUTCOME` 同判据：`TextContent.utf8_byte_length>0` 或 `ContentBlocks.block_count>0`，且其后无 TOOL_CALL） |
| `task_profile`、`task_profile_reasons` | `ELIGIBLE` \| `PARTIAL` \| `INELIGIBLE`；原因码有序元组 | §4 固定函数 |
| `environment_profile`、`environment_profile_reasons` | 同上 | §4 固定函数 |

原因码闭合全集（每个都必须有生产者，测试向量逐个覆盖）：`NO_INTENT_EVIDENCE`、`INTENT_PREFIX_ONLY`、`OUTCOME_NOT_OBSERVED`、`NO_AGENT_ACTIVITY`、`TOOL_RESULT_NOT_OBSERVED`、`TOOL_SCHEMA_UNRELIABLE`、`SOURCE_REPORTS_INPUT_TRUNCATED`、`UNLOCALIZED_COMPACTION_EVIDENCE`。

**不产出** `reconstruction` 用途：在 ① 可用的结构事实下它恒等于 `task_profile=ELIGIBLE`（COMPLETE 蕴含 ≥1 agent activity），无独立信息量；plan §7.1 的重建准入需要语义判断，留 M2/M3 边界。**不产出** 前缀 AgentStep/ActionBatch 对象：那是 M1D 契约，① 只出计数。

**报告**（`reports/turn_evidence_report.json`，只有计数，键闭合）：`unit_count`；`{unit_kind}` 3 键；`{unit_kind}×{task_profile}` 9 键；`{unit_kind}×{environment_profile}` 9 键；8 个原因码各一计数；`intent_evidence_event_count_observed`、`intent_evidence_event_count_prefix_only`；`prefix_head_event_count`（首个前缀 block 之前的非 USER 事件数）；`captures_with_eligible_task_unit_count`、`captures_with_any_intent_evidence_count`。

---

## 3. 硬门

- **门① 纯确定性、零模型、不读正文。** 全部字段只依 §1 标量；同输入逐字节一致。
- **门② 前缀分段只镜像 M1D 的 UserBlock 规则，只用消息顺序，不伪造 request 边界。** 分段不带 `request_boundary_id`，`intent_locality=PREFIX_ONLY` 永远显式；任何下游不得把分段报告为 request 级回合。
- **门③ 意图证据划分守恒。** 每条 `PLAIN_USER_TEXT` USER 事件恰属于一个单元的 `intent_evidence_event_ids`；每条前缀事件恰属于一个分段、`PREFIX_ROOTED_TURN` 的根 block，或前缀头部（首 block 之前，只记账）。
- **门④ 闭合值域。** 私有表只有 ID/序数/枚举/bool/计数；工具名、标签名、正文一律不落盘。
- **门⑤ 按用途分离的 eligibility，每个非 ELIGIBLE 都有原因码。** 无全局 `valid`；原因码全集中不得有 fold 永不产出的码。
- **门⑥ 先校验后消费 + 身份绑定。** 三个上游 validator 全过且身份链一致才消费；manifest 记录三方 run ID 与 manifest SHA。

---

## 4. 固定函数（`contracts.py` 单一定义，builder 与 validator 共用）

```text
prefix_blocks(前缀事件按 sequence_number 升序):
  遇 USER 且前一事件不是 USER → 开新 block；连续 USER 并入当前 block
  非 USER 事件归当前 block（若尚无 block → 前缀头部，只记账）
  # 与 M1D builder 步 3 的 UserBlock 规则相同：SYSTEM/TOOL_* 不切分但会终止 USER 连续段

intent_locality(unit_kind) = OBSERVED if OBSERVED_ROOTED_TURN else PREFIX_ONLY

task_profile(intent_evidence_count, intent_locality, outcome_observed):
  count == 0                          → INELIGIBLE, [NO_INTENT_EVIDENCE]
  else reasons = [INTENT_PREFIX_ONLY if PREFIX_ONLY] + [OUTCOME_NOT_OBSERVED if not outcome_observed]
       → ELIGIBLE if reasons 为空 else PARTIAL

environment_profile(agent_activity_count, unresolved_tool_call_count,
                    tool_schema_status, input_truncation_status, has_compaction):
  agent_activity_count == 0           → INELIGIBLE, [NO_AGENT_ACTIVITY]
  else reasons = [TOOL_RESULT_NOT_OBSERVED if unresolved > 0]
               + [TOOL_SCHEMA_UNRELIABLE if tool_schema_status != CONSISTENT]
               + [SOURCE_REPORTS_INPUT_TRUNCATED if input_truncation_status == SOURCE_REPORTS_TRUNCATED]
               + [UNLOCALIZED_COMPACTION_EVIDENCE if has_compaction]
       → ELIGIBLE if reasons 为空 else PARTIAL
```

原因码顺序即上文列出顺序。`tool_schema_status` 的三个非 `CONSISTENT` 值合并为一个原因码：plan §5 的处理只有一种（只记录冲突，不生成硬工具契约），细分对 ②③④ 无消费者；② 需要细分时直接读 M1B。

---

## 5. 处理逻辑（纯函数 fold，逐 capture，无 IO）

1. 读三个 run 的 §1 视图；按 `capture_occurrence_id` 分组；建 `event_id → annotation` 索引与"有配对结果的调用事件 ID 集"。
2. 对每个 capture 的前缀事件套 `prefix_blocks`；对每个 block 累计：USER 三类计数与 `PLAIN_USER_TEXT` 事件序列、`ASSISTANT_MESSAGE`/`TOOL_CALL`/`TOOL_RESULT` 计数、未解析调用数、最后一个非 USER 事件的种类与"有正文"位。
3. 最后一个 block 并入该 capture 的 `PREFIX_ROOTED` 回合（M1D 恒有且仅有一个）；其余 block 各成 `PREFIX_SEGMENT`。
4. 对每个观测回合：UserBlock 事件逐条查注解并计数；对 `agent_step_ids` 求三项计数。
5. 套 §4 固定函数；按 `(capture_occurrence_id, unit_ordinal)` 排序写 `private/evidence_units.jsonl`；报告由同一批记录聚合。

输入前提违约（fail-closed，`TurnEvidenceInputError`）：QueryTurn 引用的 UserBlock/AgentStep 不存在；某 capture 的 `PREFIX_ROOTED` 回合数 ≠ 1；观测回合 UserBlock 内事件缺注解或注解 `user_block_id` 不回指该 block；前缀 USER 事件缺注解；注解 capture 与单元不一致；capture 缺 M1B capture/quality 行；前缀事件缺 `sequence_number`。

---

## 6. 验收向量（§0 探针按 §4 函数预演；实现后必须逐项相等，差异即 fold 或规格有误）

```text
unit_count 9,268 = OBSERVED_ROOTED_TURN 927 + PREFIX_ROOTED_TURN 1,683 + PREFIX_SEGMENT 6,658
intent evidence   observed 892；prefix_only 10,802（和 = 投影 PLAIN_USER_TEXT 总数 11,694）
task_profile      OBSERVED_ROOTED_TURN: ELIGIBLE 548 / PARTIAL 337   / INELIGIBLE 42
                  PREFIX_ROOTED_TURN:   ELIGIBLE 0   / PARTIAL 1,662 / INELIGIBLE 21
                  PREFIX_SEGMENT:       ELIGIBLE 0   / PARTIAL 6,323 / INELIGIBLE 335
task reasons      INTENT_PREFIX_ONLY 7,985；OUTCOME_NOT_OBSERVED 3,486；NO_INTENT_EVIDENCE 398
environment       OBSERVED_ROOTED_TURN: ELIGIBLE 230   / PARTIAL 675   / INELIGIBLE 22
                  PREFIX_ROOTED_TURN:   ELIGIBLE 413   / PARTIAL 1,270 / INELIGIBLE 0
                  PREFIX_SEGMENT:       ELIGIBLE 3,320 / PARTIAL 2,800 / INELIGIBLE 538
env reasons       TOOL_RESULT_NOT_OBSERVED 1,854；TOOL_SCHEMA_UNRELIABLE 3,211；UNLOCALIZED_COMPACTION_EVIDENCE 489；
                  SOURCE_REPORTS_INPUT_TRUNCATED 476；NO_AGENT_ACTIVITY 560
prefix head       非 USER 事件 3,850（SYSTEM 3,665 / ASSISTANT 65 / TOOL_CALL 60 / TOOL_RESULT 60）
captures          有 ELIGIBLE task 单元 193；有任何意图证据 1,680
```

跨模块守恒（validator 正交层直接断言）：
- `intent_evidence_event_count_observed` = 投影报告 `observed_plain_user_text_count`；`intent_evidence_event_count_prefix_only` = `prefix_unlocalized_plain_user_text_count`；`captures_with_any_intent_evidence_count` = `captures_with_plain_user_text`；
- 观测回合行数 + `PREFIX_ROOTED_TURN` 行数 = M1D 报告 `query_turn_count`，且与 M1D QueryTurn 集一一对应；
- 逐 capture：前缀头部 + 所有前缀单元的 USER/ASSISTANT/TOOL_CALL/TOOL_RESULT 计数之和 = M1D `CaptureTurnAccountingV1.prefix_event_counts_by_kind`（探针已验证全量相等：USER 13,225、ASSISTANT 34,292、TOOL_CALL 38,688、TOOL_RESULT 35,664）。

---

## 7. 独立 validator（两层信任边界，与 M1D §8 / 投影 §3 同构）

- **物理层**：复用 `trajectory/run_validation.py` 公共原语（manifest 自洽、SHA、inventory、canonical JSON、闭合值域/隐私扫描、回执、Git 来源）。
- **篡改检测层**：三个上游 validator 重跑 → 同一纯 fold 复算 → 与落盘记录按 `unit_id` 双射逐字节比对（`EVIDENCE_UNIT_ROW_*`）。
- **正交不变量层**（不经 fold）：§6 三组跨模块守恒；每行 eligibility 用 §4 固定函数从**行内自有计数与 capture 事实**重算相等（`ELIGIBILITY_INCONSISTENT`）；观测回合行的证据 ID ⊆ 其 UserBlock 事件且注解类为 `PLAIN_USER_TEXT`，`has_non_text_blocks` 与 M1D 一致；分段行 `query_turn_id=null`、`intent_locality=PREFIX_ONLY`；每 capture `unit_ordinal` 从 0 连续；证据 ID 全表无重复（门③）。
- **能核验 / 不能核验**：全部字段均为结构派生，可机械核验到位；① 没有语义结论，不设人工评审门。

---

## 8. 输出布局

```text
<run_id>/
  turn_evidence_manifest.json   # 三方上游 run ID + manifest SHA、契约版本 turn-evidence-v1
  private/evidence_units.jsonl  # 每单元一行，按 (capture_occurrence_id, unit_ordinal) 排序
  reports/turn_evidence_report.json
  artifact_manifest.json
  run_receipt.json
```

CLI：`traceforge turn-evidence build --m1b-run … --m1d-run … --projection-run … --output …`；脚本 `scripts/validate_turn_evidence_run.py <run> <m1b> <m1d> <projection>`。包 `src/traceforge/turn_evidence/`，沿用 `contracts/view/reader/builder/pipeline/validation` 六件式。

---

## 9. 完成条件

- 测试先行覆盖：每个原因码至少一例；三种 `unit_kind`；Harness-only 与 Control-only 的观测回合和前缀根 block；SYSTEM 事件终止 USER 连续段（产生 0 activity 分段）；前缀头部记账；根 block 并入 `PREFIX_ROOTED_TURN` 的计数合并；意图证据划分守恒与 M1D 前缀守恒；输入违约各一例 fail-closed；validator 每个错误码一例；双跑逐字节一致。
- 干净克隆双跑复现同 run ID；validator 四参 `ok=true`；§6 向量逐项相等。
- 验收报告 `r01-turn-evidence-validation.md`；交接 §8/§9/§11 推进。

---

## 10. 缓做 / 边界

- 工具名、canonical tool role、观测 empty/error/truncated 分类：② `EnvironmentExposureProfile`，输入为 M1B **全 scope** 事件 + 配对 + 工具目录（不经 ①）；工具结果 payload 无错误标量，② 规格须决定读内容形态还是声明不可观测。
- 根 query 的语义确认、单元间语义关系、task family：③。
- 跨 capture（M1C 同组）合并与去重分母：④。同一 thread 后继 capture 的前缀含前驱内容，分段会跨 capture 重复，去重是 ④ 的职责，① 不做。
- 重建准入（plan §7）：M2/M3 边界。

## 11. 待定小决策（评审时拍板）

| 编号 | 问题 | 建议 |
|---|---|---|
| E-a | 分段是否带 `request_boundary_id`？ | **否**。前缀无可定位边界，带上就是伪造（门②）。 |
| E-b | `intent_evidence_utf8_byte_length` 是否必要？ | **是**，③ 的调用预算与截断策略需要；一个求和，无额外读取。 |
| E-c | 原因码是否细分 `tool_schema_status` 三值？ | **否**（§4 理由）。 |
| E-d | 是否为单元铸新 ID？ | **是**（v0.2 改变）：出现新实体，三种单元共用命名空间。`query_turn_id` 作外键保留。 |
| E-e | SYSTEM 事件是否切分 block？ | **不切分但终止 USER 连续段**——与 M1D builder 完全一致，避免两套规则。由此产生的 0 activity 分段（538）诚实标 `NO_AGENT_ACTIVITY`。 |
| E-f | 是否为前缀产出 AgentStep 对象？ | **否**。那是 M1D 契约；① 只出计数。若 ② 需要前缀内逐步结构，另议 M1D v0.4，不在 ① 里偷做。 |
