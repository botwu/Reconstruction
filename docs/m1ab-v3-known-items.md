# M1A/M1B v3 已知项登记

版本：v1.2

日期：2026-09-01（v1.0：R1、R4）；2026-09-02（v1.1：追加 R5–R8）；2026-09-03（v1.2：R4/R5/R7/R8 的修法已在 M1B v4 提交 `8f6f65c` 落地，待正式验收）

状态：M1A、M1B **v3** 正式通过前提下的已知项登记。R1 继续接受；R4/R5/R7/R8 的根因修法已作为 M1B v4 代码提交，但 v4 **尚未在完整 R01 上验收**，v3 结论在此之前继续有效

本文登记对抗审计在 `trajectory-compiler-m1ab-v3` 冻结代码上发现、但决定**带来源接受并暂不修改**的两个点（R1、R4）。它不撤销 [`r01-m1-v3-validation.md`](r01-m1-v3-validation.md) 的正式结论，也不新增任何代码。开发纪律只引用 [`../AGENTS.md`](../AGENTS.md)；M1 契约以 [`r01-processing-spec.md`](r01-processing-spec.md) 为准。

登记原因：这两点都不改变冻结 R01 的已发布字节，也未使任一验收门变红。R1 在 R01 上**根本未触发**；R4 是规格已命名的枚举与 oracle 已接受的设计张力。为遵守「绝不改动冻结 M1B」的约束，本会话只登记、不修法，未来任何收紧都必须走单独的 reopen 与重新验收。

## 1. 登记条目一览

| 编号 | 类别 | 代码定位 | 是否在 R01 触发 | 严重度 | 处置 |
| --- | --- | --- | --- | --- | --- |
| R1 | 隐私脱敏 fail-open（潜伏） | `src/traceforge/trajectory/privacy.py:374-393` | 否（产物 0 base64、validator `ok=true`） | 概念高 / 实测零影响 | 接受并登记，修法留待非冻结窗口 |
| R4 | 截断轴把源自报先验渲染成观测 | `src/traceforge/trajectory/compiler.py:1112-1121` | 是（全量 1,683，35 条 `OBSERVED_TRUNCATED`，0 `UNKNOWN`） | 设计张力 / 非泄漏非字节错误 | **v4 已修（待验收）**：枚举值改为 `SOURCE_REPORTS_*`，reason code 与 attrition 键同步改名 |
| R5 | validator 严格匹配谓词与 compiler 平行实现、非同一谓词 | `compiler.py:849-857` vs `validation.py:1194` | 否（R01 两谓词结果一致） | 维护风险 / 当前零影响 | **v4 已修（待验收）**：抽为 `contracts.is_strict_one_to_one_match`，compiler/validator 共用 |
| R6 | validator 验证范围是 artifact 自洽，不是从 source 重派生 | `validation.py:794-801`、`1962-1965`、`1681-1682`、`1799-1803` | 是（范围限制，恒成立） | 主张边界 / 非缺陷 | 登记为 validator 的明确能力边界 |
| R7 | `ProcessingStatus.PARTIAL` 全仓库无生产者 | `contracts.py:26`；`compiler.py:257` 恒写 `COMPLETE` | 是（1,683 全 `COMPLETE`，0 `PARTIAL`） | YAGNI 违反 / 下游死分支 | **v4 已修（待验收）**：删除 `PARTIAL` 与 `processing_partial_count`；三态 eligibility 明确归 M2 |
| R8 | `visible_payload_utf8_byte_length` 名不符实 | `compiler.py:917` | 是（恒为 JSON 外壳长度） | 可用性陷阱 / 已致 M1D 规格勘误 | **v4 已修（待验收）**：改名为 `visible_payload_envelope_utf8_byte_length`，EventOccurrence 升 v3 |

## 2. R1：脱敏续行判定只认 CR/LF

### 2.1 现象

`_consume_base64_payload`（`privacy.py:374-393`）在消费 `;base64,` 之后的 payload 时，遇到非 base64 字符会尝试「跨空白续行」。但续行分支的入口判定是：

```text
if value[cursor] in "\r\n":
```

即只有回车或换行才触发跨空白续行；随后的 `.isspace()` 才连吃空格、制表符等其余空白。因此当 base64 正文被**裸空格或制表符**（前面没有 CR/LF）分隔时——例如 `...;base64,AAAA BBBB`——扫描在该空格处直接 `break`，只把空格前的 `AAAA` 归入 Data URL 摘要 envelope，空格后的 `BBBB` 会作为普通文本段继续存活在可见 payload 中，未被折叠进摘要。

对一个以 fail-closed 为目标的脱敏器而言，这是一个 **fail-open 缺口**：只要出现 `;base64,` 头，其后全部 base64 形态内容都应无差别地收进摘要，无论换行风格如何。当前实现只对 CR/LF 折叠的 payload 闭合，对裸空格/制表符折叠的 payload 会漏掉尾段。

### 2.2 为什么可以接受并登记

- **R01 未触发**：本会话现场重编译产出的产物与冻结官方 run 逐字节一致，独立 validator 隐私轴 `ok=true`，全量派生产物 0 条 base64 残留。R01 的 Data URL 要么未折叠、要么按 CR/LF 折叠，不含裸空格/制表符分隔的 base64。
- **不改变已发布结果**：登记 R1 不触碰任何冻结字节，也不改动任一 v3 结论。
- **约束优先**：当前会话的硬约束是「绝不改动冻结 M1B」；R1 的修法属于 M1B 代码变更，必须留到单独的非冻结窗口并重新验收。

### 2.3 未来修法与回归用例草案（留待非冻结窗口，不在本会话执行）

- **修法方向**：将 `privacy.py:385` 的续行入口判定由 `value[cursor] in "\r\n"` 收紧为 `value[cursor].isspace()`（覆盖空格、制表符、换页、CR、LF），使续行对全部空白风格统一 fail-closed；或等价地在 `_base64_unit_end` 之外统一处理内嵌空白。
- **回归用例草案**：
  1. `data:image/png;base64,AAAA BBBB`（裸空格分隔）——断言整段 base64 折叠进单一摘要 envelope，可见 payload 无残留 `BBBB`；
  2. 制表符 / 换页分隔的同构用例；
  3. 保留既有 CR/LF 折叠用例，确认收紧不回归；
  4. 与产物级「0 base64 残留」不变量联动断言。
- 该修法与回归用例必须与其余 M1B 变更一起走两遍全量重编译、独立验证与对抗回归，再另行发布结论。

## 3. R4：输入截断轴把源自报先验渲染成观测

### 3.1 现象

`_input_truncation_status`（`compiler.py:1112-1121`）直接读取 `domain_meta.input_audit.input_truncated`：

```text
input_truncated is True  → OBSERVED_TRUNCATED
input_truncated is False → OBSERVED_NOT_TRUNCATED
其余 / 缺失            → UNKNOWN
```

`input_truncated` 是上游采集层写入的**自报布尔先验**。当它为 `False` 时，编译器把它渲染为枚举 `OBSERVED_NOT_TRUNCATED`——即把「来源声称未截断」表述成「观测到未截断」。这与 [`r01-processing-spec.md`](r01-processing-spec.md) §5.5 中「输入没有明确截断证据时必须为 `UNKNOWN`，不能伪报未截断」的语气存在张力：一个字段名带 `OBSERVED_` 的枚举值，其证据实际来自源自报而非独立观测。

在冻结 R01 上，该轴对全量 1,683 个 capture 均有终态，其中 35 条 `OBSERVED_TRUNCATED`、0 条 `UNKNOWN`（见 [`r01-m1-v3-validation.md`](r01-m1-v3-validation.md) 与 [`r01-processing-spec.md`](r01-processing-spec.md) §7 oracle）。

### 3.2 为什么作为规格批准的设计选择接受

- **规格已命名该枚举**：§5.5 明确把 `OBSERVED_TRUNCATED | OBSERVED_NOT_TRUNCATED | UNKNOWN` 列为 `input_truncation_status` 的合法取值。
- **oracle 已接受当前分布**：§7 外部 oracle 明确写入 `truncated input captures = 35`、`unknown input truncation = 0`，即当前把源自报 `False` 归入 `OBSERVED_NOT_TRUNCATED` 是被验收基线接受的既定行为。
- 因此 R4 不是「偷偷通过」的缺陷，而是规格已批准、oracle 已锁定的设计选择；本文只登记其**语义张力**，供下游读取该轴时保持警惕：`OBSERVED_NOT_TRUNCATED` 当前等价于「源自报未截断」，不等于「TraceForge 独立证实未截断」。

### 3.3 若未来收紧则须走 reopen（不在本会话执行）

如果后续判定「先验不得渲染成观测」应成为硬约束，则属于契约收紧，必须走单独 reopen：

- **可选修法方向**：源自报 `input_truncated=False` 在缺乏独立佐证（如可核对的输入长度护栏 / 观测到的截断标记）时降级为 `UNKNOWN`，仅当有独立证据时才给 `OBSERVED_NOT_TRUNCATED`；或引入独立的输入长度护栏作为佐证信号。
- **连带影响**：该修法会改变 §7 oracle 的 `unknown input truncation` 计数，必须同步更新规格 oracle、验收报告与全量重验，不能就地静默变更。
- 在未走 reopen、未重新冻结 oracle 前，维持当前 §5.5 / §7 已批准行为。

## 4. 与验收门的关系

R1、R4 均已纳入本会话对 8 门的现场闭合评估，未使任一门变红：

- **门 3 隐私安全**：R1 潜伏但 R01 未触发；现场重编译产物 0 base64、validator 隐私轴 `ok=true`。R1 登记为潜伏 fail-open。
- **门 2 结构忠实**：R4 为规格 §5.5 命名、§7 oracle 接受的设计选择；登记为语义张力，非结构不忠实。

8 门现场闭合的完整证据见 [`r01-m1-v3-validation.md`](r01-m1-v3-validation.md) 的本会话 live 复核记录。本登记不改变「M1A/M1B v3 正式通过」结论，也不解除进入 M1C/M2 前的各阶段硬门。

## 5. v1.1 追加条目（2026-09-02 代码级审计）

以下四项来自 M1C 验收后对 `trajectory/` 的只读代码审计。全部不改变冻结字节、不使任一验收门变红；处置均留待下一次契约 reopen。

### 5.1 R5：validator 严格匹配谓词是平行实现

compiler 的 `has_strict_match` 显式要求四条件（数量 1:1、`tool_name` 相等、result 不早于 call、`arguments_valid is True`，`compiler.py:849-857`）。validator 的对应判定是 `len(calls) == 1 and len(results) == 1 and not observed`（`validation.py:1194`），即"没有任何异常状态即匹配"。两者在当前数据上等价：`arguments_valid` 对 TOOL_CALL 恒为布尔，`None` 不可能出现；`NAME_MISMATCH`/`RESULT_BEFORE_CALL` 在 1:1 情形下与显式比较同义。但它们不是同一个谓词——若将来 compiler 新增一个不写进 `observed` 的匹配前置条件，validator 不会同步变严。这与 R01 处理规格 §7.1 "validator 独立重算"的立场一致（平行实现正是独立性的代价），登记为维护风险；收紧时应把严格匹配抽成单一规范谓词，双方引用同一组测试向量。

### 5.2 R6：validator 的验证范围是 artifact 自洽，不是从 source 重派生

validator 不读取原始 JSONL。因此以下事实只能校验其枚举合法性与内部一致性，不能证明与来源一致：`input_truncation_status`（来源 `domain_meta.input_audit` 不在任何已发布字段中，`validation.py:794-801`、`1962-1965`）、`compaction_status`（由 capture 自报的 `has_compaction`/`compaction_count`/`compaction_hashes` 推导，`1799-1803`）、`catalog_input_valid`（`1681-1682` 注释已承认）、`source_records` 的 `line_sha256`/`dataset_sha256`（只代入公式，不重算原始字节）。这不是缺陷，而是 validator 的能力边界：它能抵御"篡改单一自报字段"，不能抵御"有权改写整个 run 且同步改写全部自报字段"的攻击者。后者的防线是 `run_receipt` 的 Git provenance 与冻结 `dataset_sha256`。README 中"不信任自报语义"的表述应理解为"对可重算事实不信任自报"，本条把不可重算的清单显式化。

### 5.3 R7：`ProcessingStatus.PARTIAL` 无生产者

`contracts.py:26` 定义了 `PARTIAL`，但 `compiler.py:257` 成功路径恒写 `COMPLETE`，失败路径由 `pipeline.py:216` 写 `QUARANTINED`；全仓库没有任何代码产出 `PARTIAL`（R01 实测 1,683 全 `COMPLETE`）。`overall-plan.md` §5 描述的"每条输入最终只能处于 `USABLE_COMPLETE`、`USABLE_PARTIAL` 或 `QUARANTINED`"在 M1B 层实际只实现了两态。后果是下游 M1D 规格 §6 与其实现中的 `PARTIAL` 分支为死代码。与 M1C 删除 `RelationGrade.B` 同类：契约不应保留无生产者的成员。下一次 reopen 时二选一：删除该成员，或定义 compiler 何时产出 `PARTIAL`（例如 pairing 存在 `RESULT_NOT_OBSERVED` 但结构完整）。

### 5.4 R8：`visible_payload_utf8_byte_length` 是 JSON 外壳长度

`compiler.py:917` 写入的是去除 reasoning 后的可见 payload 经 canonical JSON 编码的字节数（含 `{"content":…}` 外壳），恒大于 0，不是正文长度。规格 §4.3 的描述是准确的，但字段名会误导下游把它当作"有无文本"的判据——M1D 规格 v0.2 §1 的勘误正是踩了这个坑。正文长度的权威来源是 typed reader 的 `TextContent.utf8_byte_length` / `ContentBlocks.block_count`。建议下一次 reopen 改名为 `visible_payload_envelope_utf8_byte_length`，或至少在 `EventOccurrenceV2` 的契约 docstring 写明。
