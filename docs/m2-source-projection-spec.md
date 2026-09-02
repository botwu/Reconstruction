# M2 前置：来源投影处理规格（草案 v0.1，待评审）

版本：v0.1（规格评审稿，未实现）

日期：2026-09-02

状态：**规格评审中**。本文兑现 [`r01-processing-spec.md`](r01-processing-spec.md) §8.3 "进入 M2 前必须先提供最小、带来源的 `SourceAnnotationProjection` 或只读 `SourceResolver`" 这一硬门。只定义 I/O 与确定性算法，批准前不写代码，不在 M1B/M1C/M1D 预埋任何结构（[`../AGENTS.md`](../AGENTS.md) §1 YAGNI）。

阅读前置：[`../AGENTS.md`](../AGENTS.md)、[`background-and-goals.md`](background-and-goals.md) §3/§5、[`overall-plan.md`](overall-plan.md) §4.6/§5/§6、[`r01-processing-spec.md`](r01-processing-spec.md) §8.2/§8.3、[`m1d-processing-spec.md`](m1d-processing-spec.md) §0/§3。

---

## 0. 实测地基（人工 sanity 观测，不写进代码）

对冻结 M1B run `519a86d3…e06d1d` 的已发布 `private/event_occurrences.jsonl` 做纯结构探针（只看 `event_kind`、`event_scope`、`payload.content.kind` 与正文**开头标签名**，不读正文语义）：

| 观察 | 数值 | 含义 |
| --- | ---: | --- |
| USER 事件总数 | 14,407 | 全部 `content.kind=TEXT` |
| 其中落在 `PRE_FIRST_OBSERVED_TERMINAL` 前缀 | 13,225（91.8%） | M1D 规格 §0 已确认：前缀不可定位到 request boundary，不成回合 |
| 开头无标签的普通文本 | 11,696 | 真实用户 query 的候选主体 |
| 开头为 `<environment_context>` | 2,222 | Harness 注入的环境上下文，伪装成 user 消息 |
| 开头为 `<in-app-browser-context>` / `<system-reminder>` / `<skill>` / `<recommended_plugins>` / `<codex_delegation>` 等 | 约 400 | Harness 包装、系统注入、插件与委派上下文 |
| 开头为 `<turn_aborted>` / `<user_interjection>` / `<subagent_notification>` | 约 95 | 中断与控制信号 |
| `content.value` 非字符串 | 2 | 结构异常，只能 `QUARANTINED` |
| **至少含一条普通用户文本的 capture** | **1,680 / 1,683** | 意图分母可以覆盖几乎全部 capture，但其中约 80% 的文本只存在于不可定位前缀 |

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

一个已发布 M1B run（先调 `validate_compiled_run` 校验再消费，同 M1C/M1D 信任边界）。只消费 `EventOccurrenceV2` 的 `event_occurrence_id`、`capture_occurrence_id`、`sequence_number`、`event_kind`、`event_scope`、`request_boundary_id`，以及经 `event_payload.py` typed reader 读出的 `TextContent`（`kind`、`utf8_byte_length`、`value` 的**开头片段**）。可选次输入：已发布 M1D run，用于把 `OBSERVED_REQUEST_WINDOW` 内的 USER 事件回指其 `user_block_id`/`query_turn_id`（有则填，无则空；不因缺失而失败）。

### 2.2 输出 `UserTextAnnotationV1`（每个 USER 事件一条）

```yaml
schema_version:
event_occurrence_id:        # 引用 M1B，不复制正文
capture_occurrence_id:
locality:                   # PREFIX_UNLOCALIZED | OBSERVED
request_boundary_id:        # OBSERVED 时非空；PREFIX_UNLOCALIZED 恒空
user_block_id:              # 可空；仅在提供 M1D run 且能回指时填
text_class:                 # 见 2.3 封闭枚举
leading_tag:                # text_class 为 HARNESS_* / CONTROL_SIGNAL 时的标签名（白名单内），否则空
utf8_byte_length:           # 透传 TextContent.utf8_byte_length
```

### 2.3 `text_class` 封闭枚举与判定（纯结构，只看开头）

```text
PLAIN_USER_TEXT       正文开头不是 "<" 起始的标签（允许前置空白）
HARNESS_CONTEXT       开头标签 ∈ 冻结白名单 A：environment_context, in-app-browser-context,
                      system-reminder, system-conventions, codex_internal_context,
                      session_context_files, local-command-caveat, available-deferred-tools
HARNESS_CAPABILITY    开头标签 ∈ 冻结白名单 B：skill, recommended_plugins, codex_delegation, task
CONTROL_SIGNAL        开头标签 ∈ 冻结白名单 C：turn_aborted, user_interjection, subagent_notification
ATTACHMENT_MARKER     开头标签 ∈ 冻结白名单 D：image
UNKNOWN_TAGGED        开头是 "<tag" 形态但 tag 不在 A–D 任一白名单
QUARANTINED           content.value 非字符串，或 typed reader 无法读出 TextContent
```

规则：

- 只看正文开头的一个标签名，不解析 XML、不看闭合、不读标签内容；白名单标签名写进契约常量，**新标签只能通过修订本规格进入白名单**，代码不得自动学习；
- `PLAIN_USER_TEXT` 不再细分（D1）。它仍可能含有 Harness 拼接文本，这是 M2 语义层的问题，本层不猜；
- `UNKNOWN_TAGGED` 是 fail-closed 的默认落点，validator 断言其计数进入公共报告，供评审决定是否扩白名单；
- `<user_interjection>` 归 `CONTROL_SIGNAL` 而非 `PLAIN_USER_TEXT`：它是 Harness 对用户插话的包装，实际插话正文是否可用留 M2（D2）。

### 2.4 硬门

- **门① 纯确定性、零 LLM**，逐字节可复现；
- **门② 不推断前缀结构。** `PREFIX_UNLOCALIZED` 事件之间不建任何顺序、归属或回合关系；本层只逐事件注解；
- **门③ 只引用不复制。** 产物不含正文、不含 `value` 的任何切片；`leading_tag` 只允许白名单内的标签名字面量；
- **门④ 分母诚实。** 公共报告按 `(locality, text_class)` 给出计数，并给出"至少含一条 `PLAIN_USER_TEXT` 的 capture 数"与"其 `PLAIN_USER_TEXT` 全部位于前缀的 capture 数"两个分母，M2 任何分布陈述必须引用后者说明可定位性缺口；
- **门⑤ Public/Control 隔离。** 全部产物属 Control 侧 `private/`；`reports/` 仅聚合计数；控制文件闭合值域，沿用 M1C 两层隐私与 `_scan_control_pathlike` known-item。

### 2.5 M2 消费约定（写入本规格，作为 M2 规格的前置约束）

- M2 语义提取只允许把 `text_class=PLAIN_USER_TEXT` 的事件作为**任务意图证据**；`HARNESS_*` 事件可作为**环境暴露证据**（system/harness fingerprint、工具可用性）但不得进入意图；`CONTROL_SIGNAL` 只作为中断/取消线索；
- 由 `PREFIX_UNLOCALIZED` 证据得出的 Episode 或意图必须携带 `intent_locality=PREFIX_ONLY`，进入 `ObservedTaskDistribution` 时单列，不与有根 Episode 混算；
- M2 不得因为前缀证据无法绑定 boundary 而回退到"按绝对路径重解析原始 JSONL 找上下文"。

---

## 3. 独立 validator

镜像 M1C 模板，双参 `<projection_run> <m1b_run>`（M1D run 可选第三参）。独立复算并断言：输入绑定与完整性；**USER 事件双向 bijection**（M1B 每个 USER 事件恰有一条注解，无遗漏无幻影）；`locality` 与 `event_scope` 一致、`request_boundary_id` 与 M1B 一致；`text_class`/`leading_tag` 由开头片段独立重算一致，`leading_tag` 恒在白名单内；报告计数与两个分母重算一致；`private/` 无正文切片（闭合值域：稳定 ID、枚举、白名单标签名、整数）。

---

## 4. 输出布局

```text
<projection_artifact_root>/<content_addressed_run_id>/
├── projection_manifest.json        # 绑定 m1b_run_id + artifact_manifest SHA-256（非路径），可选 m1d_run_id
├── private/
│   └── user_text_annotations.jsonl
├── reports/
│   └── projection_report.json      # 仅聚合计数与两个分母
├── artifact_manifest.json
└── run_receipt.json
```

---

## 5. SourceAnnotationProjection（domain_meta 先验）——建议缓做

`domain_meta` 含上游 rubric、任务摘要、风险与输入审计。要投影它们必须以只读 `SourceResolver` 重新读取冻结 JSONL：按 `source_records.jsonl` 的 `dataset_sha256 + line_number + byte_offset + line_sha256` 定位物理行、重算 `domain_meta_sha256` 与 `captures.jsonl` 比对，再只输出经审核的白名单字段，每个字段标记 `SOURCE_PRIOR`。

**建议 D3：M2 v1 不消费 domain_meta，本投影缓做。** 理由：

1. R01 全部 1,683 条的主 rubric 相同（信息检索与证据综合），rubric 对 R01 内部分布没有区分力；
2. 上游"任务摘要"是另一模型生成的标签。M2 若把它作为输入，两次独立语义提取就不再独立；它更适合在 M2 完成后作为**外部对照 oracle** 计算一致率，而不是作为先验；
3. 缓做可以让 M2 不依赖原始 JSONL 在机器上完整存在（当前验收机器上的 R01 副本不完整，见 [`r01-m1c-validation.md`](r01-m1c-validation.md) §7）；
4. 风险/输入审计字段中唯一已被 M1B 消费的 `input_truncated` 已有 `input_truncation_status` 轴（含 [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md) R4 张力），无需重复投影。

若评审坚持 M2 需要风险标签做隐私门控，则只投影 `risk` 白名单字段，仍不投影任务摘要。

---

## 6. 完成条件

- USER 事件全覆盖 bijection；`locality`/`text_class`/`leading_tag` 独立复算一致；`UNKNOWN_TAGGED` 与 `QUARANTINED` 计数进入公共报告；
- 两个分母（含普通文本的 capture 数、普通文本全在前缀的 capture 数）可独立重算；
- 纯结构、零 LLM、逐字节确定；两次独立构建一致；两层隐私通过；
- 单元测试含：普通文本、每类白名单标签、未知标签、非字符串 value、前置空白、可选 M1D 回指有/无；
- 无 R01 硬编码常量（§0 数字不入代码）、无语义占位、无 M2 预埋。

---

## 7. 待评审决策点

| 编号 | 问题 | 建议 |
| --- | --- | --- |
| D1 | `PLAIN_USER_TEXT` 是否再按长度/语言/是否含 URL 细分 | 否。任何细分都需读正文语义，留 M2 |
| D2 | `<user_interjection>` 内的插话正文是否可作意图证据 | 本层归 `CONTROL_SIGNAL`；M2 规格再决定是否剥壳，剥壳规则须是结构性的 |
| D3 | 是否实现 `SourceAnnotationProjection`/`SourceResolver` | 缓做（§5）；M2 v1 不消费 domain_meta |
| D4 | 是否把 M1D run 作为必填输入 | 否。可选；缺失时 `user_block_id` 为空，不阻塞 |
| D5 | 白名单 A–D 的初始成员 | 采用 §0 探针中出现且语义可由标签名自证的标签；`<task>`、`<irc>`、`<image>` 等低频项是否入选由评审定 |
