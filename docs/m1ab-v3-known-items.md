# M1A/M1B v3 已知项登记

版本：v1.0

日期：2026-09-01

状态：M1A、M1B v3 正式通过前提下的已知项登记；两项均**已知并接受**，不在当前会话修改冻结代码

本文登记对抗审计在 `trajectory-compiler-m1ab-v3` 冻结代码上发现、但决定**带来源接受并暂不修改**的两个点（R1、R4）。它不撤销 [`r01-m1-v3-validation.md`](r01-m1-v3-validation.md) 的正式结论，也不新增任何代码。开发纪律只引用 [`../AGENTS.md`](../AGENTS.md)；M1 契约以 [`r01-processing-spec.md`](r01-processing-spec.md) 为准。

登记原因：这两点都不改变冻结 R01 的已发布字节，也未使任一验收门变红。R1 在 R01 上**根本未触发**；R4 是规格已命名的枚举与 oracle 已接受的设计张力。为遵守「绝不改动冻结 M1B」的约束，本会话只登记、不修法，未来任何收紧都必须走单独的 reopen 与重新验收。

## 1. 登记条目一览

| 编号 | 类别 | 代码定位 | 是否在 R01 触发 | 严重度 | 处置 |
| --- | --- | --- | --- | --- | --- |
| R1 | 隐私脱敏 fail-open（潜伏） | `src/traceforge/trajectory/privacy.py:374-393` | 否（产物 0 base64、validator `ok=true`） | 概念高 / 实测零影响 | 接受并登记，修法留待非冻结窗口 |
| R4 | 截断轴把源自报先验渲染成观测 | `src/traceforge/trajectory/compiler.py:1112-1121` | 是（全量 1,683，35 条 `OBSERVED_TRUNCATED`，0 `UNKNOWN`） | 设计张力 / 非泄漏非字节错误 | 作为规格批准的设计选择接受并登记张力 |

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
