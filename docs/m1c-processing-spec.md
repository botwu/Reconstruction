# M1C 跨 capture 关系图处理规格

版本：v0.3（已批准开工；范围锁定＝全 Grade-A、缓 Grade-B）

日期：2026-09-01

状态：**范围已获批准并进入实现。本次实现全部 4 类 Grade-A 关系 + `RequestLineageForest` + `CaptureRelationGraph` + 独立 validator；Grade-B `NORMALIZED_VISIBLE_PREFIX_OF` 按 YAGNI 缓做（代码枚举不含、不留占位值，仅本规格标注「预留、本次不实现」）。始终不得改动冻结 M1B 字节/代码。**

评审记录：v0.1 经一轮多维对抗式评审（四硬门 soundness / AGENTS 纪律 / 增量非破坏 / 证据分级自洽 / validator 完备性与隐私）。v0.2 已闭合评审确认的 2 个 blocker（§6 让 Grade-A 的 `SHARED_SOURCE_REQUEST` 默认组内枚举；`EXPLICIT_REQUEST_SUCCESSOR` 定义与 §4.4 基线互斥）与全部 major/minor。其中 `EXPLICIT_REQUEST_SUCCESSOR` 基线与请求森林拓扑已在冻结 run `519a86d3…e06d1d` 上独立复算核实（见 §4.4）。v0.3 为「批准开工」修订：(1) §5.6 纠正 v0.2 与 §1 DRY 冲突的「不 import json_codec」措辞，改为复用公开内核 + 自有命名空间；(2) 全文 Grade-B `NORMALIZED_VISIBLE_PREFIX_OF` 标注「预留、本次不实现」（代码枚举不含、不留占位）；(3) §8 补充 validator 双参（`<lineage_run> <m1b_run>`）理由与隐私扫描器盲区说明。

本文是 M1C（Request/Capture Graph）的实施规格草案。项目背景见 [`background-and-goals.md`](background-and-goals.md)，总体阶段与下游边界见 [`overall-plan.md`](overall-plan.md) §4.2、§9，上游冻结契约见 [`r01-processing-spec.md`](r01-processing-spec.md)（M1A/M1B）与 [`r01-m1-v3-validation.md`](r01-m1-v3-validation.md)（M1A/M1B v3 正式验收），开发纪律只引用 [`../AGENTS.md`](../AGENTS.md)。

M1C 与 M1A/M1B 一样，是**纯确定性代码层，不调用任何模型或 agent**，不产生语义标签。它在 M1B 已发布产物之上新增跨 capture 的关系边，从不重写 M1B 节点，也从不改动 M1B 字节。

## 1. 授权范围

M1C 只实现：

```text
在一个已发布 M1B run 之上新增两类只读派生关系产物：
  RequestLineageForest    （请求级 Grade-A 显式关系）
  CaptureRelationGraph    （capture 级 Grade-A 重复/共享；Grade-B 可见前缀投影＝预留、本次不实现）
```

**本次实现范围锁定（v0.3，经批准）：** 实现全部 4 类 Grade-A（`SHARED_SOURCE_REQUEST` / `EXPLICIT_REQUEST_SUCCESSOR` / `IDENTICAL_RAW_REQUEST_HASH` / `COMPLETE_DUPLICATE_CAPTURE`）+ `RequestLineageForest` + `CaptureRelationGraph` + 独立 validator。**缓做 Grade-B `NORMALIZED_VISIBLE_PREFIX_OF`**：按 [`../AGENTS.md`](../AGENTS.md) §1 YAGNI，代码 `LineageRelation` 枚举**只放 4 个 Grade-A、不放 Grade-B、不放任何占位值**（`UNKNOWN_LINEAGE` 等一律不入代码）；本规格保留 Grade-B 的完整定义仅作「预留、本次不实现」的设计留档，待后续单独窗口再实现。下文所有标注 `NORMALIZED_VISIBLE_PREFIX_OF` 的段落均属此预留范畴。

M1C 不实现，也不得声称实现：

- QueryTurn、UserBlock、ThreadTurnGraph（属 M1D）；
- TaskEpisode、语义关系分类、任务或环境画像（属 M2，且需先冻结来源注解投影）；
- 任何 LLM 调用、agent rollout、多数投票或 Ground Truth；
- World、Truth、Reference、Verifier、难度或认证；
- 对 capture 的硬去重、合并或“选最长丢其余”。

M1C 的输出是**来源解析（source lineage）信息，只在 Control 侧使用**（见 [`overall-plan.md`](overall-plan.md) §9「Public 与 Control 隔离」：Public 侧明令禁止出现 host 绝对路径与 source lineage 解析信息）。它不进入任何面向被测模型的 Public 视图。

## 2. 输入契约

### 2.1 唯一输入是一个已发布 M1B run 目录

M1C 只消费一个已通过验收的 M1B run 的**已发布数据契约字段**（[`../AGENTS.md`](../AGENTS.md) §4：数据契约是跨模块通信的唯一边界，模块不得导入其他模块的私有实现）：

```text
traceforge lineage build \
  --m1b-run <content_addressed_m1b_run_dir> \
  --output <lineage_artifact_root>
```

硬约束：

- **不重新解析原始 JSONL。** M1C 不接收 `--input` 原始文件，不按绝对路径私下重解析 R01。它需要的全部事实都已在 M1B 产物中发布。
- **不 import M1B 私有实现。** 只读取 `private/*.jsonl`、`source_manifest.json`、`artifact_manifest.json`，通过稳定 ID 和已发布字段建边。M1B 的 `compiler.py`、`privacy.py` 等内部函数不是契约。
- **不修改 M1B 字节。** 输入 M1B run 目录以只读方式打开；M1C 产物写入独立的 lineage artifact root。
- **先校验再消费（只凭已发布物，不复刻 M1B 私有派生）。** M1C 在建图前独立核验输入 M1B run：逐个重算 `private/*.jsonl` 与 `reports/*` 文件的 SHA-256 并与 `ArtifactManifestV1.files` 逐条比对、重算 `artifact_manifest` 自身摘要、核对目录 inventory 完整、交叉核对 `SourceManifestV1` 与 `ArtifactManifestV1` 的 `dataset_id`/`dataset_sha256`/`source_schema` 一致，并把 M1C 产物绑定到**已发布的 `ArtifactManifestV1.run_id`**（作为 §5.6 的 `m1b_run_id`）。M1C **不重新推导内容寻址 run ID 的派生公式**——该公式是 M1B 私有实现（不是契约），重算它必然要复刻或 import 私有约定，违反 §2.1「不 import M1B 私有实现」与 [`../AGENTS.md`](../AGENTS.md) §1/§4。任一核验不符则在建图前整批失败，不产出半份关系图。

### 2.2 M1C 消费的已发布字段

下列字段来自冻结的 M1B 契约（[`r01-processing-spec.md`](r01-processing-spec.md) §6、`trajectory/contracts.py`），M1C 只读取、不重新定义：

| 来源产物 | M1C 消费字段 | 用途 |
| --- | --- | --- |
| `NormalizedCaptureV2` | `capture_occurrence_id` | capture 节点稳定 ID |
| | `source_record_id`、`source_capture_id` | 回溯物理行；capture 的终端 request 身份 |
| | `candidate_group_id`、`thread_id`、`account_id` | 阻塞式比较提示（**仅 BLOCKING_HINT**，见 §3 门① / §4.4） |
| | `request_boundary_ids`、`source_request_count` | 关联 RequestBoundary 节点 |
| | `raw_request_hash` | `IDENTICAL_RAW_REQUEST_HASH` 候选证据（须先过 §7 格式契约） |
| | `target_hash` | capture 身份/完整性校验（唯一，不作 lineage 键，见 §5.5） |
| `RequestBoundaryV1` | `request_boundary_id`、`capture_occurrence_id` | boundary 节点与归属 |
| | `source_request_id`、`boundary_ordinal` | 请求级 lineage 与 `SHARED_SOURCE_REQUEST` |
| | `terminal_event_id` | 关联终端事件 |
| `EventOccurrenceV2` | `capture_occurrence_id`、`sequence_number` | 可见时序 |
| | `event_kind`、`visible_payload_sha256` | 可见指纹链（`NORMALIZED_VISIBLE_PREFIX_OF` 与 `COMPLETE_DUPLICATE_CAPTURE`） |
| `ArtifactManifestV1` | `run_id`、`dataset_id`、`dataset_sha256`、`source_schema`、`compiler_contract_version`、`files` | 输入身份绑定与校验 |
| `SourceManifestV1` | `dataset_id`、`dataset_sha256`、`source_schema` | 与 `ArtifactManifestV1` 交叉一致性核验（§2.1「先校验再消费」） |

M1C 不读取、不解释 M1B 保存的可见正文原文、tool arguments、reasoning 审计摘要或任何 Data URL 摘要内容。它只使用上述**结构与指纹字段**建边。

## 3. 四条硬门

以下四门在 M1C 启动前必须先冻结，来自 [`overall-plan.md`](overall-plan.md) §4.2、[`r01-processing-spec.md`](r01-processing-spec.md) §8.1 与 [`session-handoff.md`](session-handoff.md) §11。本规格把它们细化为可校验规则：

**门① `candidate_group_id` 只能是 `BLOCKING_HINT_ONLY`。** 它是 `(thread_id, account_id)` 分区（§4.4 实测确认严格 1:1），只用于缩小比较范围以控制算法复杂度，**永远不作为任何关系边的证据**。任何 lineage 边都不得把“同候选组”写进 evidence，也不得因“同候选组”而建边。

**门② Grade-A 显式关系不受候选组边界限制。** 全部 Grade-A 证据（§5）在**全局、组盲**范围内判定。R01 实测中 `IDENTICAL_RAW_REQUEST_HASH` 有 5 组跨候选组（§4.4），若把 Grade-A 限制在候选组内会漏掉这些真实关系。候选组只能用于 Grade-B 前缀的默认比较范围优化（§6），不能裁剪 Grade-A。

**门③ M1C validator 独立核验候选组分区，而非重算其 ID 字符串。** `candidate_group_id` 的派生公式（namespace 常量 + `stable_id` 序列化）只存在于 M1B 私有实现（`compiler.py`），不是已发布契约；重算该字符串必然要复刻或 import 私有约定，违反 §2.1 与 [`../AGENTS.md`](../AGENTS.md) §1/§4。因此 validator **只用已发布字段做分区一致性校验**：把 capture 按已发布的 `candidate_group_id` 分区，再按 `(thread_id, account_id)` 分区，断言两个分区**严格 1:1 双射**（等价类完全对应，§4.4 实测确认）。这恰好覆盖门①的真实目的——证明候选组无非就是 `(thread_id, account_id)` 分区、不携带任何额外 lineage 信息——且无需任何私有公式。若未来确需按值重算 `candidate_group_id`，前置条件是先由 M1B 把其稳定 ID 构造（namespace + 身份字段集）升格为已发布冻结契约。

**门④ `raw_request_hash` 只有满足 §7 冻结格式契约才作证据，否则该 capture 的 `raw_request_hash_status` = `UNKNOWN`（逐 capture 资格状态，非边关系，见 §5/§7）。** R01 实测 1,683 条中 1,675 条为 64 位十六进制、8 条为 80/86 长度的异常值（§4.4）；异常值一律 `UNKNOWN`，绝不进入 `IDENTICAL_RAW_REQUEST_HASH`，即使它们恰好互相相等。

## 4. 数据层级与新增契约

### 4.1 层级

```text
（已冻结，来自 M1B）
NormalizedCapture / RequestBoundary / EventOccurrence

（M1C 新增，只引用不复制）
RequestNode
    一个 distinct source_request_id，及其归属 capture 与 boundary 引用

RequestLineageForest
    RequestNode 之间的 Grade-A 显式 request 关系（EXPLICIT_REQUEST_SUCCESSOR）

CaptureRelationEdge
    capture 之间的 Grade-A（SHARED_SOURCE_REQUEST / IDENTICAL_RAW_REQUEST_HASH /
    COMPLETE_DUPLICATE_CAPTURE）关系
    （Grade-B NORMALIZED_VISIBLE_PREFIX_OF＝预留、本次不实现，见 §1）

CaptureRelationGraph
    CaptureRelationEdge 的集合
```

M1C 的节点只保存对 M1B 节点的**稳定 ID 引用**与该关系自身的证据，不复制任何 M1B 节点的正文、payload 或字段值（[`../AGENTS.md`](../AGENTS.md) §1 DRY：契约只有一个权威来源；§4：原始事件不可覆盖，派生物带来源引用）。

### 4.2 RequestLineageForest（请求级，Grade-A）

节点 `RequestNode` 字段草案（最终字段以评审冻结为准）：

```yaml
schema_version:
request_node_id:          # 稳定 ID，见 §5.6
source_request_id:        # 来自 RequestBoundaryV1，不重定义
owner_capture_ids:        # 出现该 source_request_id 的全部 capture（保留全部，不去重丢弃）
boundary_ordinals:        # 该 request 在各 capture 中的 boundary_ordinal
```

边 `RequestSuccessorEdge` 字段草案：

```yaml
schema_version:
edge_id:                  # 稳定 ID，见 §5.6
parent_request_node_id:
child_request_node_id:
relation: EXPLICIT_REQUEST_SUCCESSOR
evidence:                 # 支撑该 successor 的 (capture_id, parent_ordinal, child_ordinal) 证据集合
```

“forest”而非“tree”：R01 实测请求级相邻关系构成**真正的森林**——每个请求节点入度 ≤ 1（无双亲）、可有多子（出度实测最高 13）、无环（§4.4 已在冻结 run 上独立复算核实）。capture 在共享 source-request 前缀后分叉正是多子分支的来源。M1C 只保存显式相邻 successor 边，不推断跨越多个 request 的间接祖先关系。

### 4.3 CaptureRelationGraph（capture 级，Grade-A + Grade-B）

边 `CaptureRelationEdge` 字段草案：

```yaml
schema_version:
edge_id:                  # 稳定 ID，见 §5.6
endpoint_capture_ids:     # 无向关系保存排序后的两端；有向关系（前缀）区分 from/to
relation:                 # §5 冻结枚举之一（唯一权威字段）
evidence:                 # 该关系类型的最小可重算证据（见 §5）
```

`grade`（A|B）与 `directionality`（UNDIRECTED|DIRECTED）是 `relation` 的**固定函数**（§5 冻结枚举已一一确定），因此**不作为独立可写字段物化进每条边**，以免复制 §5 枚举表、引入可漂移的第二处事实（[`../AGENTS.md`](../AGENTS.md) §1 DRY，亦符合 §4.3 自身「不输出可能失配的第二份 dump」的立场）。读取方由单一 `relation → (grade, directionality)` 映射派生；validator 断言该映射与 §5 一致（§8）。

CaptureRelationGraph 是一张属性图，但与 M1B 一样**不引入图数据库、不输出可能失配的第二份 graph dump**：规范化的 `capture_relation_edges.jsonl` 与 `request_*` 表就是图，节点是 M1B 已发布 capture / request，边是本层新增记录。

### 4.4 输入侧 lineage 基线（信息性，非硬编码）

下列数字由冻结 M1B run `519a86d3…e06d1d` 的已发布产物派生，用于给本规格的关系定义与决策点提供实测依据。它们**只是外部 oracle，不得写入 M1C 核心或通用 validator 分支**（同 [`r01-processing-spec.md`](r01-processing-spec.md) §4.5 的边界约束）：

```text
captures                              = 1,683
request boundaries                    = 9,561
distinct source_request_ids           = 6,301
candidate groups                      = 956  （775 单例 + 181 多元，最大 32）
(thread,account) ↔ candidate_group_id = 严格 1:1（0 例跨组）

SHARED_SOURCE_REQUEST
  source_request_ids 被 >1 capture 共享 = 942
  涉及 capture                          = 475
  共享 capture 对                       = 1,671（全部“共享前缀后分叉”，0 例跨候选组）

EXPLICIT_REQUEST_SUCCESSOR（请求级，同一 capture 内 boundary_ordinal 相邻的 source_request_id 对，见 §5.2）
  含 ≥2 boundary 的 capture             = 798
  相邻观测（含重复见证）                = 7,878
  去重后 distinct 请求节点相邻边        = 4,994
  森林拓扑（在冻结 run 独立复算）       = 入度 ≤ 1、出度最高 13、无环、无双亲 → 真正的森林

（对照观测，非本枚举）capture 请求序列的 capture 级线性严格前缀
  组内 = 0；跨组 = 0   → 说明 capture 是「共享前缀后分叉的兄弟」，不是 capture 级线性后继，故本关系落在请求级（§9 D1）

IDENTICAL_RAW_REQUEST_HASH
  raw_request_hash 64-hex 合格          = 1,675
  raw_request_hash 异常长度(80/86)      = 8   → 一律 UNKNOWN（§7 逐 capture 资格状态，不建边）
  被 >1 capture 共享的合格值组          = 19（涉及 47 capture）
  其中跨候选组的组                      = 5   → 门② 的实证依据

COMPLETE_DUPLICATE_CAPTURE（可见事件链完全相同）
  = 0
  target_hash 全 1,683 唯一（无完整重复的独立佐证由本计数给出），故 target_hash 不作 lineage 键

NORMALIZED_VISIBLE_PREFIX_OF（Grade-B，可见指纹链严格前缀）
  组内有序对 = 7（分布在 3 个候选组）
  跨组下界   ≥ 1（疑似短链偶合，正是前缀只能作投影的理由）
```

结论：在 R01 上，`SHARED_SOURCE_REQUEST`（1,671 对，分叉森林）与 `EXPLICIT_REQUEST_SUCCESSOR`（4,994 条请求节点森林边）都大量发火；`IDENTICAL_RAW_REQUEST_HASH` 少量发火且**会跨候选组**；只有 `COMPLETE_DUPLICATE_CAPTURE` 在 R01 为 0 但仍作为通用契约保留；Grade-B 前缀极稀疏且可能跨组偶合。**注意：capture 级「线性严格前缀 = 0」是解释请求级归属的对照观测，不是 `EXPLICIT_REQUEST_SUCCESSOR` 的基线**——后者按 §5.2 的请求相邻定义大量发火，v0.1 曾把两者混为一谈，v0.2 已拆清。

## 5. 证据分级（冻结枚举）

关系类型是封闭枚举，任何一条**边**必须恰好落在其中之一（边只承载已成立的正向关系；不成立即不建边，不存在“无法判定”的边）：

```text
Grade A（高可信显式关系，全局组盲判定）
  SHARED_SOURCE_REQUEST
  EXPLICIT_REQUEST_SUCCESSOR
  IDENTICAL_RAW_REQUEST_HASH
  COMPLETE_DUPLICATE_CAPTURE

Grade B（低等级可见投影，仅投影）——预留、本次不实现
  NORMALIZED_VISIBLE_PREFIX_OF        # 代码枚举不含此值，见 §1 范围锁定
```

> 实现说明（v0.3）：代码里的 `LineageRelation` 枚举**恰好只有上列 4 个 Grade-A**。Grade-B 段落保留在本规格仅为设计留档；测试专门守护「`NORMALIZED_VISIBLE_PREFIX_OF` 与任何占位值不在枚举」以防提前引入。

**逐 capture 资格状态（不是边关系）**：`raw_request_hash_status ∈ { QUALIFIED, UNKNOWN }` 是单个 capture 的 `raw_request_hash` 是否满足 §7 格式契约的注解。`UNKNOWN` 的 capture 不参与 `IDENTICAL_RAW_REQUEST_HASH` 建边——它不产生任何“UNKNOWN”边，只是**缺席**该关系。此状态与边关系枚举分属两个命名空间，切勿混用同一 token（v0.1 曾用悬空的 `UNKNOWN_LINEAGE` 边表述，v0.2 已移除）。

### 5.1 SHARED_SOURCE_REQUEST（Grade A，无向，capture 级）

两个不同 capture 的 `RequestBoundary.source_request_id` 集合存在非空交集即建边。evidence = 排序后的共享 `source_request_id` 集合（或其 canonical 摘要）。全局组盲计算（门②）。这是 R01 的主力关系（1,671 对）。

### 5.2 EXPLICIT_REQUEST_SUCCESSOR（Grade A，有向，请求级）

在**同一 capture 的有序 boundary 序列**中，`boundary_ordinal` 相邻的两个**不同** `source_request_id` 构成 parent→child successor 边（进入 RequestLineageForest）。这是数据中显式记录的相邻关系，不是推断。同一对 (parent_srid, child_srid) 在多个 capture / 多个位置被见证时**去重为一条请求节点边**，其 evidence 汇集全部见证 `(capture_id, parent_ordinal, child_ordinal)`（§4.2）；相邻两 boundary 若 `source_request_id` 相同则不建自环边。R01 实测：7,878 次相邻观测去重为 **4,994 条 distinct 请求节点边**（§4.4，非 0）。

注意：R01 中 capture 请求序列不存在 capture 级线性严格前缀（§4.4 的对照观测 = 0），因此本关系只以**请求节点相邻边**形式存在，不表述为“capture A 续 capture B”——这也是它落在请求级 RequestLineageForest 而非 capture 级图的理由（§9 D1）。

### 5.3 IDENTICAL_RAW_REQUEST_HASH（Grade A，无向，capture 级）

两个不同 capture 的 `raw_request_hash` **均满足 §7 格式契约且相等**时建边。evidence = 该合格 hash。异常格式值（§4.4 的 8 条）一律 `UNKNOWN`，绝不建此边。全局组盲计算，可跨候选组（门②）。

### 5.4 COMPLETE_DUPLICATE_CAPTURE（Grade A，无向，capture 级）

两个不同 capture 的**可见指纹链完全相同**：按 `sequence_number` 排序的 `(event_kind, visible_payload_sha256)` 序列逐项相等，且 `source_request_id` 序列相等。evidence = 该指纹链摘要。**全局组盲计算（门②）。** R01 中为 0，但保留为通用契约。**即使判定为完整重复，也只建边，不删除任一 capture**（禁止“删短留长”，见 §5.7）。

边界澄清：若两 capture 可见指纹链逐项相等但 `source_request_id` 序列不同（等长、非前缀），则既不满足本关系（要求 srid 序列相等）、也不满足 §5.5 严格前缀（要求长度不等）——此情形**有意不建边**（两 capture 是同内容但不同请求身份，非重复亦非投影）。R01 中 `target_hash` 全 1,683 唯一，该集合为空；此处显式声明是为消除边界含糊，validator 据此断言该情形不产生任何边。

### 5.5 NORMALIZED_VISIBLE_PREFIX_OF（Grade B，有向，capture 级）

> **预留、本次不实现（v0.3）。** 本节完整保留 Grade-B 定义仅作设计留档；本次代码枚举不含此关系、不建此边、不留占位值。下方定义待后续单独窗口实现时再据以落地。

capture A 按 `sequence_number` 排序的 `(event_kind, visible_payload_sha256)` 指纹链是 capture B 对应链的**严格前缀**时，建 A→B 的 Grade-B 投影边。完全由 M1B 已发布的 per-event 可见指纹重算，**M1B 无需改动**。

Grade-B 硬约束（[`overall-plan.md`](overall-plan.md) §4.2）：

- 不用于因果继承 / lineage；
- 不用于主分布硬去重；
- 不单独作为任何下游认证任务的硬证据。

`target_hash` 全局唯一（§4.4），只用于 capture 身份与完整性交叉校验，**不产生任何 lineage 边**。

### 5.6 稳定 ID

M1C 为自己的 `request_node_id`、`edge_id` 采用与 [`r01-processing-spec.md`](r01-processing-spec.md) §4.3 **相同的已公开内容寻址约定**——`sha256(namespace "\0" canonical_json(identity))`，并**复用 `trajectory/json_codec.py` 的公开内核**（`stable_id`、`canonical_json_bytes`、`sha256_bytes` 等），只**传入自有命名空间常量**（`"lineage-request-node-v1"`、`"lineage-edge-v1"`、`"lineage-run-v1"`）。

> 勘察修正（v0.3）：`json_codec.py` 是全包**公开可信内核**——连独立验收 validator `trajectory/validation.py` 都 import 它做逐文件重哈希。v0.2 曾写「不 import `json_codec`、在自有命名空间独立实现」，与 [`../AGENTS.md`](../AGENTS.md) §1 DRY（契约/内核只有一个权威来源，禁重复实现）直接冲突，v0.3 已改为「复用内核 + 自有命名空间常量」。§2.1「不 import M1B 私有实现」真正约束的是 M1B 的**私有派生公式**（`run_id`/`candidate_group_id`/`capture_occurrence_id` 等的 namespace 字符串与身份字段集），而非通用哈希/canonical 内核；M1C 复刻公式才违纪，复用内核不违纪。

身份字段全部来自已冻结上游标识与本关系语义，绝对路径不参与：

```text
request_node_id = sha256(
  "lineage-request-node-v1\0" + m1b_run_id + "\0" + source_request_id )

edge_id = sha256(
  "lineage-edge-v1\0" + m1b_run_id + "\0" + relation + "\0"
  + canonical(ordered endpoint ids) + "\0" + canonical(evidence_key) )
```

`m1b_run_id`（= 已发布 `ArtifactManifestV1.run_id`，§2.1）参与全部 ID，使同一关系在不同 M1B run 上不会串号。schema 版本与身份算法版本分开管理（同 [`r01-processing-spec.md`](r01-processing-spec.md) §6）。

### 5.7 禁止硬去重

M1C 保留全部 capture 与全部关系边。**不得**因 `COMPLETE_DUPLICATE_CAPTURE` 或 `NORMALIZED_VISIBLE_PREFIX_OF` 而删除、合并或隐藏任一 capture（[`overall-plan.md`](overall-plan.md) §4.2“不得直接选最长 capture 后丢弃其他记录”）。去重是下游（M2 lineage 去重）在带来源约束下的独立决策，不在 M1C 发生。

## 6. 可见前缀算法与比较范围

- 指纹链只由 M1B 已发布的 `EventOccurrenceV2` 字段重建：按 `sequence_number` 排序的 `(event_kind, visible_payload_sha256)` 序列。M1C 不重新计算任何 payload 摘要，也不读取正文。
- **全部 Grade-A 关系必须全局、组盲判定（门②），一律不得默认在候选组内枚举。** 实现方式一律为**全局键桶**，而非组内枚举：
  - `SHARED_SOURCE_REQUEST`：按 `source_request_id` 建全局倒排桶，桶内 >1 capture 即两两建边；
  - `IDENTICAL_RAW_REQUEST_HASH`：按合格 `raw_request_hash` 建全局桶（R01 有 5 组跨候选组）；
  - `COMPLETE_DUPLICATE_CAPTURE`：按 `(可见指纹链摘要, source_request_id 序列摘要)` 建全局桶；
  - `EXPLICIT_REQUEST_SUCCESSOR`：capture 内相邻关系，天然与候选组无关，全局收集去重。
  候选组对 Grade-A **至多只影响枚举顺序与性能，绝不改变边集**；validator 全局独立复算须得到**同一边集**（§8）。**不得**以 R01「`SHARED_SOURCE_REQUEST` 0 例跨候选组」这一实测巧合作为对 Grade-A 收窄比较范围的依据（[`r01-processing-spec.md`](r01-processing-spec.md) §4.5、§8.1；§4.4 数字不得写入核心分支）。
- **（预留、本次不实现）** Grade-B 的 `NORMALIZED_VISIBLE_PREFIX_OF` 原计划默认在候选组内枚举（门① 的 BLOCKING_HINT 用法，D2 可评审是否放开为全局但标记更低置信）：Grade-B 仅投影、不要求完整性，跨组前缀漏建可接受（§4.4 跨组 ≥1 疑为短链偶合）。本次不落地此算法，仅留作后续窗口的设计依据。

## 7. `raw_request_hash` 格式契约（门④，决策点 D4）

冻结格式契约（草案，待 §9 D4 评审确认）：

```text
capture 的 raw_request_hash_status = QUALIFIED（可作 IDENTICAL_RAW_REQUEST_HASH 证据），当且仅当：
  - 类型为字符串；
  - 长度恰好 64；
  - 全部字符属于小写十六进制 [0-9a-f]。
否则 raw_request_hash_status = UNKNOWN（该 capture 不参与 IDENTICAL 建边，见 §5）。
```

R01 实测：1,675 条 `QUALIFIED`、8 条（长度 80/86）`UNKNOWN`。契约以**格式**而非 R01 具体值判定，不把任何样本 hash 写入代码。`raw_request_hash_status` 是逐 capture 资格注解，不是边关系（§5）。

## 8. 独立 validator

新增 `scripts/validate_m1c_run.py`，镜像 M1B `trajectory/validation.py` 的模板与信任边界（[`r01-processing-spec.md`](r01-processing-spec.md) §7.1）：独立读取已发布 M1C 产物，不调用 M1C 建图器重建期望结果，也不内置 R01 常量。

**双参调用（`<lineage_run_dir> <m1b_run_dir>`，均必填）。** lineage run 只以 `m1b_run_id` + `artifact_manifest` SHA-256 **内容寻址绑定** M1B（§10 禁存路径、不复制 M1B 数据），因此要「全局组盲独立复算完整边集」就必须回到 M1B 源表取 capture/boundary/hash/指纹等已发布字段作 oracle。validator 先断言所给 M1B run 的已发布身份（`ArtifactManifestV1.run_id` 等）与 lineage 声明的绑定一致，再据此复算；两者不一致即 fail-closed。

它必须独立重算：

**输入绑定与完整性**
- 逐个重算 `private/*.jsonl` 与 `reports/*` 的 SHA-256 并与 `ArtifactManifestV1.files` 逐条比对、重算 manifest 自身摘要、核对目录 inventory；交叉核对 `SourceManifestV1` 与 `ArtifactManifestV1` 的 `dataset_id`/`dataset_sha256`/`source_schema`；确认 M1C 产物绑定的 `m1b_run_id` 等于已发布 `ArtifactManifestV1.run_id`。**不重算 M1B 私有的内容寻址 run_id 派生公式**（§2.1）。

**门③ 候选组分区一致性（不重算私有 ID 公式）**
- 按已发布 `candidate_group_id` 分区与按 `(thread_id, account_id)` 分区，断言两者**严格 1:1 双射**；断言无任何边把「同候选组」写入 evidence（门①）。

**边集完整性（双向 bijection，不止逐条 soundness）**
- 对每类 **Grade-A** 关系，validator 从 M1B 已发布字段**全局、组盲独立枚举出完整边集**，与 `private/` 已发布边集做**双向集合相等**断言（既无漏报、也无幻影）。逐条复核只能防「多报无效边」，唯有完整性断言能防「悄悄删边」这种来源洗白——对只在 Control 侧使用的 lineage 产物这是首要威胁。
- `IDENTICAL_RAW_REQUEST_HASH` 必须复现全部跨候选组边；`SHARED_SOURCE_REQUEST`、`COMPLETE_DUPLICATE_CAPTURE` 同样全局复算完整边集。
- **（预留、本次不实现）Grade-B** 前缀边：原计划逐条 soundness（每条已发布前缀边由 per-event 指纹链独立重算成立）即可，因默认组内、跨组有意漏建而**不做完整性 bijection**。本次既不产出 Grade-B 边，validator 亦无此分支；作为守护，validator 断言 `private/` 边集**不含** `NORMALIZED_VISIBLE_PREFIX_OF` 或任何非 4-Grade-A 的 relation 值。

**守恒**
- capture 节点数等于输入 M1B capture 数，无 capture 被删除或合并；
- `RequestNode` 数等于 distinct `source_request_id` 数；每个 `RequestNode` 的 `owner_capture_ids` / `boundary_ordinals` 完整填充（§4.2「保留全部，不去重丢弃」），无遗漏。

**格式契约、稳定 ID、派生属性**
- `raw_request_hash_status` 格式契约判定与 `UNKNOWN` 计数（门④）；确认 `UNKNOWN` capture 未参与 IDENTICAL 建边；
- 每条边 `edge_id`、每个 `request_node_id` 的稳定 ID 由 §5.6 公式独立重算一致；
- 断言每条边的 `relation → (grade, directionality)` 与 §5 冻结映射一致（§4.3 派生而非物化）；
- 断言无任何边以 `target_hash` 为 evidence（§5.5）。

**隐私（两层：reports/ 聚合 + private/ 闭合值域）**
- `reports/` 公共报告只含聚合计数，不含原始 ID、正文、路径、URL 或 Data URL；
- `private/*.jsonl` **逐记录闭合 schema 校验 + 值域白名单为主**：字段仅限稳定 ID hex、`source_request_id` 令牌、64-hex 指纹、序数、枚举。**注意 M1B 的 `find_privacy_violations` 只扫 Base64 Data URL 与 reasoning 结构，不覆盖普通 URL / 绝对路径 / 主机身份**；故 private/ 隐私以**闭合值域白名单**作首要防线，`find_privacy_violations` 仅作 Data URL/reasoning 兜底（[`r01-processing-spec.md`](r01-processing-spec.md) §5.2 的「派生 artifact 一律禁 Base64 Data URL」适用于 M1C 派生 private/）；
- **控制文件**（`lineage_manifest.json`、`artifact_manifest.json`、`run_receipt.json`）断言不含 `--m1b-run`/`--output` 的绝对路径、文件名、主机身份或原始 ID；输入 M1B run 只以内容寻址 run_id / `artifact_manifest` SHA-256 绑定（[`overall-plan.md`](overall-plan.md) §9 Public 禁 host 绝对路径与 source lineage 解析信息；同 [`r01-processing-spec.md`](r01-processing-spec.md) §4.4 run_receipt 禁存输入/输出绝对路径）。

自报的边、分级或计数即使被同步篡改，也不能覆盖上述可重算事实（fail-closed，[`../AGENTS.md`](../AGENTS.md) §3）。validator 对所有同契约 lineage run 使用同一逻辑，不含 R01 分支。

## 9. 待评审决策点

- **D1（四类 Grade-A 归属与定义）**：推荐 `EXPLICIT_REQUEST_SUCCESSOR` 落在请求级 RequestLineageForest（capture 内相邻 boundary 的请求节点边，R01 实测 4,994 条 distinct 边、真森林拓扑，§4.4），其余三类落在 capture 级 CaptureRelationGraph。依据：capture 是「共享前缀后分叉的兄弟」，capture 级线性严格前缀 = 0（对照观测），故 successor 只能表述为请求节点相邻边、不能表述为 capture 级后继。评审需确认该归属，以及 successor 是否需要额外表达「共同祖先分叉点」。
- **D2（前缀是否跨候选组）——推迟到 Grade-B 单独窗口**：`NORMALIZED_VISIBLE_PREFIX_OF` 本次不实现（见 §1），故其默认范围（组内 vs 全局）随 Grade-B 一并推迟评审；留档推荐为默认组内、可选全局但标记更低置信（跨组前缀在 R01 疑为短链偶合 ≥1）。Grade-A 的 `IDENTICAL_RAW_REQUEST_HASH` 必须全局（门②），不受此推迟影响。
- **D3（run 布局）**：推荐独立内容寻址 lineage run，`run_id` 绑定输入 M1B run 的 `artifact_manifest` SHA-256，M1B 字节保持不变，lineage 仅 Control 侧。评审需确认目录布局（见 §10）。
- **D4（`raw_request_hash` 格式判定式）**：推荐 §7 的“64 位小写十六进制”契约。评审需确认是否接受，以及对 `target_hash`（同为 64-hex 但全局唯一）是否只作身份校验、不建边。

## 10. 输出布局（草案）

```text
<lineage_artifact_root>/<content_addressed_lineage_run_id>/
├── lineage_manifest.json          # 以 M1B run_id + artifact_manifest SHA-256 绑定输入身份（非路径），含 source_schema
├── private/
│   ├── request_nodes.jsonl
│   ├── request_successor_edges.jsonl
│   └── capture_relation_edges.jsonl
├── reports/
│   └── lineage_report.json        # 仅聚合计数，无原始 ID/正文/URL
├── artifact_manifest.json
└── run_receipt.json
```

`private/` 保存带稳定 ID 引用的关系；`reports/` 只保存聚合分级计数。`artifact_manifest.json` 只列确定性业务文件（`lineage_manifest.json` 与 `private/`、`reports/` 各文件），不列自身与 `run_receipt.json`（同 [`r01-processing-spec.md`](r01-processing-spec.md) §6）。**输入 M1B run 一律以其内容寻址 `run_id` 与 `artifact_manifest` SHA-256 绑定，三个控制文件均不得保存 `--m1b-run`/`--output` 的绝对路径、文件名或主机身份**（[`overall-plan.md`](overall-plan.md) §9；同 [`r01-processing-spec.md`](r01-processing-spec.md) §4.4），由 §8 validator 断言。真实 lineage 产物不进入 Git。

## 11. 完成条件（草案）

M1C 单独验收须满足：

- 所有 capture 节点守恒，无删除/合并；`RequestNode` 数 = distinct `source_request_id` 数，`owner_capture_ids` 完整；
- 四门（§3）在代码与 validator 中均可校验并通过；
- Grade-A 全局组盲，validator 对每类 Grade-A 独立复算**完整边集并双向 bijection**，`IDENTICAL_RAW_REQUEST_HASH` 复现跨候选组边；
- Grade-B `NORMALIZED_VISIBLE_PREFIX_OF` 本次不实现（枚举无、代码无占位），本规格标注预留；
- 独立 validator 重算全部边、分级、稳定 ID、**候选组分区一致性（1:1 双射）**、边集完整性与守恒计数并通过；
- 两次独立建图产物逐字节一致，峰值内存低于冻结停止线；
- 隐私两层：公共报告仅聚合计数；`private/` 闭合值域（无正文/URL/Data URL/绝对路径），控制文件无绝对路径/主机身份；
- 单元测试含正常场景与关键失败场景（[`../AGENTS.md`](../AGENTS.md) §5），静态检查与差异检查通过；
- 不含任何 R01 硬编码常量、模型调用、下游桩或 M1B 改动。

### 11.1 验收门证明方式与 known-items（v0.3 补齐）

验收门的实证方式分两类：**e2e 覆盖**（`tests/test_lineage_validation.py`：篡改**新建测试 M1B run** 或重签 lineage 产物后，断言目标 issue code fire；冻结 R01 字节不动）与**防御性不变量 tripwire**（正常输入结构上不触发，仅作 fail-closed 兜底，不强测）。

**e2e 覆盖的验收门与守卫码：**
- 门①/②组盲唯一实证 —— `IDENTICAL_RAW_REQUEST_HASH` 跨候选组边：两个分属不同 `(thread_id, account_id)` 候选组、共享同一合格 hash 的 capture 建 1 条跨组 IDENTICAL 边（`candidate_group_count==2`，边端点分属两组）。
- 门③候选组 1:1 分区 —— 正例（多组 run 过全部核验）+ 负例（篡改 M1B `captures.jsonl` 令两组共用同一 `candidate_group_id`、重签 → `LINEAGE_CANDIDATE_GROUP_PARTITION_MISMATCH`）。
- 边集双向 bijection —— node 删/幻影/篡改（`LINEAGE_NODE_MISSING` / `LINEAGE_NODE_PHANTOM` / `LINEAGE_NODE_MISMATCH`）、successor 边删/幻影（`LINEAGE_EDGE_MISSING` / `LINEAGE_EDGE_PHANTOM`）、endpoint 不在 M1B（`LINEAGE_ENDPOINT_NOT_IN_M1B`）。
- 分级与证据卫生 —— relation 越级（`LINEAGE_RELATION_LEVEL_MISMATCH`）、`target_hash` 混入 evidence（`LINEAGE_EVIDENCE_TARGET_HASH`，§5.5）。
- 报告闭合 —— 计数篡改（`LINEAGE_REPORT_COUNT_MISMATCH`）、白名单增删键（`LINEAGE_REPORT_ALLOWLIST_MISMATCH`）。
- 内容寻址绑定 —— 绑错 M1B oracle（`M1B_RUN_ID_BINDING_MISMATCH` + `M1B_MANIFEST_SHA_BINDING_MISMATCH`）。
- `COMPLETE_DUPLICATE_CAPTURE` 端到端首次产出并过 validator 重算（两条逐字节相同、仅 `line_number` 不同的 capture 记录）。
- reader 透传字段类型守卫 —— 篡改 M1B `candidate_group_id` 为非字符串、重签 → `LineageInputError`（把下游裸 `TypeError` 前移为 fail-closed 显式错误）。

**防御性不变量 tripwire（不强测）：**
- `LINEAGE_REQUEST_NODE_CONSERVATION`（`lineage/validation.py`）：validator 复算的 `RequestNode` 集与 distinct `source_request_id` 集均源自同一 `boundaries_by_capture`，两者基数天然恒等；该码仅防将来复算逻辑回归，正常 run 永不触发。

**已接受风险（known-items）：**
1. **控制文件 host-identity 扫描的值域缺口**：`_scan_control_pathlike`（`lineage/validation.py`）只判 `startswith("/")` 或含 `"://"`，故不含 `/` 的裸主机名/属主名（如 `dev-wujian` / `wujian1`）不被拦截。缓解：三个控制文件字段均为**闭合值域**（内容寻址 run_id / 64-hex / schema 令牌 / 计数），**无任何自由文本槽**可承载主机身份，`private/` 亦为闭合值域白名单。记为已接受风险；将来若引入自由文本控制字段再补强扫描。
2. **`RelationGrade.B` 死值已删**：实现确认 `relation_properties` / `CAPTURE_LEVEL_RELATIONS` / `REQUEST_LEVEL_RELATIONS` 均不引用 B，为兑现 §1「枚举不放 Grade-B、不放占位值」已从 `contracts.RelationGrade` 删除该成员；将来实现 Grade-B 时再新增成员并扩展 `_RELATION_PROPERTIES`（[`../AGENTS.md`](../AGENTS.md) §1 YAGNI）。

## 12. 后续边界

M1C 完成并单独验收后才讨论 M1D（QueryTurn）。进入 M2 前仍须先冻结最小、带来源的 `SourceAnnotationProjection` 或只读 `SourceResolver`（[`r01-processing-spec.md`](r01-processing-spec.md) §8.3）。M1C 不得预埋任何 M1D/M2 结构（[`../AGENTS.md`](../AGENTS.md) §1 YAGNI）。
