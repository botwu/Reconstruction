# M1D 结构型 QueryTurn 处理规格（v0.3，实现同步稿）

版本：v0.3（与 `src/traceforge/query_turns/` 实现同步；待 R01 正式验收）。v0.1 的"QueryTurn↔RequestBoundary 1:1""boundary=单次推理"两条地基已被冻结 R01 run 实测**证伪**，v0.2 据实测重写输入/输出/处理逻辑。

> **v0.2 自审勘误（2026-09-02）**：开工前对抗自审揪出 outcome 判据 2 处真错，已改正——(1) 原用 `visible_payload_utf8_byte_length>0` 判"有无文本"是错的（§199：它含 JSON 外壳、恒 >0，不是文本长度）；(2) 原假设可逐回合拿 TEXT/EMPTY oracle，但 M1B 只对 capture 末条给权威 `terminal_status`。改正见 §1 表、§3 门③、§4 步 4：content 空/非空一律经 `event_payload.py` typed reader 的冻结标量（`TextContent.utf8_byte_length` / `ContentBlocks.block_count`）判，镜像 M1B `_terminal_status` 的 tool_calls 优先约定；capture 末条 assistant 交叉核对 `CaptureQualityV3.terminal_status`，中间 turn 独立派生 + validator 复算。

> **v0.3 实现同步修订（2026-09-03，落实 [`m1d-review-20260902.md`](m1d-review-20260902.md) D1–D5 与 §3 决定）**：(1) **§8 validator 信任边界重述**——篡改检测层调用与 pipeline 同一个纯 fold 重建期望（不复刻算法：复制品对 fold 自身缺陷零检出、只倍增维护面），另设**不经 fold**的正交不变量层（观测事件分区守恒、记账逐 kind 对账、末 assistant 与 M1B oracle 交叉核对、报告计数由已发布表重算）检出 fold 缺陷（D1）；(2) `parallel_semantics` 无 ActionBatch 时为 `null`，不臆造 `UNKNOWN`（D2）；(3) `CaptureTurnAccountingV1` 字段以实现为准，`observed_user_event_count` 由 `observed_event_counts_by_kind["USER"]` 派生、不物化（D3）；(4) 删除 `processing_status` 透传与 `QUARANTINED` 分支——M1D 输入全集是 M1B 已编译 capture，M1B validator 已断言其恒 `COMPLETE`，quarantined 记录无 capture 行、无事件，结构上不进入 M1D；契约不保留无生产者状态（D4/D5，`PARTIAL` 已随 M1B v4 删除）；(5) 终态交叉核对对象改写为"观测窗口内序号最大的 `ASSISTANT_MESSAGE`"，与 M1B `_terminal_status(messages[-1])` 一致（§3 决定一）；配对索引重复不设 tripwire，因 M1B validator 已断言严格 1:1 且 M1D 先校验后消费（§3 决定二）；空观测流以防御性测试固定"0 turn / 0 edge / 1 accounting"（§3 决定三）；(6) 上游名称随 M1B v4：`EventOccurrenceV3`/`NormalizedCaptureV3`/`CaptureQualityV3`，`visible_payload_envelope_utf8_byte_length`。

状态：**已实现，待验收**。本稿只定义 I/O 与确定性算法；不在 M1A/B/C 预埋 M1D 结构（[`../AGENTS.md`](../AGENTS.md) §1 YAGNI）。

阅读前置：[`../AGENTS.md`](../AGENTS.md)、[`background-and-goals.md`](background-and-goals.md) §2/§5/§9、[`overall-plan.md`](overall-plan.md) §4.4–4.6/§5、[`r01-processing-spec.md`](r01-processing-spec.md) §5.1/§8.2/§183、[`m1c-processing-spec.md`](m1c-processing-spec.md)。

---

## 0. 三条实测地基（设计据此，不是可选背景）

对冻结 M1B run（1683 capture / 9561 boundary / 175858 event，全 `processing_status=COMPLETE`）做纯结构探针，得到三条决定 M1D 定位的事实（数字为**人工 sanity 观测**，不写进代码，[`r01-processing-spec.md`](r01-processing-spec.md) §4.5）：

1. **boundary 是"两次相邻观测请求之间的消息段"，不是"一次推理"。** `_build_boundaries`（`compiler.py`）令 boundary 0 只 own 终止 assistant 那一条、boundary k≥1 own `range(depth[k-1], depth[k])`。实测 89% 窗口含 1 个 assistant，但 11%（1060 个）含 ≥2 个（最多 73 个）——因 `terminal_prefix_depths` 是源实际观测到的请求终止点，密度不均，相邻两终止间可夹未被单独观测的 assistant 推理步。**故 boundary 不能作为回合切分单元。**

2. **92% 的用户 query 落在不可定位的 `PRE_FIRST_OBSERVED_TERMINAL` 前缀里。** 全体 14407 个 USER 事件，13225 在前缀，仅 1182 在可观测窗口；`PRE_FIRST` 前缀还吞掉 70% 的 assistant、71% 的 tool_result。**1341/1683（80%）capture 的观测窗口里 0 个可定位 USER；885/1683（53%）capture 只有 1 个 boundary（观测内容=孤零零一条终止 assistant）。** 只有 342 capture 能产出哪怕一个"有根"回合。前缀是 M1B 明确标注不可定位的重建历史（§183），M1D **绝不**在其内部编造回合或分类。

3. **AgentStep 是唯一始终可定位的可靠单元。** 工具反馈以 `role="tool"`→`TOOL_RESULT` 承载（50140 个），未混入 USER；每个观测 assistant 事件都带确定的 `ActionBatch` 与 `ToolPairingRecordV3` 链接。**M1D 以 AgentStep 为核心，QueryTurn 是其上一层"带根可定位状态"的诚实分组。**

**由此定位（对应 [`r01-processing-spec.md`](r01-processing-spec.md) §8.2）：** M1D 确定性地把**可观测事件流**折叠成 `AgentStep`／`UserBlock`／`AssistantOutcome`／`QueryTurn`／结构性 `ThreadTurnGraph`，并对"前缀不可定位""孤儿观测"做显式记账。前缀内容不细分、不成回合。无任何模型调用。语义关系与 `TaskEpisode` 全部留 M2（§11）。

---

## 1. 输入契约（只读，先校验后消费）

主输入=一个已发布 M1B run。先调 M1B 权威 `validate_compiled_run` 完成"先校验再消费"（同 M1C 信任边界），再抽取以下**已发布公开标量**（契约见 [`../src/traceforge/trajectory/contracts.py`]）。**M1B 私有派生公式不 import，其 ID 当不透明外键。**

| 来源契约 | 消费字段 | M1D 用途 |
|---|---|---|
| `EventOccurrenceV3` | `event_occurrence_id`、`capture_occurrence_id`、`sequence_number`、`event_kind`、`event_scope`、`request_boundary_id`(可空)、`message_index`、`sub_index` | 有序事件流；AgentStep/UserBlock 归属 |
| `EventOccurrenceV3.payload`（经 `event_payload.py` typed reader，仅取**冻结标量**） | assistant/user content 的 `TextContent.utf8_byte_length`、`ContentBlocks.block_count`、`kind` | content 空/非空分类（outcome）＋ `has_non_text_blocks` 附件标记（**不读文本值/块内容**） |

> **注（v0.2 勘误，v0.3 随 M1B v4 改名）**：`EventOccurrenceV3.visible_payload_envelope_utf8_byte_length` **不能**当"有无文本"判据——§199 明确它是可见 payload 经 canonical JSON 编码后的外壳字节数（含 `{"content":…}` 外壳，恒 >0），不是 content 文本长度。content 空/非空一律经 typed reader 的 `TextContent.utf8_byte_length` / `ContentBlocks.block_count` 判。
| `RequestBoundaryV1` | **不直接读取**；boundary 仅经 `EventOccurrenceV3.request_boundary_id` 作证据锚点 | `UserBlock/AgentStep.request_boundary_id`、`QueryTurn.boundary_ids_spanned` |
| `NormalizedCaptureV3` | `capture_occurrence_id`、`has_compaction` | capture 全集（=M1B 已编译 capture）；compaction 记账 |
| `ActionBatchV2` | `action_batch_id`、`capture_occurrence_id`、`assistant_event_id`、`tool_call_event_ids`、`execution_semantics` | AgentStep 的 ActionBatch 与并行语义 |
| `ToolPairingRecordV3` | `matched_call_event_id`、`matched_result_event_id` | ToolObservation 归属（**按配对而非位置**）；未解析调用=中断判据 |
| `CaptureQualityV3` | `capture_occurrence_id`(可空)、`terminal_status` | capture 末条 assistant 的独立 oracle（validator 交叉核对）。`processing_status` **不消费**：M1B validator 已断言已编译 capture 恒 `COMPLETE`；quarantined 行 `capture_occurrence_id=null`、无 capture 行与事件，join 时天然不在 M1D 全集内 |

**一律不读**：任何消息正文、`reasoning_content` 原文（M1B 已归约为 `sha256+byte+存在性`）、raw content block 内容、绝对路径/URL、domain_meta/rubric（属 M2 `SourceAnnotationProjection`）。

**可选次输入（缓做，见 §11 D-c）**：M1C lineage run，用于跨 capture `ThreadTurnGraph`。v0.2 **默认不启用**——R01 的线程价值大多在不可定位前缀里，跨 capture 拓扑更适合 M2；先只做 capture 内结构边（YAGNI）。

---

## 2. 输出契约（全部不可变、稳定 ID 引用、闭合值域）

写盘均经 `canonical_json_line`；所有集合按稳定 ID 排序，保证逐字节可复现。

- **`UserBlockV1`**：`schema_version`、`user_block_id`、`capture_occurrence_id`、`request_boundary_id`、`event_ids`（有序 USER 事件 ID）、`message_index_start`、`message_index_end`、`has_non_text_blocks`(bool)。
- **`AgentStepV1`**：`schema_version`、`agent_step_id`、`capture_occurrence_id`、`request_boundary_id`、`assistant_event_id`、`action_batch_id`(可空)、`tool_observation_event_ids`（有序，**经配对**的观测结果）、`unresolved_tool_call_event_ids`（有序，发出但无观测结果的调用）、`parallel_semantics`（透传 `ActionBatchV2.execution_semantics`；**无 ActionBatch 时 `null`**——无 batch 则无并行语义，不臆造 `UNKNOWN`，D2）。
- **`AssistantOutcomeV1`**：`schema_version`、`assistant_outcome_id`、`capture_occurrence_id`、`terminal_assistant_event_id`、`outcome_kind`（**结构派生**：`TEXT_OUTCOME`/`EMPTY_OUTCOME`；见 §4 步 4）。
- **`QueryTurnV1`**：`schema_version`、`query_turn_id`、`capture_occurrence_id`、`turn_ordinal`（capture 内序）、`root_status`（`PREFIX_ROOTED`｜`OBSERVED_ROOTED`）、`user_block_id`(可空；PREFIX_ROOTED 恒空)、`agent_step_ids`（有序）、`assistant_outcome_id`(可空)、`turn_status`（`COMPLETE`｜`INCOMPLETE`）、`boundary_ids_spanned`（有序）。
- **`CaptureTurnAccountingV1`**（每 capture 一条，诚实分母）：`schema_version`、`capture_occurrence_id`、`has_unlocalizable_prefix`(bool)、`prefix_event_counts_by_kind`（前缀内各 kind 计数）、`observed_event_counts_by_kind`（观测窗口内各 kind 计数；与 UserBlock/AgentStep/孤儿观测/SYSTEM/TOOL_CALL(经 batch) 划分守恒可验，§8）、`orphan_tool_observation_event_ids`（观测窗口内、配对到前缀调用或无配对、无归属 AgentStep 的结果）、`agent_step_count`、`query_turn_count`、`prefix_rooted_turn_count`、`observed_rooted_turn_count`、`has_compaction`。**不含** `processing_status`（M1D 全集恒 `COMPLETE`，无信息量，D4/D5）；`observed_user_event_count` 由 `observed_event_counts_by_kind["USER"]` 派生、不物化（D3）。
- **`ThreadTurnEdgeV1`**：`schema_version`、`edge_id`、`relation`（**恒** `STRUCTURAL_NEXT_TURN`）、`parent_query_turn_id`、`child_query_turn_id`、`evidence`（capture 内相邻的 turn_ordinal 见证）。

`turn_status`、`root_status`、`outcome_kind`、`parallel_semantics` 均为**固定函数**（`contracts.py` 单一定义），由 builder 按 §4 派生、validator 经同一 fold 复算 + 正交不变量断言（§8），不重复物化二义来源（[`m1c-processing-spec.md`](m1c-processing-spec.md) §4.3 DRY）。

---

## 3. 硬门

- **门① 纯确定性、零 LLM。** 全部分类仅用 §1 结构标量，相同输入逐字节一致。
- **门② 只在可观测窗口分类，前缀整体不可定位。** 只对 `event_scope=OBSERVED_REQUEST_WINDOW` 事件归类；`PRE_FIRST_OBSERVED_TERMINAL` 前缀**不产生 UserBlock/AgentStep/QueryTurn**，只在 `CaptureTurnAccountingV1` 里整体记账。绝不推断前缀内是真实query还是Harness注入（§183 明确不可定位）。
- **门③ 结构信号分类，不读正文。** 归类只依 `event_kind`/`event_scope`/配对记录/typed reader 冻结标量（`TextContent.utf8_byte_length`、`ContentBlocks.block_count`）；不足以判定→fail-closed，宁可 INCOMPLETE 不臆测（[`background-and-goals.md`](background-and-goals.md) §5）。
  - **D-b（附件）**：仅置 `has_non_text_blocks` 布尔（typed reader 的 `CONTENT_BLOCKS` 种类位可得）；不细分块类型（细分需读非冻结的 `blocks:list[Any]`，留 M2）。
- **门④ ToolObservation 按配对归属，不按位置。** 结果归属其 `tool_call_id` 配对的调用所在 AgentStep（跨 boundary 也如此），绝不用到达顺序反推因果（[`overall-plan.md`](overall-plan.md) §4.4）。
- **门⑤ Public/Control 隔离。** 全部产物（含事件 ID 引用）属 Control 侧 `private/`；`reports/` 仅聚合计数；控制文件闭合值域（稳定 ID/序数/枚举/64-hex/bool/计数），无正文/URL/绝对路径/主机身份（沿用 M1C 两层隐私）。

---

## 4. 处理逻辑（纯函数，逐 capture，无 IO）

**核心洞察：M1D 在"按 `sequence_number` 升序的可观测事件流"上工作，boundary 只作证据锚点与 capture 终止识别，不作切分单元。** 对每个 M1B 已编译 capture（全集，无 eligibility 分级；M1B validator 已断言其 `processing_status=COMPLETE`，quarantined 记录无 capture 行、无事件，结构上不进入 M1D）：

**步 0 · 取流。** 取该 capture 全部 `OBSERVED_REQUEST_WINDOW` 事件，按 `sequence_number` 升序。`PRE_FIRST` 前缀事件不进流，仅按 kind 计数写入 `CaptureTurnAccountingV1`（`has_unlocalizable_prefix = 前缀事件数>0`）。

**步 1 · 建 AgentStep（键=assistant 事件，与 boundary 无关）。** 对流中每个 `ASSISTANT_MESSAGE` 事件 A（不论其是否 boundary 终止）：
- `action_batch` = `assistant_event_id==A` 的 `ActionBatchV2`（无则空）。
- 对 `action_batch.tool_call_event_ids` 每个调用，查其 `ToolPairingRecordV3`：`matched_result_event_id` 存在且该结果为观测事件→计入 `tool_observation_event_ids`（按结果 `sequence_number` 排序）；否则该调用计入 `unresolved_tool_call_event_ids`。
- 产 `AgentStepV1`（`request_boundary_id` = A 的 boundary）。

**步 2 · 收孤儿观测。** 流中所有 `TOOL_RESULT` 事件里，配对到**前缀 assistant 调用**（`matched_call_event_id` 非观测事件）或无配对的，不归任何 AgentStep→计入 `orphan_tool_observation_event_ids`。

**步 3 · 切 QueryTurn（键=可定位 USER）。** 顺序扫描流，维护"当前 turn"：
- 遇 `USER` 事件：收敛**按 `sequence_number` 严格相邻**的极大 USER 段成一个 `UserBlockV1`（段间一旦出现任何非 USER 事件即断开；允许多条连续 user，[`overall-plan.md`](overall-plan.md) §4.5）；**起一个新 turn，`root_status=OBSERVED_ROOTED`**，绑定该 UserBlock。
- 遇 `ASSISTANT_MESSAGE` 事件（步 1 已建其 AgentStep）：
  - 若当前无 turn（capture 以 assistant 开头——即 53% 单 boundary 及所有"根在前缀"的情形）→**起首个 turn，`root_status=PREFIX_ROOTED`，`user_block_id=null`**。
  - 把该 AgentStep 追加进当前 turn；记其 boundary 入 `boundary_ids_spanned`。
- 遇 `SYSTEM` 事件（观测窗口内，Harness 包装/系统注入）：不起 turn，记入 capture 级记账；不塞进 UserBlock。
- `TOOL_RESULT` 事件不影响 turn 切分（其归属已由步 1/2 按配对确定）——**这正是"用户中途插话"（实测 216 例）能被正确切分的原因：插话前的结果仍归前一 turn 的 step，插话本身起新 turn。**

**步 4 · 定 turn_status 与 AssistantOutcome（镜像 M1B `_terminal_status` 的 tool_calls 优先约定）。** 对每个 turn 的**末个 AgentStep** A_last，经 `event_payload.py` typed reader 读 A_last assistant 的 content-presence（**只取冻结标量** `TextContent.utf8_byte_length` / `ContentBlocks.block_count`，不读文本值/块内）：
- **A_last 有 ActionBatch（末步是工具调用步）** → 无 `AssistantOutcome`，`turn_status=INCOMPLETE`。统一覆盖两种：结果未全观测（`unresolved_tool_call_event_ids` 非空，工具循环被截断）＋结果全到但本 turn 未再合成最终答案（收尾未落文本）。对应 §12 D-a：不引第三态。
- **A_last 无 ActionBatch** → 据 content-presence 定 `outcome_kind`：`TextContent.utf8_byte_length>0`（或合法 Data URL envelope，§201 恒表示非空原文）或 `ContentBlocks.block_count>0` → `TEXT_OUTCOME`；否则 `EMPTY_OUTCOME`。产 `AssistantOutcomeV1(terminal_assistant_event_id=A_last)`。
- `turn_status = COMPLETE` ⟺ `outcome_kind==TEXT_OUTCOME`；否则 `INCOMPLETE`（turn 仍保留，[`overall-plan.md`](overall-plan.md) §4.5）。
- **交叉核对（validator 强制，§8 不变量层）**：capture **观测窗口内序号最大的 `ASSISTANT_MESSAGE`**（即 capture 末条消息——M1B `_terminal_status(messages[-1])` 的判定对象；编译 capture 的末 boundary 终止恒为 assistant）经 `terminal_status_for_step(has_action_batch, content_present)` 的结构映射须与 `CaptureQualityV3.terminal_status` 一致：`TOOL_CALL_PENDING↔有 ActionBatch`、`TEXT_OUTCOME↔无 ActionBatch 且 content 非空`、`EMPTY_OUTCOME↔无 ActionBatch 且 content 空`。`INVALID`（末条非 assistant）M1D 不建模、跳过。注意核对对象是**末 assistant**而非"末 turn"：capture 以 USER-only turn 收尾时两者分叉，M1B oracle 只看末条消息。**中间 turn 无 M1B oracle**，由本规则独立派生、validator 复算一致即可（不臆测、可复现）。

**步 5 · capture 内结构边。** 同 capture 相邻 `turn_ordinal` i→i+1 连 `ThreadTurnEdgeV1(relation=STRUCTURAL_NEXT_TURN)`。无语义关系。

**步 6 · 记账。** 填 `CaptureTurnAccountingV1`（前缀/观测逐 kind 计数、孤儿观测、各类 turn 计数、compaction）。

全部产物按稳定 ID 排序写盘。

---

## 5. 三种真实 capture 形态的 worked trace（据 §0 实测形态）

**形态甲（53%，单 boundary）** 观测流 = `[A_term]`（仅一条终止 assistant，可能带 tool_calls）。
→ 无 USER→起 1 个 `PREFIX_ROOTED` turn；1 个 AgentStep；A_term 无 unresolved 且有文本无 tool_calls → `TEXT_OUTCOME`/`COMPLETE`；若 A_term 发了 tool_calls 而无结果 → `INCOMPLETE`、outcome 缺失。记账：`has_unlocalizable_prefix=true`、`observed_rooted_turn_count=0`。

**形态乙（无 USER 的多步工具循环，属 80% 无根之列）** 流 = `[tool_result…, A1(+calls), tool_result…, A2(+calls), …, A_n(text)]`。
→ 起 1 个 `PREFIX_ROOTED` turn；A1..A_n 各一 AgentStep（A1 的结果落在 A2 前的 tool_result，按配对归 A1）；开头的 tool_result 若配对到前缀调用→孤儿观测；A_n 有文本无未决调用→`COMPLETE`。整段是"用户在前缀里问过、这里只看到 agent 干活到给出答案"的**如实无根记录**。

**形态丙（20%，含可定位 USER；含实测 216 例中途插话）** 流 = `[A1(+calls), tool_result…, USER, A2(text)]`。
→ 步1：A1、A2 各一 AgentStep，A1 结果按配对归 A1。步3：A1 前无 USER→turn T0 `PREFIX_ROOTED` 含 {A1}；遇 USER→起 T1 `OBSERVED_ROOTED` 绑 UserBlock，含 {A2}。步4：T0 末步 A1 结果已到但无最终文本→`INCOMPLETE`；T1 末步 A2 文本→`COMPLETE`。边 T0→T1。**用户中途插话被干净切成两回合，插话前的工具结果正确归属前一回合。**

---

## 6. 输入全集与诚实记账

- 不用全局 `valid`（[`overall-plan.md`](overall-plan.md) §5）。M1D 的输入全集是 M1B 已编译 capture，**恒 `COMPLETE`**：M1B v4 已删 `PARTIAL`，`QUARANTINED` 记录在 M1B 只有 attrition/quality 行、无 capture 行与事件，M1D 结构上看不到、也不重复或伪称检查过（契约不保留无生产者状态，D4/D5）。全部 capture 产回合，逐回合判 `turn_status`。
- `INCOMPLETE` 回合保留（承 §4.5）；`PREFIX_ROOTED` 回合保留但显式标注根不可定位——下游据 `root_status` 决定能否用于任务画像。
- `has_compaction` 如实透传到 capture 记账（前缀被压缩、边界更不可靠），不改变切分。
- 基础设施失败/无效/quarantine 分账属 M1B attrition report（[`background-and-goals.md`](background-and-goals.md) §5、§9 诚实分母）；M1D 的诚实分母是 `capture_count`（=M1B 已编译 capture 数）与逐 capture 的前缀/观测逐 kind 计数。

---

## 7. 稳定 ID（M1D 自有命名空间）

内容寻址，命名空间独立（`m1d-query-turn-v1`/`m1d-agent-step-v1`/…），不复用 M1B/M1C 私有派生公式；上游 ID 当不透明外键。`m1d_run_id = stable_id(namespace, {m1d_contract_version, m1b_run_id, m1b_artifact_manifest_sha256})`——绑定输入身份而非路径（同 M1C §5.6/§10）。各产物 ID 以其确定性组成（如 AgentStep 以 `capture_occurrence_id+assistant_event_id`）取，保证复算一致。

---

## 8. 独立 validator（两层信任边界）

`validate_query_turn_run(<m1d_run>, <m1b_run>)`，无 R01 分支。先做产物完整性（逐文件重哈希 + manifest 自摘要 + inventory + canonical + 隐私摘要 + 控制文件无绝对路径/URL），再经 M1B 权威 `validate_compiled_run` 重新读入上游（先校验后消费），断言 `m1b_run_id`/`m1b_artifact_manifest_sha256`/内容寻址 `query_turn_run_id` 处处一致。图完整性分**两层**，各答一个问题：

**① 篡改检测——"已发布产物是否就是这份 M1B 输入应得的产物？"** 从 M1B 已发布字段重建输入视图，调用与 pipeline **同一个纯 fold**（`builder.build_query_turn_graph`，无 IO、无私有公式）重建期望，六张私有表逐一与 `private/` 做**双向集合相等**（canonical 字节比对：`*_MISSING`/`*_PHANTOM`/`*_MISMATCH`）。删产物、造幻影、改归属/状态/计数、同步重签，全部被推翻。**v0.3 明确不复刻 fold**（v0.2 写"不调用 M1D 组装器重建期望"已撤回）：复制同一算法对 fold 自身缺陷零检出——两份同 bug 必互相一致——只会让每次修改的维护面翻倍并制造漂移误报；比照 M1B v4 R5"业务规则只有一个定义、各方独立重建输入"。

**② 正交不变量——"fold 本身是否算对了？"** 只用 M1B 视图 + M1D 已发布表、**不经 fold** 断言的守恒律（测试以"置空 bijection 仍能抓到"证明其检出力不依赖 fold）：
- **观测事件分区守恒（D1）**：每 capture 观测事件 ID 集合 **恰好** = UserBlock.event_ids ∪ AgentStep.assistant_event_id ∪ AgentStep.tool_observation_event_ids ∪ 记账孤儿观测 ∪ 观测 SYSTEM ∪ 已发布 AgentStep 所属 ActionBatch 的 TOOL_CALL；各成员两两不交（`M1D_PARTITION_OVERLAP`）、kind 与划分相符（`_KIND_MISMATCH`）、不引用前缀/他 capture 事件（`_FOREIGN_EVENT`）、无遗漏（`_UNCOVERED`）。
- **记账逐 kind 对账**：`observed_event_counts_by_kind`/`prefix_event_counts_by_kind` 与 M1B 视图相等（`M1D_ACCOUNTING_*_COUNT_MISMATCH`）。
- **末 assistant 与 M1B oracle 交叉核对**：见 §4 步 4（`QUERY_TURN_TERMINAL_STATUS_MISMATCH`）。
- **报告计数由已发布表独立重算**：`reports/m1d_report.json.counts` 不从 fold 取，而由六张表计数（`QUERY_TURN_REPORT_COUNT_MISMATCH`）；键集闭合于 `QUERY_TURN_COUNT_KEYS`。

结构纯度（`relation` 恒 `STRUCTURAL_NEXT_TURN`、无语义关系/`TaskEpisode`）由闭合枚举与 bijection 共同保证；两层隐私沿用 M1C（`reports/` 仅聚合计数；`private/` 闭合值域；控制文件无主机身份）。

**决定记录（评审 §3）**：配对索引 `matched_call_event_id` 重复不设 tripwire——M1B 契约下不可能重复且 M1B validator 已断言严格 1:1，M1D 先校验后消费；空观测流（capture 全部事件在前缀）在合法 M1B 输入上不可达，以防御性测试固定行为为"0 turn / 0 edge / 1 accounting"。

**known-item — 末 assistant 交叉核对是防御性 tripwire。** `QUERY_TURN_TERMINAL_STATUS_MISMATCH` 在正常输入下**永不 fire**：M1B validator（`trajectory/validation.py` `QUALITY_TERMINAL_STATUS_MISMATCH`）已独立复算并断言 `terminal_status`，故先校验后消费的 M1B 视图恒自洽；而篡改 M1D 私有表不触及本核对的输入（末观测 ASSISTANT 标量取自 M1B 视图），无法被强制点亮。它是与 M1C `LINEAGE_REQUEST_NODE_CONSERVATION` 同构的守恒 tripwire——存在以证伪"上游 oracle 与本层记账脱节"，不作正例强测（测试仅断言其在合法 run 上静默）。

---

## 9. 输出布局

```text
<m1d_artifact_root>/<content_addressed_m1d_run_id>/
├── query_turn_manifest.json           # 绑定 m1b_run_id + artifact_manifest SHA-256（非路径）
├── private/
│   ├── query_turns.jsonl
│   ├── user_blocks.jsonl
│   ├── agent_steps.jsonl
│   ├── assistant_outcomes.jsonl
│   ├── capture_turn_accounting.jsonl
│   └── thread_turn_edges.jsonl
├── reports/
│   └── m1d_report.json               # 仅聚合计数
├── artifact_manifest.json
└── run_receipt.json
```
真实 M1D 产物不进 Git。

---

## 10. 完成条件

- 观测事件分区守恒（UserBlock/AgentStep/孤儿/SYSTEM/TOOL_CALL 划分，validator 不经 fold 断言），前缀零回合、整体记账；
- 六张私有表双向 bijection + 记账对账 + 末 assistant 交叉核对 + 报告由表重算 全通过；
- 六类只在观测窗口、纯结构、零 LLM、逐字节确定；两次独立构建产物一致；
- 结构纯度（无语义边/无 Episode）+ 两层隐私 + 稳定 ID 复算通过；
- INCOMPLETE/PREFIX_ROOTED 保留；诚实分母 = M1B 已编译 capture 数 + 逐 capture 逐 kind 计数；
- 单元测试含正常与关键失败场景（三形态 + 中途插话 + 孤儿观测 + 前缀守恒 + 分区守恒属性 + D-a 结果全到仍 INCOMPLETE + 空观测流 + 关 bijection 的不变量篡改用例）；静态与差异检查通过；
- 无 R01 硬编码常量、无模型调用、无语义关系占位、无 M2/World 预埋。

---

## 11. 明确缓做 / M2 边界

留 M2（两次独立、封闭枚举的 LLM 语义提取，[`overall-plan.md`](overall-plan.md) §4.6/§8.3）：语义关系 `CONTINUES/REFINES/DEPENDS_ON/CORRECTS/CANCELS/BRANCHES_FROM/NEW_TASK/AMBIGUOUS`、`TaskEpisode` 及 DAG、任务/环境画像、可解性、重建 eligibility、**前缀内容的任何语义还原**、**跨 capture 线程合并**。比照 M1C 对 Grade-B 的处理：这些枚举**不进 M1D 代码、不放占位值**，仅留档。进入 M2 前须先冻结最小、带来源的 `SourceAnnotationProjection`（[`r01-processing-spec.md`](r01-processing-spec.md) §8.3）。

---

## 12. 剩余待定小决策（地基已定，仅细节）

| 编号 | 问题 | 建议 |
|---|---|---|
| **D-a** | `outcome_kind` 是否需第三态区分"发了调用且结果全到但无最终文本（工具循环收尾未合成答案）"？ | 建议归入 `INCOMPLETE`（末步有 ActionBatch 即非 TEXT_OUTCOME），不新增态；避免过度建模。 |
| **D-b** | 附件仅置 `has_non_text_blocks` 布尔？ | **是**（细分需读非冻结块，留 M2）。 |
| **D-c** | v0.2 是否启用 M1C 做跨 capture `ThreadTurnGraph`？ | **否**（YAGNI：R01 线程价值多在前缀；跨 capture 合并本属 M2）。仅 capture 内结构边。 |
| **D-d** | `PREFIX_ROOTED` 单终止 capture（形态甲，53%）是否仍产回合，还是只记账？ | **仍产**（一个 PREFIX_ROOTED/单 AgentStep 回合），保持"观测事件全覆盖"守恒可验；其低价值由 `root_status` 显式暴露给下游。 |
| **D-e**（v0.3） | `parallel_semantics` 无 ActionBatch 时取 `UNKNOWN` 还是 `null`？ | **`null`**（无 batch 则无并行语义，不臆造；D2）。 |
| **D-f**（v0.3） | 终态交叉核对对象是"末 turn"还是"末 assistant"？ | **末 assistant**（观测窗口内序号最大的 `ASSISTANT_MESSAGE`），与 M1B `_terminal_status(messages[-1])` 同对象；USER-only 收尾时末 turn 无 assistant、M1B 给 INVALID、M1D 跳过。 |
| **D-g**（v0.3） | validator 是否复刻 fold 以求"独立"？ | **否**。篡改检测调用同一纯 fold；fold 缺陷由不经 fold 的正交不变量层检出（§8）。 |
