# M1C 已知项登记

版本：v1.1

日期：2026-09-02（v1.0 登记）；2026-09-03（v1.1：K1、K2 经 M1C v2 reopen 关闭，K3 改判）

状态：K1、K2 **已通过 M1C v2（提交 `fcff8cf`，[`r01-m1c-validation.md`](r01-m1c-validation.md) §0）从根因关闭**；K3 改判为非代码问题，不修。

本文登记 M1C v1 验收之后、对冻结 lineage run `19d0325c…aa359` 与已发布 M1B run `519a86d3…e06d1d` 做只读独立复算时发现的三个点，以及各自的处置结果。

## 1. 登记条目一览

| 编号 | 类别 | 定位 | 是否在 R01 触发 | 严重度 | 处置 |
| --- | --- | --- | --- | --- | --- |
| K1 | `IDENTICAL_RAW_REQUEST_HASH` 的 Grade-A 定级缺乏语义依据；门② 的实证依据由此反转 | v1：`m1c-processing-spec.md` §3 门②、§4.4、§5.3；`lineage/builder.py` | 是（19 组 / 47 capture / 41 边，17 边为该关系独有） | **高** | **已关闭（v2）**：删除该关系及门④，确立"上游不透明摘要不入证据"原则；门②改为预防性不变量 |
| K2 | 规格 §5.4 空集断言的论证是非推论 | `m1c-processing-spec.md` §5.4 | 结论为真（独立复算 0 组），理由不成立 | 低（文档） | **已关闭（v0.4）**：论证改为直接复算 |
| K3 | 桶内两两建边 O(K²) 无上限护栏 | `lineage/builder.py` 三处 `combinations` | 否（R01 最大桶 K=5） | 低 | **改判不修**：见 §4 |

## 2. K1：`raw_request_hash` 相等不蕴含同一请求

### 2.1 现象

规格 §5.3 把 `IDENTICAL_RAW_REQUEST_HASH` 列为 Grade-A"高可信显式关系"，建边条件只有"两个 capture 的 `raw_request_hash` 均满足 §7 格式契约且相等"。§3 门② 与 §4.4 进一步把"该关系有 5 组跨候选组"作为"Grade-A 必须全局组盲判定"的实证依据。

对 19 个发火组做独立复算（只读已发布 `captures.jsonl` 与 `event_occurrences.jsonl`，不调用 M1C 代码），组内各属性是否恒等：

| 属性 | 恒等组数 / 19 |
| --- | ---: |
| `message_count`、`model`、`source_request_count`、首个 SYSTEM 事件的 `visible_payload_sha256` | 19 |
| 不可定位前缀（`PRE_FIRST_OBSERVED_TERMINAL`）事件数 | 19 |
| 不可定位前缀的可见指纹链（即请求输入的实际内容） | **1** |
| 全 capture 可见指纹链 | **1** |
| `tool_catalog_id` | **0** |
| `usage` | 0 |
| `request_time_start` | 11 |

也就是说，同一 `raw_request_hash` 的 capture 拥有相同的消息条数、模型、system 提示和请求数，但**在 18/19 组里请求输入内容不同，在 19/19 组里工具目录不同**。5 个跨候选组的组分别来自 4/3/3/2/2 个不同的 account 与 thread，前缀事件数组内一致（152、51、8、128、141），内容互异。

TraceForge 不掌握该字段的原像定义；它是上游采集层写入的不透明值。能够确定的只有：**相等不蕴含同一请求，甚至不蕴含同一套工具**。从组内恒等的属性看，它更接近一个请求骨架或模板指纹。

### 2.2 后果

1. Grade-A 的定义是"高可信显式关系"（§5）。41 条边里 24 条与 `SHARED_SOURCE_REQUEST` 重合（这些 capture 本就共享请求身份），**17 条为该关系独有**——它们断言两个既不共享请求身份、也不共享内容的 capture 之间存在高可信 lineage。这 17 条边当前无法从已发布事实中得到支持。
2. 门② 的论证反转。规格用"IDENTICAL 有 5 组跨候选组"证明组盲是必要的；但这 5 组恰好是跨 account 的模板级碰撞。剔除后，R01 中真正跨候选组的 Grade-A 关系为 0（`SHARED_SOURCE_REQUEST` 0 例跨组、`EXPLICIT_REQUEST_SUCCESSOR` 是 capture 内关系、`COMPLETE_DUPLICATE_CAPTURE` 为 0）。组盲作为**预防性不变量**依然正确——不能因某个 cohort 的巧合收窄通用契约——但它在 R01 上没有实证支持，规格应如此陈述。
3. 这与 [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md) R4 属同一类问题：把上游自报的不透明值渲染成 TraceForge 自己的观测结论。

### 2.3 处置：M1C v2 删除该关系（2026-09-03）

三个可选方向（降级改名 / 加语义前置条件 / 删除）中，项目负责人选择**删除**：R01 上它独有的信息量为零（24 条重合边已由 SHARED 覆盖，17 条独有边不可信），保留任何形式都是在为一个原像未知的上游字段维护契约、代码与测试。提交 `fcff8cf` 同时删除关系枚举成员、`RawRequestHashStatus`、`classify_raw_request_hash`、报告 qualified/unknown 计数、reader 读取、builder/validator 分支与 issue code，净 −245 行；规格 v0.4 把"上游自报的不透明摘要只作元数据、不入任何关系证据"写为 §5 的证据来源原则，门②改为预防性不变量、其 e2e 实证改用共享 srid 的跨组合成 fixture。

v2 重新验收（[`r01-m1c-validation.md`](r01-m1c-validation.md) §0）确认：产物相对 v1 恰好只少 41 条 IDENTICAL 边，`request_nodes` 与 `request_successor_edges` 逐字节不变。

这一原则同样适用于 M1B 的 `input_truncation_status`（[`m1ab-v3-known-items.md`](m1ab-v3-known-items.md) R4），其修法归入 M1B v4 reopen。

### 2.5 复算方法（可重现，不入代码）

对已发布 `captures.jsonl` 按 64-hex `raw_request_hash` 分桶取 >1 的组；对每组比较 `message_count`、`model`、`tool_catalog_id`、`source_request_count`、`usage`、`request_time_start` 是否恒等；对 `event_occurrences.jsonl` 按 capture 收集 `(sequence_number, event_kind, visible_payload_sha256)`，分别对全链与 `event_scope=PRE_FIRST_OBSERVED_TERMINAL` 子链取 canonical 摘要比较；对 lineage run 的 `capture_relation_edges.jsonl` 统计 IDENTICAL 端点对与 SHARED 端点对的交集。

## 3. K2：§5.4 空集断言的论证（已关闭）

§5.4 曾写："R01 中 `target_hash` 全 1,683 唯一，该集合为空"，用以支撑"可见指纹链相等但 `source_request_id` 序列不同"的情形在 R01 不存在。`target_hash` 与 `raw_request_hash` 一样是上游不透明值，它的唯一性不能推出任何可重算派生属性的唯一性。结论本身经独立复算为真（可见指纹链相等的 capture 分组数 = 0）。规格 v0.4 已把论证替换为该直接复算。

## 4. K3：桶内两两建边缺上限（改判：不是代码问题）

两处 `combinations(sorted(bucket), 2)` 边数为 K(K−1)/2，R01 最大桶 K=5。v1.0 曾建议加显式上限并 fail-closed。复议后改判**不修**：一个被上千 capture 共享的 `source_request_id` 只可能来自上游把 system 模板或常量误写进请求 ID，那是 M1A 来源适配器的输入契约问题（`source_request_ids` 的值域），不该由 M1C 用一个任意常数护栏去兜。在没有任何 cohort 触发之前为它加常量、异常类、镜像检查与测试，违反 [`../AGENTS.md`](../AGENTS.md) §1 YAGNI（"性能优化必须基于测量结果"）。若将来某个 cohort 真的出现病态桶，正确的修法在 M1A 拒收，而非 M1C 截断。

## 5. 与验收门的关系

K1、K2 通过 M1C v2 reopen 从根因关闭并重新验收，当前有效结论以 [`r01-m1c-validation.md`](r01-m1c-validation.md) §0 为准。v2 产物中不存在 `IDENTICAL_RAW_REQUEST_HASH` 边，M2 无需再对其设限。
