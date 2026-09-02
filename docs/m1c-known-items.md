# M1C 已知项登记

版本：v1.0

日期：2026-09-02

状态：M1C 正式通过（[`r01-m1c-validation.md`](r01-m1c-validation.md)）前提下的已知项登记；三项均**已知并接受**，不在登记会话修改已验收代码

本文登记 M1C 验收之后、对冻结 lineage run `19d0325c…aa359` 与已发布 M1B run `519a86d3…e06d1d` 做只读独立复算时发现的三个点。它不撤销 M1C 的正式结论，也不新增任何代码。任何收紧都必须走单独 reopen 与重新验收。

## 1. 登记条目一览

| 编号 | 类别 | 定位 | 是否在 R01 触发 | 严重度 | 处置 |
| --- | --- | --- | --- | --- | --- |
| K1 | `IDENTICAL_RAW_REQUEST_HASH` 的 Grade-A 定级缺乏语义依据；门② 的实证依据由此反转 | `m1c-processing-spec.md` §3 门②、§4.4、§5.3；`lineage/builder.py:130-167` | 是（19 组 / 47 capture / 41 边，17 边为该关系独有） | **高**（证据分级错误，影响 M2 去重与 lineage 主张） | 登记；建议 reopen 降级或加语义前置条件 |
| K2 | 规格 §5.4 空集断言的论证是非推论 | `m1c-processing-spec.md` §5.4 | 结论为真（独立复算 0 组），理由不成立 | 低（文档） | 登记；下次修订规格时更换论证 |
| K3 | 桶内两两建边 O(K²) 无上限护栏 | `lineage/builder.py:106`、`152`、`195` | 否（R01 最大桶 K=5） | 中（新 cohort 可击穿 512 MiB 停止线） | 登记；新 cohort 接入前补显式上限并 fail-closed |

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

### 2.3 为什么可以接受并登记

- 边只在 Control 侧使用，不进入任何 Public 视图；M2 尚未启动，尚无下游消费这 17 条边。
- 验收门检验的是"validator 能否独立复算出与规格定义一致的边集"，该门成立；问题在规格定义本身的证据等级，不在实现。
- 修改属于契约收紧，须走 reopen。

### 2.4 建议的 reopen 方向（不在本会话执行）

三选一，由项目负责人决定：

- **降级并改名**：改为 Grade-B 投影关系，命名反映其真实语义（例如 `IDENTICAL_REQUEST_SKELETON_HASH`），与缓做的 `NORMALIZED_VISIBLE_PREFIX_OF` 同级；
- **加语义前置条件保留 Grade-A**：同 hash 且不可定位前缀指纹链相等才建边（R01 上只剩 1 组），否则缺席；
- **删除该关系**：R01 上它独有的信息量为零（24 条重合边已由 SHARED 覆盖，17 条独有边不可信）。

无论哪种，§3 门② 的措辞应从"实证依据"改为"预防性不变量"，§4.4 的"门② 的实证依据"一句应删除。

### 2.5 复算方法（可重现，不入代码）

对已发布 `captures.jsonl` 按 64-hex `raw_request_hash` 分桶取 >1 的组；对每组比较 `message_count`、`model`、`tool_catalog_id`、`source_request_count`、`usage`、`request_time_start` 是否恒等；对 `event_occurrences.jsonl` 按 capture 收集 `(sequence_number, event_kind, visible_payload_sha256)`，分别对全链与 `event_scope=PRE_FIRST_OBSERVED_TERMINAL` 子链取 canonical 摘要比较；对 lineage run 的 `capture_relation_edges.jsonl` 统计 IDENTICAL 端点对与 SHARED 端点对的交集。

## 3. K2：§5.4 空集断言的论证

§5.4 写："R01 中 `target_hash` 全 1,683 唯一，该集合为空"，用以支撑"可见指纹链相等但 `source_request_id` 序列不同"的情形在 R01 不存在。`target_hash` 与 `raw_request_hash` 一样是上游不透明值，它的唯一性不能推出任何可重算派生属性的唯一性。结论本身经独立复算为真（可见指纹链相等的 capture 分组数 = 0），但论证应替换为该直接复算。K1 已说明为何不能用上游哈希代替派生事实。

## 4. K3：桶内两两建边缺上限

`_build_shared_source_request_edges`、`_build_identical_raw_request_hash_edges`、`_build_complete_duplicate_capture_edges` 三处对桶内 capture 做 `combinations(sorted(bucket), 2)`，边数为 K(K−1)/2。R01 最大桶 K=5，无害。若未来 cohort 出现被上千 capture 共享的 `source_request_id`（例如同一 system 模板被采集层误写入 request ID），边数会达百万级，突破 512 MiB 停止线且 validator 的完整边集 bijection 同样膨胀。建议在新 cohort 接入前于三处加显式桶大小上限，超限整批 fail-closed 并写入 attrition，而不是静默截断。

## 5. 与验收门的关系

三项均不改变 `19d0325c…aa359` 的已发布字节，也未使 [`r01-m1c-validation.md`](r01-m1c-validation.md) 任一门变红。K1 影响的是规格对证据等级的**主张**，不是实现对规格的**符合性**。本登记不改变「M1C 正式通过」结论；但在 K1 的 reopen 决定之前，**M2 不得把 `IDENTICAL_RAW_REQUEST_HASH` 边用于 lineage 去重或任何合并判断**。
