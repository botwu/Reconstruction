# M2 前置：来源投影处理规格（v0.3，冻结）

版本：v0.3（**冻结**；实现依据）

日期：2026-09-02（v0.1 草案）；2026-09-03（v0.2 吸收 [`m2-source-projection-review-20260903.md`](m2-source-projection-review-20260903.md) P1–P8 / D6–D9；v0.3 依 §8 探针在 v4 run 上的结果冻结白名单与验收向量）

状态：**已冻结并已实现、已正式验收**（实现提交 `1de39ae`，包 `src/traceforge/source_projection/`；验收 [`r01-user-text-projection-validation.md`](r01-user-text-projection-validation.md)：干净克隆双跑复现 run `47cfac20…`（绑定 M1D）/ `9dc26f2f…`（未绑定），23 项计数与 12 个标签计数与 §6 验收向量逐项相等）。本文兑现 [`r01-processing-spec.md`](r01-processing-spec.md) §8.3 的 M2 前置硬门（`UserTextProjection` 必做；`SourceAnnotationProjection`/`SourceResolver` 在 M2 首次消费 `domain_meta` 前必做）。只定义 I/O 与确定性算法，不在 M1B/M1C/M1D 预埋任何结构（[`../AGENTS.md`](../AGENTS.md) §1 YAGNI）。

v0.2 相对 v0.1 的变化：删除无生产者的 `QUARANTINED`，新增透传字段 `content_form` 与类别 `NO_LEADING_TEXT`（§2.2/§2.3）；冻结开头标签文法（§2.3）；`UNKNOWN_TAGGED` 恒不输出标签名（§2.3/§3）；M1D 回指从"可空"改为"提供即必须可解析"（§2.1/§3）；报告增加第三个分母（§2.4）；白名单准入规则（§2.3）；validator 改为与 M1D 同构的两层信任边界（§3）；包名、契约版本、run ID 绑定（§4）；只读探针固定程序（§8）。

v0.3 相对 v0.2 的变化：§0 换为 v4 run `6be45e01…` 的完整探针结果；白名单 A/B/C 成员冻结，删除无成员的白名单 D 与无生产者的 `ATTACHMENT_MARKER`；新增 `EMPTY_TEXT`（去空白后为空的正文，R01 有 4 条生产者，且使分母定义精确）；准入规则 ② 明确"单一通用英文单词须能引用注入协议出处"，`skill`（41 次）暂不准入并登记为首个扩展候选（§7 D10）；§6 写入验收向量。

阅读前置：[`../AGENTS.md`](../AGENTS.md)、[`background-and-goals.md`](background-and-goals.md) §3/§5、[`overall-plan.md`](overall-plan.md) §4.6/§5/§6、[`r01-processing-spec.md`](r01-processing-spec.md) §8.2/§8.3、[`m1d-processing-spec.md`](m1d-processing-spec.md) §0/§3。

---

## 0. 实测地基（人工 sanity 观测，不写进代码）

2026-09-03 按 §8 程序对已验收 M1B **v4** run `6be45e01…` 的 `private/event_occurrences.jsonl` 做只读结构探针（只看 `event_kind`、`event_scope`、`content.kind`、`value` 形态与正文**开头标签名**，不读正文语义）。v0.1 在 v3 run 上的数字与之逐项一致（v4 对 USER 事件字节中性）。

| 观察 | 数值 | 含义 |
| --- | ---: | --- |
| USER 事件总数 | 14,407 | 全部 `content.kind=TEXT`；`value` 为字符串 14,405、为隐私 envelope 2 |
| 其中落在 `PRE_FIRST_OBSERVED_TERMINAL` 前缀 | 13,225（91.8%） | M1D 规格 §0 已确认：前缀不可定位到 request boundary，不成回合；`OBSERVED` 事件的 `request_boundary_id` 无一为空、前缀无一非空 |
| 去空白后开头不是开标签的普通文本 | 11,694 | 真实用户 query 的候选主体（含 2 条分段 envelope，其首段为普通文本） |
| 去空白后为空 | 4 | 全在前缀；v0.3 单列为 `EMPTY_TEXT` |
| `<` 开头但不满足开标签文法（`</x>`、`<3` 等）、前置 BOM/零宽字符 | 0 | 文法边界在 R01 上无争议样本 |
| 开头为开标签 | 2,709 | 出现 ≥ 2 次的标签名 15 个（下表），只出现 1 次的标签名 7 个（名字不落盘） |
| **至少含一条普通用户文本的 capture** | **1,680 / 1,683** | 其中 1,350 个的普通文本全部位于前缀；仅 330 个在观测窗口内有普通文本（≤ M1D 有 `UserBlock` 的 342 个 capture） |

开标签名频表（≥ 2 次；形态 bare = `<tag>`，attrs = `<tag attr=…>`）：

| 标签名 | 总数 | 前缀 | 观测 | 形态 | v0.3 处置 |
| --- | ---: | ---: | ---: | --- | --- |
| `environment_context` | 2,222 | 1,974 | 248 | bare | A |
| `in-app-browser-context` | 203 | 192 | 11 | attrs | A |
| `turn_aborted` | 91 | 78 | 13 | bare | C |
| `system-reminder` | 66 | 56 | 10 | bare 63 / attrs 3 | A |
| `skill` | 41 | 40 | 1 | bare | 不准入（规则 ②，D10） |
| `recommended_plugins` | 35 | 35 | 0 | bare | B |
| `codex_delegation` | 23 | 22 | 1 | bare | B |
| `codex_internal_context` | 4 | 2 | 2 | attrs | A |
| `task` | 3 | 3 | 0 | bare | 不准入（规则 ②） |
| `subagent_notification` | 3 | 1 | 2 | bare | C |
| `image` | 3 | 3 | 0 | attrs | 不准入（规则 ②） |
| `user_interjection` | 2 | 0 | 2 | bare | C |
| `system-conventions` | 2 | 2 | 0 | bare | A |
| `available-deferred-tools` | 2 | 2 | 0 | bare | A |
| `local-command-caveat` | 2 | 2 | 0 | bare | A |

v0.1 暂列白名单 A 的 `session_context_files` 只出现 1 次，依规则 ① 不准入。两条隐私 envelope 均为 `text-with-data-url-segments.v1`（3 段：文本 / `text/html` Data URL / 文本），首段为普通文本，故归 `PLAIN_USER_TEXT`；R01 上 `DATA_URL_SUMMARY` 与 `CONTENT_BLOCKS` 形态为 0，但二者是 M1B 契约合法形态，契约必须覆盖。

两个结论决定本规格的定位：

1. **前缀 USER 正文必须能进入 M2 的任务意图证据**，否则 `ObservedTaskDistribution` 只剩 M1D 规格 §0 所述的约 342 个有根 capture。放行的代价是这些证据不能绑定到任何 request boundary 或 QueryTurn，必须以显式 `locality` 标记暴露给下游。
2. **用户角色的消息不等于用户文本。** 约 19% 的 USER 事件是 Harness 注入。区分只能按结构（开头标签的封闭白名单），不能按语义猜测；这与 M1D 门②"绝不推断前缀内是真实 query 还是 Harness 注入"不冲突——M1D 不做，本层做，且只做结构判定。

另一个事实：M1B 已发布 `captures.jsonl` 只保留 `domain_meta_sha256`，上游后处理标注（rubric、任务摘要、风险、输入审计）**不在任何已发布字段中**。要使用它们必须重新读取冻结 JSONL（§5）。

---

## 1. 授权范围

本层只产出两类只读、Control 侧、带来源引用的投影，均**不复制 M1B 正文，只引用稳定 ID**：

```text
UserTextProjection          （必做）  对每个 USER 事件的结构分类与可定位性注解
SourceAnnotationProjection  （缓做，见 §5 D3） 对 domain_meta 白名单字段的只读先验投影
```

不实现、也不得声称实现：任务意图、语义关系、TaskEpisode、任务或环境画像（属 M2 本体）；任何 LLM 调用；对前缀内部回合结构的任何推断；对 M1B/M1C/M1D 字节的任何改动。

---

## 2. UserTextProjection

### 2.1 输入

一个已发布 M1B run（先调 `validate_compiled_run` 校验再消费，同 M1C/M1D 信任边界；校验失败即拒绝，不产任何行）。只消费 `EventOccurrenceV3` 的 `event_occurrence_id`、`capture_occurrence_id`、`sequence_number`、`event_kind`、`event_scope`、`request_boundary_id`，以及经 `event_payload.py` typed reader 读出的 `ContentPayload`：`TextContent`（`utf8_byte_length`、`value` 的**开头片段**，`value` 可为字符串或隐私 envelope）或 `ContentBlocks`（只取种类位，不读块内）。

reader 在已校验 run 上不可能失败（M1B validator 已对每个事件跑过同一 typed reader）；若仍失败，抛异常 fail-closed，**不**产生任何"隔离"行——契约之外的形态不设枚举值。

可选次输入：已发布 M1D run（先调 M1D 权威 `validate_query_turn_run` 校验），用于把 `OBSERVED_REQUEST_WINDOW` 内的 USER 事件回指其 `user_block_id`。未提供时全部 `user_block_id = null`；**提供时每个 `OBSERVED` USER 事件必须能在 `user_blocks.jsonl` 的 `event_ids` 中找到恰一个归属**（M1D 规格 §8 分区守恒保证），找不到是契约违反，构建失败。

### 2.2 输出 `UserTextAnnotationV1`（每个 USER 事件一条，以 `event_occurrence_id` 键控，不造新 ID）

```yaml
schema_version:
event_occurrence_id:        # 引用 M1B，不复制正文；本表主键
capture_occurrence_id:
locality:                   # PREFIX_UNLOCALIZED | OBSERVED（由 event_scope 一一映射）
request_boundary_id:        # OBSERVED 时非空且与 M1B 一致；PREFIX_UNLOCALIZED 恒空
user_block_id:              # 未提供 M1D run 恒空；提供时 OBSERVED 必非空、PREFIX_UNLOCALIZED 必空
content_form:               # TEXT_STRING | TEXT_WITH_DATA_URL_SEGMENTS | DATA_URL_SUMMARY | CONTENT_BLOCKS
text_class:                 # 见 2.3 封闭枚举
leading_tag:                # 仅 HARNESS_CONTEXT / HARNESS_CAPABILITY / CONTROL_SIGNAL 时为白名单内标签名字面量；其余（含 UNKNOWN_TAGGED）恒空
utf8_byte_length:           # TEXT 时透传 TextContent.utf8_byte_length（envelope 时为脱敏前原文长度）；CONTENT_BLOCKS 时为 null
```

### 2.3 `content_form`、开头标签文法与 `text_class` 封闭枚举（纯结构，只看开头）

**`content_form`**（透传 typed reader 与 `privacy.py` envelope kind，不做判断）：

```text
TEXT_STRING                    TextContent.value 是 str
TEXT_WITH_DATA_URL_SEGMENTS    value 是 traceforge.privacy.text-with-data-url-segments.v1 envelope
DATA_URL_SUMMARY               value 是 traceforge.privacy.data-url-summary.v1 envelope
CONTENT_BLOCKS                 ContentBlocks（R01 为 0，但属 M1B 契约合法形态，必须覆盖）
```

**判定用开头文本 `head`**：`TEXT_STRING` 取 `value`；`TEXT_WITH_DATA_URL_SEGMENTS` 取首个 segment——若为 `text-segment.v1` 取其 `value`，若为 Data URL 摘要则无开头文本；`DATA_URL_SUMMARY` 与 `CONTENT_BLOCKS` 无开头文本。`head` 先按 Python `str.isspace()` 左侧去空白，再只取前 **256 个码点**参与判定；判定结果只依赖这 256 个码点。

**开头标签文法（契约常量，validator 复算的对象）**：

```text
LEADING_TAG = ^<([A-Za-z_][A-Za-z0-9_.:-]*)(?=[\s>/])      # 作用于去空白后的 head
```

标签名大小写敏感，与白名单精确匹配；允许属性与自闭合（`<task id="x">`、`<x/>`）；不解析 XML、不看闭合、不读标签内容。去空白后以 `<` 开头但不匹配 `LEADING_TAG` 者（`</x>`、`<3`、`<<`、`<-`、`< x`）**不是**开标签。

**`text_class`**（v0.3 冻结）：

```text
PLAIN_USER_TEXT       有开头文本，去空白后非空，且开头不是一个匹配 LEADING_TAG 的开标签
EMPTY_TEXT            有开头文本，但去空白后为空
HARNESS_CONTEXT       开标签名 ∈ 白名单 A：environment_context, in-app-browser-context, system-reminder,
                      system-conventions, codex_internal_context, local-command-caveat,
                      available-deferred-tools
HARNESS_CAPABILITY    开标签名 ∈ 白名单 B：recommended_plugins, codex_delegation
CONTROL_SIGNAL        开标签名 ∈ 白名单 C：turn_aborted, user_interjection, subagent_notification
UNKNOWN_TAGGED        匹配 LEADING_TAG 但标签名不在 A–C 任一白名单；leading_tag 恒空
NO_LEADING_TEXT       无开头文本：DATA_URL_SUMMARY、首段为 Data URL 摘要的分段 envelope、CONTENT_BLOCKS
```

规则：

- 白名单标签名写进契约常量，**新标签只能通过修订本规格进入白名单**，代码不得自动学习；
- **白名单准入规则**（三条同时满足）：① 在 §8 探针（v4 run）中出现 ≥ 2 次；② 标签名是 Harness 专有词汇——下划线/连字符复合词或产品专名直接满足；单一通用英文单词（`skill`、`task`、`image`、`file`、`irc`）不满足，除非本规格能引用其所属 Harness 注入协议的出处且探针中形态一致，因为它们可能是用户自写标记；③ 语义可由标签名自证。据此 v0.3：`session_context_files`（1 次）依 ① 不准入；`skill`（41 次，全 bare）、`task`、`image` 依 ② 不准入，均落 `UNKNOWN_TAGGED`；无成员的白名单 D 与 `ATTACHMENT_MARKER` 删除（无生产者）。`skill` 登记为首个扩展候选（§7 D10）；
- `EMPTY_TEXT` 单列而不并入 `PLAIN_USER_TEXT`，是为了让门④三个分母精确表示"有非空普通文本"；`NO_LEADING_TEXT` 在 R01 上为 0，但由输入契约合法形态定义，保留；
- `PLAIN_USER_TEXT` 不再细分（D1）。它仍可能含有 Harness 拼接文本，这是 M2 语义层的问题，本层不猜；
- `UNKNOWN_TAGGED` 是 fail-closed 的默认落点，其计数进入公共报告；但**标签名不得出现在任何产物或报告中**（可能是用户文本），白名单扩展评审只能经 §8 的 Control 侧离线探针；
- `<user_interjection>` 归 `CONTROL_SIGNAL` 而非 `PLAIN_USER_TEXT`：它是 Harness 对用户插话的包装，实际插话正文是否可用留 M2（D2）；
- 实现与 validator **共用**同一纯函数 `classify_leading_text(head) -> (text_class, leading_tag)`（规则只一个定义）；validator 的独立性由 §3 不经该函数的正交不变量层提供。

### 2.4 硬门

- **门① 纯确定性、零 LLM**，逐字节可复现；
- **门② 不推断前缀结构。** `PREFIX_UNLOCALIZED` 事件之间不建任何顺序、归属或回合关系；本层只逐事件注解；
- **门③ 只引用不复制。** 产物不含正文、不含 `value` 的任何切片；`leading_tag` 只允许白名单内的标签名字面量，`UNKNOWN_TAGGED` 的标签名不得以任何形式落盘；
- **门④ 分母诚实。** 公共报告按 `(locality, text_class)` 与 `content_form` 给出计数，并给出三个分母：`captures_with_plain_user_text`（至少含一条 `PLAIN_USER_TEXT` 的 capture 数）、`captures_with_plain_user_text_only_in_prefix`（其 `PLAIN_USER_TEXT` 全部位于前缀）、`captures_with_observed_plain_user_text`（观测窗口内至少含一条 `PLAIN_USER_TEXT`）。M2 任何分布陈述必须引用后两者说明可定位性缺口；提供 M1D run 时第三个分母 ≤ M1D `user_blocks.jsonl` 的 capture 去重数（R01 为 342）是跨模块不变量；
- **门⑤ Public/Control 隔离。** 全部产物属 Control 侧 `private/`；`reports/` 仅聚合计数；控制文件闭合值域，沿用 M1C 两层隐私与 `_scan_control_pathlike` known-item。

### 2.5 M2 消费约定（写入本规格，作为 M2 规格的前置约束）

- M2 语义提取只允许把 `text_class=PLAIN_USER_TEXT` 的事件作为**任务意图证据**；`HARNESS_*` 事件可作为**环境暴露证据**（system/harness fingerprint、工具可用性）但不得进入意图；`CONTROL_SIGNAL` 只作为中断/取消线索；`EMPTY_TEXT`、`UNKNOWN_TAGGED` 与 `NO_LEADING_TEXT` 不作任何证据，只计数；
- 由 `PREFIX_UNLOCALIZED` 证据得出的 Episode 或意图必须携带 `intent_locality=PREFIX_ONLY`，进入 `ObservedTaskDistribution` 时单列，不与有根 Episode 混算；
- M2 不得因为前缀证据无法绑定 boundary 而回退到"按绝对路径重解析原始 JSONL 找上下文"。

---

## 3. 独立 validator（两层信任边界，与 M1D 规格 §8 同构）

双参 `<projection_run> <m1b_run>`，若 `projection_manifest` 绑定了 M1D run 则第三参必填、缺失即失败。先对 M1B run（与 M1D run）跑各自权威 validator，再：

- **篡改检测层**：从已校验 M1B（+M1D）输入重跑同一纯函数 `classify_leading_text` 与同一 fold，逐行比对已发布 `user_text_annotations.jsonl`、报告与 manifest 摘要；任何差异即失败。
- **正交不变量层**（不经 `classify_leading_text`，独立成立的守恒律）：
  - **USER 事件双向 bijection**：M1B 每个 USER 事件恰有一条注解，无遗漏无幻影；主键 `event_occurrence_id` 唯一；
  - `locality ⇔ event_scope` 一一映射；`request_boundary_id` 与 M1B 事件逐行一致；`PREFIX_UNLOCALIZED ⇒ request_boundary_id = null`；
  - `content_form` 与 typed reader 读出的 `ContentPayload` 类型及 envelope kind 一致；`utf8_byte_length` 与 `TextContent.utf8_byte_length` 一致、`CONTENT_BLOCKS ⇒ null`；
  - `leading_tag ≠ null ⇔ text_class ∈ {HARNESS_CONTEXT, HARNESS_CAPABILITY, CONTROL_SIGNAL}`，且 `leading_tag` 恒在对应类别的白名单内；`text_class = NO_LEADING_TEXT ⇔ content_form ∈ {DATA_URL_SUMMARY, CONTENT_BLOCKS} ∨ 分段 envelope 首段为 Data URL 摘要`（分段 envelope 首段若为文本段，其正文可能全为空白，此时归 `EMPTY_TEXT`）；
  - M1D 绑定时：`OBSERVED ⇒ user_block_id ≠ null` 且该 `UserBlock.event_ids` 含此事件、`UserBlock.capture_occurrence_id` 一致；`PREFIX_UNLOCALIZED ⇒ user_block_id = null`；未绑定时全表 `user_block_id = null`；
  - 报告的 `(locality, text_class)`、`content_form` 计数与三个分母由已发布注解表重算一致；M1D 绑定时 `captures_with_observed_plain_user_text ≤ |{UserBlock.capture_occurrence_id}|`；
  - `private/` 值域闭合：稳定 ID、枚举、白名单标签名字面量、整数、null；沿用 M1C 两层隐私与 `_scan_control_pathlike`。

---

## 4. 输出布局、命名与身份

包 `src/traceforge/source_projection/`（容纳 `UserTextProjection`；日后 `SourceAnnotationProjection` 加入不改名）；契约版本 `user-text-projection-v1`；CLI `source-projection build --m1b-run <dir> [--m1d-run <dir>] --output <root>`；脚本 `scripts/validate_user_text_projection_run.py`。

run ID = `stable_id(contract_version, m1b_run_id, m1b_artifact_manifest_sha256, m1d_run_id | null, m1d_artifact_manifest_sha256 | null)`，与路径、机器、provenance 无关。

```text
<projection_artifact_root>/<content_addressed_run_id>/
├── projection_manifest.json        # 绑定 m1b_run_id + artifact_manifest SHA-256（非路径）；绑定 M1D 时含 m1d_run_id + 其 manifest SHA-256
├── private/
│   └── user_text_annotations.jsonl # 以 event_occurrence_id 键控
├── reports/
│   └── projection_report.json      # 仅聚合计数与三个分母
├── artifact_manifest.json
└── run_receipt.json
```

---

## 5. SourceAnnotationProjection（domain_meta 先验）——建议缓做

`domain_meta` 含上游 rubric、任务摘要、风险与输入审计。要投影它们必须以只读 `SourceResolver` 重新读取冻结 JSONL：按 `source_records.jsonl` 的 `dataset_sha256 + line_number + byte_offset + line_sha256` 定位物理行、重算 `domain_meta_sha256` 与 `captures.jsonl` 比对，再只输出经审核的白名单字段，每个字段标记 `SOURCE_PRIOR`。

**决定 D3（v0.2）：M2 v1 不消费 domain_meta，本投影缓做；[`r01-processing-spec.md`](r01-processing-spec.md) §8.3 硬门同步改为"M2 首次消费 `domain_meta` 之前必做"。** 原则性理由只有一条：

- 上游"任务摘要"是另一模型生成的标签。M2 若把它作为输入，两次独立语义提取就不再独立；它更适合在 M2 完成后作为**外部对照 oracle** 计算一致率，而不是作为先验。

以下是 R01 特有的辅助事实，不构成通用依据：R01 全部 1,683 条的主 rubric 相同，rubric 对 R01 内部分布没有区分力；`input_truncated` 已由 M1B 投影为 `input_truncation_status`（v4 起 `SOURCE_REPORTS_*`），无需重复；验收机上完整 R01 位于 AFS 真源路径（[`session-handoff.md`](session-handoff.md) §4），仓内副本是截断副本。

若 M2 需要风险标签做隐私门控，则只投影 `risk` 白名单字段，仍不投影任务摘要；届时按 [`../AGENTS.md`](../AGENTS.md) 单独起规格。

---

## 6. 完成条件

- USER 事件全覆盖 bijection；`locality`/`content_form`/`text_class`/`leading_tag` 经 §3 两层校验；`EMPTY_TEXT`、`UNKNOWN_TAGGED` 与 `NO_LEADING_TEXT` 计数进入公共报告；
- 三个分母可由已发布注解表独立重算；M1D 绑定时跨模块不变量成立；
- 纯结构、零 LLM、逐字节确定；两次独立构建一致；两层隐私通过；
- 单元测试含：普通文本、每类白名单标签（含带属性与自闭合形态）、未知标签（断言 `leading_tag` 为空）、`</x>`/`<3`/`<<` 开头、前置空白与 BOM、空正文、Data URL 摘要 envelope、分段 envelope 首段为文本/为 Data URL/为全空白文本、`CONTENT_BLOCKS`、M1D 回指有/无、M1D 绑定但事件无归属（构建失败）、错误 oracle fail-closed、篡改重签与置空篡改检测后不变量层仍 fail-closed；
- 无 R01 硬编码常量（§0/§6 数字不入代码）、无语义占位、无 M2 预埋。

以上完成条件已由提交 `1de39ae` 兑现并经 [`r01-user-text-projection-validation.md`](r01-user-text-projection-validation.md) 验收（2026-09-03）；单元测试清单中的每一项在 `tests/test_source_projection_*.py` 中都有对应用例。

**验收向量**（§8 探针按 v0.3 规则在 v4 run `6be45e01…` 上的参考计算；[`r01-user-text-projection-validation.md`](r01-user-text-projection-validation.md) §4 逐项对照：**全部相等**）：

| 计数 | 值 |
| --- | ---: |
| 注解行数 = USER 事件数 | 14,407 |
| `content_form`：`TEXT_STRING` / `TEXT_WITH_DATA_URL_SEGMENTS` / `DATA_URL_SUMMARY` / `CONTENT_BLOCKS` | 14,405 / 2 / 0 / 0 |
| `OBSERVED`：`PLAIN_USER_TEXT` / `HARNESS_CONTEXT` / `HARNESS_CAPABILITY` / `CONTROL_SIGNAL` / `UNKNOWN_TAGGED` / `EMPTY_TEXT` / `NO_LEADING_TEXT` | 892 / 271 / 1 / 17 / 1 / 0 / 0（合计 1,182） |
| `PREFIX_UNLOCALIZED`：同序 | 10,802 / 2,230 / 57 / 79 / 53 / 4 / 0（合计 13,225） |
| 全体：同序 | 11,694 / 2,501 / 58 / 96 / 54 / 4 / 0 |
| 白名单标签计数 | `environment_context` 2,222；`in-app-browser-context` 203；`system-reminder` 66；`codex_internal_context` 4；`system-conventions` 2；`available-deferred-tools` 2；`local-command-caveat` 2；`recommended_plugins` 35；`codex_delegation` 23；`turn_aborted` 91；`subagent_notification` 3；`user_interjection` 2 |
| `captures_with_plain_user_text` / `…_only_in_prefix` / `captures_with_observed_plain_user_text` | 1,680 / 1,350 / 330 |
| 绑定 M1D run `84d826b3…` 时 | 全部 1,182 个 `OBSERVED` 事件回指非空；330 ≤ 342 |

---

## 7. 决策点

| 编号 | 问题 | 决定 |
| --- | --- | --- |
| D1 | `PLAIN_USER_TEXT` 是否再按长度/语言/是否含 URL 细分 | 否。任何细分都需读正文语义，留 M2 |
| D2 | `<user_interjection>` 内的插话正文是否可作意图证据 | 本层归 `CONTROL_SIGNAL`；M2 规格再决定是否剥壳，剥壳规则须是结构性的 |
| D3 | 是否实现 `SourceAnnotationProjection`/`SourceResolver` | 缓做（§5）；M2 v1 不消费 domain_meta；§8.3 硬门措辞同步修订 |
| D4 | 是否把 M1D run 作为必填输入 | 否。可选；但提供即必须全部可解析（§2.1），不是"能填则填" |
| D5 | 白名单的初始成员 | v0.3 依 §0 探针与 §2.3 准入三规则冻结：A 7 个、B 2 个、C 3 个；`session_context_files` 依 ①、`skill`/`task`/`image` 依 ② 不准入；白名单 D 与 `ATTACHMENT_MARKER` 删除 |
| D10（v0.3） | `skill`（41 次、全 bare、40 前缀 / 1 观测）是否准入 B | **暂不**。它是单一通用英文单词，本规格目前不能引用其注入协议出处；安全性质不受影响（`UNKNOWN_TAGGED` 本就不作意图证据），代价只是 M2 v1 少 41 条能力暴露证据。登记为首个白名单扩展候选：一旦在 Control 侧核实其注入格式出处，经规格修订准入 |
| D11（v0.3） | 全空白正文归 `PLAIN_USER_TEXT` 还是单列 | 单列 `EMPTY_TEXT`（R01 有 4 条生产者），使三个分母精确表示"有非空普通文本" |
| D6（v0.2） | `QUARANTINED` 的替代 | `content_form` 透传 + `NO_LEADING_TEXT`；reader 失败抛异常，不设枚举值 |
| D7（v0.2） | validator 是否复刻分类函数以求"独立" | 否。共用 `classify_leading_text`；独立性来自 §3 正交不变量层（与 M1D D-g 同理） |
| D8（v0.2） | 包名、契约版本、CLI | `source_projection/`、`user-text-projection-v1`、`source-projection build` |
| D9（v0.2） | 行级 ID | 不造新 ID，以 `event_occurrence_id` 键控；run ID 绑定见 §4 |

---

## 8. 只读结构探针固定程序（Control 侧，冻结前与白名单扩展评审时执行）

目的：为 §0、§2.3 白名单准入与 §6 验收向量提供数据；**不是**生产代码，不入仓，不读正文语义，输出只含标签名与整数。

输入：已发布并通过 `validate_compiled_run` 的 M1B run 的 `private/event_occurrences.jsonl`。

逐行只取 `event_kind = USER` 的事件，按 §2.3 的 `content_form` 取法得到 `head`（去空白、截 256 码点），用 `LEADING_TAG` 文法判定，统计并输出：

1. `event_kind × event_scope` 计数（全部事件，作为与 M1B 报告的一致性核对）；
2. USER 的 `content.kind` 与 `content_form` 计数；`OBSERVED` 且 `request_boundary_id = null`、`PREFIX` 且非 null 的计数（应恒为 0）；
3. `event_scope × 粗类别` 计数，粗类别 ∈ {PLAIN, TAGGED, LT_NOT_TAG（`<` 开头但不匹配文法，按下一字符类别细分）, CLOSING_TAG_FIRST, EMPTY_OR_WHITESPACE, NO_LEADING_TEXT}——其中 PLAIN/LT_NOT_TAG/CLOSING_TAG_FIRST/EMPTY_OR_WHITESPACE 在契约中同属 `PLAIN_USER_TEXT`，探针拆开只为核对文法边界；PLAIN 中前置 BOM/零宽字符计数；
4. 开标签名频表：`标签名 × (前缀计数, 观测计数) × 形态(bare/attrs/selfclose)`，只列出现 ≥ 2 次者的名字，出现 1 次的只报"不同标签名个数"（单例标签名可能是用户自造文本，不落盘）；
5. 三个分母（§2.4 门④）。

结果以表格形式更新 §0 并作为 §6 验收向量；探针脚本本身放在仓外临时目录，用后删除。v0.3 已按此程序在 v4 run 上执行（2026-09-03），结果见 §0 与 §6；白名单扩展评审（如 D10）时重复执行。
