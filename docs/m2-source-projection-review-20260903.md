# M2 前置来源投影规格 v0.1 评审意见（2026-09-03）

状态：只读评审，对象为 [`m2-source-projection-spec.md`](m2-source-projection-spec.md) v0.1。对照代码为 HEAD `6a7475e`（M1B v4 `8f6f65c`、M1D `1c588be`）的 `trajectory/contracts.py`、`event_payload.py`、`privacy.py`、`validation.py` 与 `query_turns/`；对照数据为已验收 M1B v4 run `6be45e01…` 与 M1D run `84d826b3…` 的已发布聚合计数。本文不写代码；采纳项已同步进规格 v0.2，未决项在 §4 列为冻结前置。

## 1. 确认正确

- **定位正确**：把"USER 事件的结构分类"放在一个独立、只消费已发布 M1B run 的投影层，而不是回头改已验收的 M1D。这与 M1D 门②（不推断前缀内是真实 query 还是 Harness 注入）互补而不冲突，也符合 OCP：M1D 冻结，扩展在新层。
- **信任边界同构**：先 `validate_compiled_run` 再消费、只引用 `event_occurrence_id` 不复制正文、Control 侧 `private/` + 仅聚合计数的 `reports/`，与 M1C/M1D 一致。
- **fail-closed 默认落点**：`UNKNOWN_TAGGED` 作为白名单外标签的兜底并进入公共报告，是正确的"显式不确定性"处理；白名单只能经规格修订扩展、代码不得自动学习，符合"封闭枚举"原则。
- **门④分母诚实**：要求 M2 任何分布陈述引用"普通文本全在前缀的 capture 数"，直面 M1D 规格 §0 的事实（92% USER 事件在不可定位前缀，仅 342 capture 有根）。
- **D1 不细分 `PLAIN_USER_TEXT`**、**D2 `<user_interjection>` 归 `CONTROL_SIGNAL`**、**D4 M1D run 可选**：三项建议均成立，采纳。
- **D3 缓做 `SourceAnnotationProjection`** 的理由 2（上游"任务摘要"是另一模型产物，作先验会破坏 M2 两次独立提取的独立性，应留作事后对照 oracle）是原则性理由，与 R01 无关，采纳；理由 1/3/4 是 R01 特例，不能作为通用依据（见 P4）。

## 2. 需要修正

| 编号 | 问题 | 定位 | 处置（已写入 v0.2） |
| --- | --- | --- | --- |
| P1 | **`QUARANTINED` 类别无生产者**。规格 §2.3 把它定义为"`content.value` 非字符串，或 typed reader 无法读出 `TextContent`"。但 (a) M1B validator 对每个事件都跑 `read_event_payload`（`trajectory/validation.py:975`），先校验后消费的 run 上 reader 失败不可达，应是 tripwire 异常而非枚举值；(b) `TextContent.value` 合法地可以是 dict——隐私 envelope `DATA_URL_SUMMARY_V1`（整段是 Data URL）或 `TEXT_WITH_DATA_URL_SEGMENTS_V1`（文本与 Data URL 分段，`privacy.py:347-371`），§0 的"非字符串 2 条"就是这种合法形态，不是结构异常；(c) 契约允许 USER `content.kind=CONTENT_BLOCKS`（`event_payload.py:282`），R01 恰为 0 不等于契约不允许 | 规格 §0、§2.3 | 删除 `QUARANTINED`。新增透传字段 `content_form ∈ {TEXT_STRING, TEXT_WITH_DATA_URL_SEGMENTS, DATA_URL_SUMMARY, CONTENT_BLOCKS}`（直接来自 typed reader 与 envelope kind）；`text_class` 新增 `NO_LEADING_TEXT`，用于 `DATA_URL_SUMMARY`、首段为 Data URL 摘要的分段 envelope、以及 `CONTENT_BLOCKS`；分段 envelope 首段为 `TEXT_SEGMENT_V1` 时按该段开头判定。原则：**枚举覆盖输入契约的全部合法形态，而不是 R01 的实测形态；契约之外的形态（reader 失败）不设枚举值，抛异常 fail-closed** |
| P2 | **开头标签文法未冻结**。"开头是 `<tag` 形态"没有定义前置空白集合、标签名字符集、终止符、大小写、属性与自闭合、`</x>` 开头、以及最多读多少字符。文法不冻结，validator 的"独立重算一致"就没有对象 | 规格 §2.3 | 冻结为契约常量：`^[空白]*<([A-Za-z_][A-Za-z0-9_.:-]*)(?=[空白>/])`，空白取 Python `str.isspace()`，标签名大小写敏感、白名单精确匹配，允许属性与自闭合（`<task id=..>`、`<x/>`），判定只读 `lstrip` 后前 256 个码点。`<` 后不满足文法者（`</x>`、`<3`、`<<`、`<-`）一律 `PLAIN_USER_TEXT`：该类的定义是"开头不是一个可识别的开标签"，不是"不含 `<`" |
| P3 | **`leading_tag` 内容安全**。§2.2 已限定只在白名单类别填标签名，但未对 `UNKNOWN_TAGGED` 显式写 `null`。白名单外的标签名可能是用户自造文本，一旦写入产物或报告就违反门③/门⑤ | 规格 §2.2、§2.4、§3 | 显式规定 `UNKNOWN_TAGGED ⇒ leading_tag = null`，validator 断言 `leading_tag ≠ null ⇔ text_class ∈ {HARNESS_CONTEXT, HARNESS_CAPABILITY, CONTROL_SIGNAL, ATTACHMENT_MARKER}` 且 `leading_tag ∈ 白名单`。由此白名单扩展评审**不能**依赖产物，只能由 Control 侧离线探针（只输出标签名与计数）完成，写成规格 §8 固定程序 |
| P4 | **与 [`r01-processing-spec.md`](r01-processing-spec.md) §8.2/§8.3 漂移**。§8.2 写"M1D 先确定性区分真实 query、附件上下文、Harness 包装、系统注入、工具反馈和中断控制"，而已验收的 M1D v0.3 明确不做（门②），这项工作实际落在本层；§8.3 把"`SourceAnnotationProjection` 或只读 `SourceResolver`"写成进入 M2 的硬门，与 D3 缓做直接冲突 | `r01-processing-spec.md` §8.2、§8.3 | 修订 §8.2：M1D 只产结构；USER 事件的结构分类由 M2 前置 `UserTextProjection` 承担。修订 §8.3：硬门改为"进入 M2 前必须先冻结并验收 `UserTextProjection`；M2 首次消费 `domain_meta` 之前必须先冻结并验收 `SourceAnnotationProjection`/`SourceResolver`"。"M2 不得绕过 M1 artifact 私下重解析原始 JSONL"保持不变 |
| P5 | **M1D 回指的可选性写得过宽**。§2.1 写"有则填，无则空；不因缺失而失败"。但 M1D 规格 §8 的分区守恒保证：观测窗口内每个 USER 事件恰属一个 `UserBlock.event_ids`。所以只要提供了 M1D run，`OBSERVED` 事件的 `user_block_id` 就**必须**可解析，解析不到是契约违反，不是"可空" | 规格 §2.1、§3 | 改为：未提供 M1D run 时全部 `user_block_id = null`；提供时 validator 断言 `locality=OBSERVED ⇒ user_block_id ≠ null` 且反查 `UserBlock.event_ids` 包含该事件、`PREFIX_UNLOCALIZED ⇒ user_block_id = null`。`projection_manifest` 记录 `m1d_run_id + 其 artifact_manifest SHA-256`（有则绑定），run ID 随之变化 |
| P6 | **§0 地基基于 v3 run 且不完整**。§0 探针跑在 `519a86d3…`（v3）上，标签频表只列了部分成员（"等"、"约 400"、"约 95"），D5 需要完整频表才能决定 `task`、`irc`、`image` 等低频项 | 规格 §0、§7 D5 | 冻结前在 v4 run `6be45e01…` 上重跑只读结构探针（§8 程序），产出完整"标签名 × 前缀/观测 × 计数"表，更新 §0；本评审会话因执行审批渠道不可用未能完成该探针，列为 v0.2 冻结前置（§4） |
| P7 | **门④分母缺一项**。两个分母（含普通文本的 capture 数、普通文本全在前缀的 capture 数）之外，M2 有根意图的真实分母是"观测窗口内至少含一条 `PLAIN_USER_TEXT` 的 capture 数"，且提供 M1D run 时它必须 ≤ 有 `UserBlock` 的 capture 数（R01 为 342），这是一条跨模块可复算的不变量 | 规格 §2.4 门④、§3 | 报告增加第三个分母 `captures_with_observed_plain_user_text`，validator 在提供 M1D run 时断言其 ≤ M1D `user_blocks.jsonl` 的 capture 去重数 |
| P8 | **白名单准入规则缺失**。D5 只问"低频项是否入选"，没有给准入判据；`task`、`image` 这类通用英文名词可能是用户自写标记，误收会把用户文本判成 Harness | 规格 §2.3、§7 D5 | 准入三条同时满足：① 在 v4 探针中出现 ≥ 2 次；② 标签名是 Harness 专有词汇（含下划线/连字符复合词或产品专名，如 `environment_context`、`in-app-browser-context`、`codex_delegation`），通用单词名词（`task`、`image`、`file`）不准入；③ 语义可由标签名自证。不满足者留在 `UNKNOWN_TAGGED`，计数进公共报告，随 M2 需求再议 |

## 3. 需要评审决定（本会话作为项目负责人的决定）

| 编号 | 问题 | 决定 |
| --- | --- | --- |
| D3 | 是否实现 `SourceAnnotationProjection` | **缓做**，但把理由收敛为原则性理由（独立性），并按 P4 修订 §8.3，使"缓做"不再违反硬门 |
| D5 | 白名单初始成员 | 按 P8 准入规则，**在 v4 探针结果出来后**定；v0.2 只写规则与 v0.1 已列成员的暂定归类，最终成员表在 v0.3 冻结 |
| D6（新） | `QUARANTINED` 的替代 | 按 P1：`content_form` + `NO_LEADING_TEXT`；reader 失败抛异常 |
| D7（新） | validator 与实现是否共用分类函数 | **共用**同一纯函数 `classify_leading_text(head) -> (text_class, leading_tag)`（DRY，规则只一个定义），独立性由不经该函数的正交不变量层提供：USER 事件 bijection、`locality ⇔ event_scope`、`request_boundary_id` 与 M1B 一致、`leading_tag` 白名单闭合与类别一致性（P3）、`content_form` 与 typed reader 一致、三个分母守恒、M1D 回指一致（P5）。与 M1D v0.3 §8 两层信任边界同构 |
| D8（新） | 包与契约命名 | 包 `src/traceforge/source_projection/`（容纳 `UserTextProjection`，日后 `SourceAnnotationProjection` 可加入而不改名），契约版本 `user-text-projection-v1`，CLI `source-projection build --m1b-run … [--m1d-run …] --output …`，脚本 `scripts/validate_user_text_projection_run.py` |
| D9（新） | 行级 ID | 不造新 ID：`user_text_annotations.jsonl` 以 `event_occurrence_id` 键控（bijection 使其天然唯一）；run ID = `stable_id(contract_version, m1b_run_id, m1b_manifest_sha256, m1d_run_id?, m1d_manifest_sha256?)` |

## 4. 冻结前置（v0.2 → v0.3 冻结的必做项）

1. 在 v4 run `6be45e01…` 上执行规格 §8 的只读结构探针，产出完整标签频表与 P1 所述 `content_form` 分布、`<` 后非标签的形态分布、前置 BOM/零宽字符计数，更新 §0；探针只输出标签名与整数，不输出正文。
2. 依 P8 规则定白名单 A–D 成员，写入 §2.3；`UNKNOWN_TAGGED` 预期计数一并记录为验收向量。
3. 用探针复算三个分母，作为验收向量写入 §6。
4. 修订 `r01-processing-spec.md` §8.2/§8.3（P4）。
5. 之后按 [`../AGENTS.md`](../AGENTS.md) 顺序：冻结规格 → 先写正常+失败测试 → 最小实现 → 独立 validator → 在 `6be45e01…`（+ `84d826b3…` 作可选输入）上双跑验收 → `docs/r01-user-text-projection-validation.md`。

## 5. 明确不做

- 不在 M1D 内加分类字段（M1D 已验收冻结；扩展在本层）。
- 不在产物或报告中出现任何白名单外标签名或正文切片。
- 不做 `<user_interjection>` 剥壳、不做 `PLAIN_USER_TEXT` 细分、不做前缀内部结构推断。
- 不因 R01 全部 USER 为 `TEXT` 而在契约里省略 `CONTENT_BLOCKS` 形态。
