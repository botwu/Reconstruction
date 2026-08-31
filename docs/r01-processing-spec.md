# R01 回流处理实施规格

版本：v1.0

日期：2026-08-31

状态：M1A、M1B 实施基线；后续阶段必须另行审核

本文是 R01 回流处理的实施事实来源。项目背景见 [`background-and-goals.md`](background-and-goals.md)，总体阶段与下游边界见 [`overall-plan.md`](overall-plan.md)，开发纪律只引用 [`../AGENTS.md`](../AGENTS.md)。

## 1. 当前授权范围

当前开发会话只实现：

```text
M1A Source Adapter
+
M1B Structural Compiler
```

当前不实现：

- 跨 capture 的 Request/Capture Graph；
- QueryTurn、TaskEpisode 或任何 LLM 调用；
- 任务分布、环境暴露分布和难度画像；
- World、Truth、Reference、Verifier；
- Harbor、AGS 或模型 rollout。

M1A、M1B 全量验收完成并审核后，才能为 M1C 及之后阶段制定实施规格。

## 2. 冻结输入

```text
路径：/Users/wujian1/Downloads/seed2traj/return_data/four_batch/by-rubric/R01.jsonl
dataset_id：r01-four-batch-202607-v1
物理行数：1,683
字节数：560,481,884
SHA-256：3832d8aa4ecd577636ce67b56d8798d9fb6311662a7bbce65814e5e8d260d4d8
```

R01 是来源 cohort 和检索 rubric，不是业务 Domain。一条 JSONL 是一个规范化 capture 快照，不是 Session、用户请求或独立任务。

每条记录的顶层结构为：

```text
domain_meta
messages
meta
tools
```

`thread_id` 和 `account_id` 在全部记录中相等，共有 956 个值。它们只能作为候选比较分组，不能直接作为 Session、Episode 或 lineage ID。

## 3. 数据层级

```text
SourceRecord
    一条物理 JSONL 行

NormalizedCapture
    一个 restored_long 可见上下文快照

RequestBoundary
    source_request_ids 与 terminal_prefix_depths 给出的响应终点

Immutable Visible EventLog
    capture 内规范化可见消息的线性顺序

Derived Edges
    assistant decision、ActionBatch、tool call/result 等显式关系
```

EventLog 只表示数据中的规范化可见顺序。R01 全部声明 `representation=restored_long`，并包含 `tool_result_blocks_reordered` 修复记录，因此不能把 EventLog 声称为原始 wire order。

## 4. M1A：Source Adapter

### 4.1 命令行

```bash
traceforge trajectory compile \
  --input <R01.jsonl> \
  --dataset-id r01-four-batch-202607-v1 \
  --source-schema traceforge.restored-long-capture.v1 \
  --expected-sha256 3832d8aa4ecd577636ce67b56d8798d9fb6311662a7bbce65814e5e8d260d4d8 \
  --output <artifact_root>
```

M1 不提供模型、并发、resume 或兼容模式参数。

`source_format=jsonl` 只表示物理编码；`source_schema=traceforge.restored-long-capture.v1` 才表示单条记录的语义契约。`trajectory/source_adapter.py` 负责校验精确的 `domain_meta/messages/meta/tools` envelope 及 `representation=restored_long`，再交给不解释任意 JSON 的 Structural Compiler。

这些字段是版本化 schema，不是对 R01 样本值的硬编码。路径、dataset ID、digest、rubric、Domain、模型名、工具名和统计值不参与 adapter 分支。不支持的 `source_schema` 在读文件和创建 staging 前失败；已声明 schema 中的单条坏 envelope 进入该行 `QUARANTINED` 终态。

### 4.2 流式读取

输入必须以二进制方式读取：

1. 第一遍在扫描前后校验文件 stat，并流式计算数据集摘要、字节数和物理行数；
2. 校验 expected digest；
3. 第二遍重新打开文件，先确认 stat 与第一遍结束时一致；
4. 逐行生成 offset、length 和 digest 账本，同时进行严格 JSON 解析与 capture 编译，并重新计算整文件摘要、字节数和行数；
5. 第二遍结束后再次校验 stat、数据集摘要、字节数和行数；
6. 单行解析或结构错误写入该行终态，不丢弃来源，也不伪造正常 capture/event。

实现只在内存中保留数据集级扫描结果和当前正在处理的物理行/capture；逐行账本直接流式落盘。不得把整个 560 MB 文件、全部原始行或全部解析对象载入内存。

### 4.3 来源引用与稳定 ID

dataset digest 覆盖原始文件全部字节；line digest 覆盖物理行全部字节，包括行末换行。

```text
source_record_id = sha256(
  "source-record-v1\0"
  + dataset_id + "\0"
  + dataset_sha256 + "\0"
  + 1-based line_number + "\0"
  + line_sha256
)
```

绝对路径不参与 ID，移动输入文件不得改变稳定对象标识。

实际 `SourceRecordRefV1` 字段固定为：

```yaml
schema_version:
source_record_id:
dataset_id:
dataset_sha256:
line_number:
byte_offset:
byte_length:
line_sha256:
ingestion_status: PARSED | QUARANTINED
parse_error: null | {code, message}
```

`parse_error` 只描述严格 JSON 摄取错误。JSON 已成功解析、但不满足 capture 结构契约时，来源记录仍为 `PARSED`，结构错误进入对应的 `CaptureQuality.processing_error`。

### 4.4 确定性序列化

- UTF-8；
- key 排序；
- 紧凑分隔符；
- 禁止 NaN 和 Infinity；
- 不改变原始字符串；
- 每条 JSONL 使用一个 LF 结束；
- 文件通过临时文件、`fsync` 和原子替换发布。

运行时间、机器和本地绝对路径只能进入非确定性的 `run_receipt.json`，不得影响业务 artifact digest。

### 4.5 通用实现与 R01 oracle 的边界

核心 compiler 接受调用方提供的 JSONL、`dataset_id`、`source_schema` 和可选 expected SHA-256，不内置 R01 路径、摘要、行数或统计值。validator 同样不内置任何来源 profile，只根据已发布 run 重算通用结构与一致性事实。

本文件第 2 节和第 7 节的 R01 数字只作为文档化的外部验收 oracle，不参与来源解析、结构编译、稳定 ID 或通用一致性判断。若以后需要机器执行 oracle 对比，必须由调用方显式传入独立 expectation/profile，不得把特定数据常量写入核心或通用 validator。

## 5. M1B：Structural Compiler

### 5.1 RequestBoundary

每个 capture 必须独立校验：

```text
len(source_request_ids)
= len(terminal_prefix_depths)
= source_request_count
```

同时要求：

- depths 严格递增；
- 最后一个 depth 等于 `len(messages)`；
- 每个 boundary 落在 assistant message；
- `capture_id` 等于最后一个 `source_request_id`。

已确认全量 R01 包含 9,561 个 boundary，全部满足这些不变量。

首个 boundary 的 terminal assistant 之前的消息标为 `PRE_FIRST_OBSERVED_TERMINAL`。首个 terminal assistant 本身归属于第一个 `OBSERVED_REQUEST_WINDOW`。不能为更早的可见 assistant 伪造 request ID。119 条 compaction 记录只有 capture 级 count/hash，只能保存为 `UNLOCALIZED_COMPACTION_EVIDENCE`，不能推断具体消息位置。

### 5.2 EventOccurrence

事件类型固定为：

```text
SYSTEM
USER
ASSISTANT_MESSAGE
TOOL_CALL
TOOL_RESULT
```

每个事件保存 occurrence ID、source/capture 引用、JSON pointer、消息和调用位置、request boundary、typed content、`visible_payload_utf8_byte_length`、`visible_payload_sha256` 和 `integrity_status`。

`visible_payload_utf8_byte_length` 是去除 reasoning 审计摘要后的可见 payload 经 canonical JSON 编码后的 UTF-8 字节数，不是原始 JSONL 行长，也不是 assistant content 字符数。`visible_payload_sha256` 对同一份 canonical bytes 求摘要。M1 v1 只有在该事件的可见 payload 已完整映射时才产出事件，因此 `integrity_status` 固定为 `COMPLETE`；它不表示原始 wire 日志完整、任务完成、工具成功或不存在 compaction。

assistant message 和它的 tool calls 拆成不同事件，通过 tool call payload 的 `assistant_event_id` 关联。同一个 assistant decision 中的一个或多个 tool call 形成一个 `ActionBatch`。result 到达顺序不能用来推断 tool call 的并行或因果顺序。

list 型 content 必须保留 content block，禁止转成字符串。所有派生 artifact 都不得输出 Base64 Data URL；只允许保存 mime、长度、hash 和原始 JSON pointer。公共报告连这些逐条摘要也不输出，只保存聚合计数。

### 5.3 reasoning_content

旧回流 `reasoning_content`：

- 原文不复制进 EventLog；
- 不参与 lineage、任务识别、环境画像、难度、GT 或模型输入；
- 只保存存在性、长度、SHA-256 和原始 JSON pointer；
- 原文仅通过受控的原始行引用保留审计能力。

assistant event 的 payload 中虽然保留字段名 `reasoning_content`，其值是固定的摘要对象 `{present, utf8_byte_length, sha256, source_json_pointer}`，不是原始 reasoning。事件的 visible byte length 和 visible hash 明确排除该摘要对象。

这与下游 Harbor 的无损采集不冲突。Harbor 可以保存新 rollout 实际返回的 thinking，但任何任务生成、Truth 和 Verifier 都不得依赖它。

### 5.4 Tool call/result pairing

只使用显式 `tool_call_id`，不得按相邻位置猜测。状态固定为：

```text
MATCHED_ONE_TO_ONE
RESULT_NOT_OBSERVED
DUPLICATE_CALL_ID
DUPLICATE_RESULT
ORPHAN_RESULT
NAME_MISMATCH
RESULT_BEFORE_CALL
INVALID_CALL_ARGUMENTS
```

所有 call 和 result 都以 occurrence 保存，禁止使用单值字典覆盖重复 ID。`RESULT_NOT_OBSERVED` 只表示当前 capture 未观察到结果，不能解释成工具没有执行。

### 5.5 多维质量状态

禁止产生一个全局 `valid`：

```text
processing_status:
  COMPLETE | PARTIAL | QUARANTINED

boundary_status
tool_pairing_applicable
tool_pairing_statuses[]
tool_schema_status
terminal_status
privacy_status
compaction_status
processing_error
reason_codes[]
```

`leaf_response_status=completed` 只说明捕获响应结束，不能解释为任务完成或回答正确。

`processing_status` 只描述编译器是否忠实产出可见结构，不评价原轨迹质量。缺失 observation、schema conflict、inferred schema、pending tool call、compaction 和 input truncation 都进入独立质量轴；只要可见结构完整落盘，仍为 `COMPLETE`。JSON 或关键 boundary envelope 无法可信编译时为 `QUARANTINED`，且不得产出伪造 capture/event。`PARTIAL` 只保留给未来确有安全子树可落盘、但当前契约明确允许缺失另一子树的情况；M1 v1 不用它掩盖异常。

`processing_error` 在 `COMPLETE` 时必须为 `null`。在 `QUARANTINED` 时保存稳定 `code` 和诊断 `message`：严格 JSON 错误与 `SourceRecordRef.parse_error` 对齐；capture 结构错误使用 `CaptureCompileError` 的 reason code。`reason_codes` 保存同一稳定 code，供聚合和筛选使用。错误终态只写 `source_records.jsonl` 与 `capture_quality.jsonl`，不得伪造 `NormalizedCapture`、boundary 或 event。

### 5.6 Capture 内部属性图

M1B 已经建图，但不引入图数据库，也不重复输出一份可能失配的 graph dump。规范化表是节点，稳定 ID 和外键是边：

```mermaid
flowchart LR
    SR[SourceRecordRef] -->|materializes| C[NormalizedCapture]
    C -->|contains| RB[RequestBoundary]
    C -->|contains ordered occurrences| E[EventOccurrence]
    RB -->|owns / terminates at| E
    A[Assistant Event] -->|emits| AB[ActionBatch]
    AB -->|contains| TC[Tool Call Event]
    TC -->|grouped by explicit ID| PR[ToolPairingRecord]
    TR[Tool Result Event] -->|grouped by explicit ID| PR
```

其中：

- `EventOccurrence.sequence_number` 表示规范化可见顺序；
- `RequestBoundary.terminal_event_id` 与 `EventOccurrence.request_boundary_id` 表示 boundary 边；
- `ActionBatch.assistant_event_id/tool_call_event_ids` 表示 decision 边；
- `ToolPairingRecord.call_event_ids/result_event_ids` 保留全部 occurrence；
- 所有节点都可经 `source_record_id` 和 `source_json_pointer` 回到原始物理行。

M1C 只新增跨 capture 的 lineage edge，不重写这些节点。

## 6. M1A、M1B 输出

```text
artifacts/r01/<content_addressed_run_id>/
├── source_manifest.json
├── private/
│   ├── source_records.jsonl
│   ├── captures.jsonl
│   ├── request_boundaries.jsonl
│   ├── event_occurrences.jsonl
│   ├── action_batches.jsonl
│   ├── tool_pairings.jsonl
│   ├── tool_catalogs.jsonl
│   └── capture_quality.jsonl
├── reports/
│   └── attrition_report.json
├── artifact_manifest.json
└── run_receipt.json
```

`private/` 可以保存 system、user、assistant 可见内容、tool arguments 和 observation 的完整 typed 形态，但不得保存 reasoning 原文。`reports/` 不得包含原始 ID、原文、路径、URL、data URL 或敏感 literal。

`source_manifest.json` 和 `artifact_manifest.json` 都保存 `source_schema`，内容寻址 run ID 也绑定该字段，防止同一原始字节被不同语义契约误用为同一个 run。

`artifact_manifest.json` 的 `files` 只列出十个确定性业务文件：`source_manifest.json`、八个 `private/*.jsonl` 和 `reports/attrition_report.json`。它不列出自身，以避免自引用摘要；也不列出含时间、机器和绝对路径的 `run_receipt.json`，避免非确定信息改变业务清单。`run_receipt.json` 反向保存 `artifact_manifest.json` 的 SHA-256。独立 validator 仍会检查这两个文件以及完整目录 inventory；“不进入 files”不表示不校验。

真实输出不进入 Git。Git 只保存重新构造的虚构 fixture。

## 7. 全量验收基线

### 7.1 独立 validator

编译成功后，使用 CLI 输出的内容寻址 run 目录执行：

```bash
uv run python scripts/validate_m1_run.py <content_addressed_run_dir>
```

validator 独立读取已发布文件，不调用 compiler 重建期望结果。它校验目录 inventory、manifest digest/size/record count、稳定 ID、source/capture/boundary/event/ActionBatch/pairing 外键、event visible length/hash/integrity、reasoning 摘要形态、公共计数重算以及 `run_receipt` 到 artifact manifest 的绑定。该命令对所有同契约 run 使用同一逻辑，不包含 R01 分支或常量。

### 7.2 R01 外部 oracle

下列数字不参与 compiler 分支或通用 validator 逻辑，只用于确认冻结 R01 的完整运行没有静默漏数：

```text
physical lines                   = 1,683
parsed captures                  = 1,683
request boundaries               = 9,561
unique requests                  = 6,301
system events                    = 6,220
user events                      = 14,407
assistant message events         = 48,667
tool call events                 = 56,424
tool result events               = 50,140
all event occurrences            = 175,858
pre-first scoped events          = 127,746
request-window scoped events     = 48,112
action batches                   = 42,318
tool pairing records             = 56,424
strict one-to-one pairings       = 49,800
captures with unobserved results = 1,248
unobserved call IDs              = 6,474
duplicate result groups          = 150
extra result occurrences         = 190
tool definitions                 = 18,566
tool definition conflicts        = 349
captures with inferred schemas   = 280
inferred tool name annotations   = 566
compaction captures              = 119
truncated input captures         = 35
terminal text outcomes           = 472
terminal tool-call pending       = 1,211
compiler COMPLETE                = 1,683
compiler PARTIAL                 = 0
compiler QUARANTINED             = 0
```

完成条件：

- 1,683 条 source line 全部有结构终态；
- 两遍扫描的逐行账本、文件 stat、总字节数和 digest 闭合；
- 两次运行的确定性 artifacts 字节一致；
- 冻结 R01 全量运行峰值内存低于 512 MiB，且实现不保留全部原始行或全部解析对象；
- 私有 artifact 与公共报告物理分离；
- 派生内容不包含 reasoning 原文；
- 测试、静态检查和全量不变量全部通过。

## 8. 后续阶段边界

### 8.1 M1C：Request/Capture Graph

高可信 Grade A 关系来自共享 source request、显式 request successor、相同 raw request hash 或完整重复 capture。

`NORMALIZED_VISIBLE_PREFIX_OF` 只能作为 Grade B 投影关系。它不能单独用于因果 lineage、硬去重或主分布；只共享 system、thread/account 或时间接近时不建边。

### 8.2 M1D：QueryTurn

M1D 先确定性区分真实 query、附件上下文、Harness 包装、系统注入、工具反馈和中断控制，只输出 `UserBlock`、`QueryTurn` 与结构性 `ThreadTurnGraph`。

### 8.3 M2：TaskEpisode 与画像

M2 才允许通过两次独立、封闭枚举的语义提取建立：

```text
CONTINUES
REFINES
DEPENDS_ON
CORRECTS
CANCELS
BRANCHES_FROM
NEW_TASK
AMBIGUOUS
```

两次结果和证据不一致时必须 `ABSTAINED`，不得重复调用直到得到一致答案。自动画像完成后，由项目负责人一次性冻结首个业务 Domain/task family，不做逐样本人工 review。

### 8.4 Harbor 交接

正确时序是：

```text
TaskWorldCandidateRevision
→ G0–G5 + G7
→ Public/Control 物理分包
→ RunnableTaskWorldCandidateBundle
→ Harbor rollout + independent evaluation
→ G6
→ CertifiedTaskWorldRelease
```

Harbor smoke 与 R01 处理可以并行开发，但在上述候选包契约冻结前不得拼接。
