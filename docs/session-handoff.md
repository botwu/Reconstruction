# TraceForge 开发会话交接

版本：M2 前置 `UserTextProjection` 检查点

日期：2026-09-03

状态：M1A/M1B **v4**、M1C v2（重绑定 v4 run）、M1D v1 与 M2 前置 `UserTextProjection` v1 正式通过；已验收代码停止在 `UserTextProjection`（提交 `1de39ae`）；M1 主线与 M2 前置硬门在 R01 上全部闭合；M2 已拆为 ①–④ 四个子模块（§11），① `TurnEvidence` 规格草案 v0.1 已起草，下一步为评审冻结

## 1. 本文用途

本文是新开发会话的当前检查点和权威索引，不替代总体计划或模块规格。新会话按以下顺序阅读：

1. [`../AGENTS.md`](../AGENTS.md)：唯一开发规范；
2. 本文：当前事实、停止线和下一步；
3. [`background-and-goals.md`](background-and-goals.md)：背景、目标和主张边界；
4. [`overall-plan.md`](overall-plan.md)：完整架构、模块和阶段门；
5. [`r01-processing-spec.md`](r01-processing-spec.md)：M1 精确输入、算法、契约与验收；
6. [`r01-m1b-v4-validation.md`](r01-m1b-v4-validation.md)：M1A/M1B **v4** 正式验收证据（当前有效）；[`r01-m1-v3-validation.md`](r01-m1-v3-validation.md)：v3 历史验收；
7. [`m1c-processing-spec.md`](m1c-processing-spec.md) 与 [`r01-m1c-validation.md`](r01-m1c-validation.md)（§0′ 为当前有效结论）：M1C 契约与正式验收证据；
   [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md)（R1–R9；R4/R5/R7/R8 随 v4 关闭，R9 随 M1D 提交关闭）与 [`m1c-known-items.md`](m1c-known-items.md)（K1–K3）：已知项登记；K1/K2 已由 M1C v2 从根因关闭，**上游不透明摘要不入任何关系证据**是 v2 起的通用原则；
8. [`m1d-processing-spec.md`](m1d-processing-spec.md)（v0.3，实现同步稿）与 [`r01-m1d-validation.md`](r01-m1d-validation.md)：M1D 契约与正式验收证据；[`m1d-review-20260902.md`](m1d-review-20260902.md)：已处置的评审意见（存档）；
9. [`m2-source-projection-spec.md`](m2-source-projection-spec.md)（v0.3，已冻结并已实现）与 [`r01-user-text-projection-validation.md`](r01-user-text-projection-validation.md)：M2 前置 `UserTextProjection` 契约与正式验收证据（含同批 validator 公共原语重构的重新验收）；[`m2-source-projection-review-20260903.md`](m2-source-projection-review-20260903.md)：已处置的评审意见（存档）；
10. [`m2-turn-evidence-spec.md`](m2-turn-evidence-spec.md)（v0.1 草案，待评审冻结）：M2 ① `TurnEvidence` 契约、固定函数与验收向量；M2 四子模块拆解见 §11；
11. [`reference-repositories.md`](reference-repositories.md) 与 [`implementation-sources.md`](implementation-sources.md)：参考逻辑和迁移边界。

如果本文与模块规格冲突，以 `r01-processing-spec.md`（M1A/B）、`m1c-processing-spec.md`（M1C）、`m1d-processing-spec.md`（M1D）和 `m2-source-projection-spec.md`（`UserTextProjection`）的契约为准；如果与开发纪律冲突，以 `AGENTS.md` 为准。

## 2. 背景与最终目标

真实企业 Agent 回流包含用户请求、模型回复、工具调用、observation、上下文包装和部分运行元信息，但通常没有标准答案、完整用户环境、可靠成功标签或清晰责任归因。TraceForge 不把回流直接改写成训练样本，也不声称恢复真实用户环境。

项目最终目标是：

> 从真实回流中提取可审计的任务与环境约束，将其编译成 Task 与 World 严格绑定、可执行、可验证、可调难度，并位于目标模型能力边界附近的训练和评测单元。

核心立场：回流提供生成约束，不提供 Ground Truth；Truth 必须来自可控 World，Reference 只证明至少一条可达路径，Verifier 独立检查结果；强模型 rollout 用于验证、诊断和难度校准，不能通过投票生成 GT。

## 3. 总体链路与当前停点

```mermaid
flowchart LR
    A[真实回流 JSONL] --> B[M1A Source Adapter]
    B --> C[M1B Capture 内结构编译]
    C --> C1[EventLog / ActionBatch / Pairing]
    C1 --> D[M1C 跨 capture lineage]
    C1 --> E[M1D 结构型 QueryTurn]
    C1 --> P[M2 前置 UserTextProjection]
    E -. 可选回指 .-> P
    P --> S{{当前 STOP}}
    S -. M2 ① TurnEvidence 规格 v0.1 待评审冻结 .-> F[M2 ①→④ 子模块]
    F --> G[M3 Task-World 联合合成]
    G --> H[M4 认证、rollout 闭合与定向修复]
    H --> I[M5 六维难度校准]
    I --> J[CertifiedTaskWorldRelease]
```

当前只完成实线部分：M1A/B v4 → M1C v2、M1D v1、M2 前置 `UserTextProjection` v1 均在 R01 上正式验收，四条 run 以内容寻址身份链式绑定（`47cfac20…` ← (`6be45e01…`, `84d826b3…`)；`84d826b3…` ← `6be45e01…` → `9ff708d9…`）。M1D 与 M1C 并列消费 M1B run，互不依赖（M1D 默认不启用 M1C 做跨 capture 线程图，规格 §11 D-c）；`UserTextProjection` 消费 M1B run，M1D run 只是可选回指输入。仓库中没有 M2、World、认证、难度或 Harbor 的空壳实现。

## 4. 仓库与冻结输入

```text
本地仓库：/Users/wujian1/Downloads/traceforge（Mac）
        /mnt/afs_toolcall/wujian1/Projects/workspace/traceforge（验收机，AFS 共享盘，分支 m1c-lineage）
远程：git@gitlab.sh.sensetime.com:wujian1/traceforge.git
分支：main
R01（Mac）：/Users/wujian1/Downloads/seed2traj/return_data/four_batch/by-rubric/R01.jsonl
R01（验收机全量真源）：/mnt/afs_toolcall/juxiaolong1/Projects/DataFilter_v2/gpt56sol/domain/by-rubric/R01.jsonl
  （验收机仓内 return_data/.../R01.jsonl 为 368 行截断副本，禁止用作输入）
dataset_id：r01-four-batch-202607-v1
source_schema：traceforge.restored-long-capture.v1
字节数：560,481,884
物理行数：1,683
SHA-256：3832d8aa4ecd577636ce67b56d8798d9fb6311662a7bbce65814e5e8d260d4d8
```

必须保持以下区别：

- 一条 JSONL 是 capture 快照，不是 Session、request 或 task；
- `thread_id/account_id` 只形成 956 个候选比较组，不能直接充当 lineage；
- R01 是来源 cohort 和检索 rubric，不是业务 Domain；
- 当前统计是 capture 结构与损耗画像，不是 Episode、任务或真实环境分布。

## 5. 当前代码冻结点与契约版本

M1A/M1B **v4** 正式全量运行绑定的代码冻结点（`trajectory/` 代码 = 提交 `8f6f65c`；run 在其后代 `943ba92` 上构建，二者 `trajectory/` 无差异）：

```text
Git commit：943ba922270a368440ad5a81028da4beda58508b
Git tree：aa083fbe9d46fba3b786e2d4d375967034b78576
dirty：false
TraceForge：0.3.0
compiler contract：trajectory-compiler-m1ab-v4
ToolPairing schema：traceforge.tool-pairing.v3
```

（v3 冻结点 `c3c0a8f` / contract `trajectory-compiler-m1ab-v3` 为历史结论，v4 validator 对 v3 run 按设计 fail-closed。）

M1C v2 重绑定 v4 run 的正式运行绑定同一提交 `943ba92`（`lineage/` 相对 `fcff8cf` 仅 `reader.py` 文档字符串 2 行，无运行时变化）：

```text
lineage contract：lineage-compiler-m1c-v2
```

M1D v1 正式全量运行绑定的代码冻结点：

```text
Git commit：1c588bed6cfbb696611823ce427fdaa8fd06e249
Git tree：2bc0f4bc4da4eadeb3fbcfa3a187618d3e6669f8
dirty：false
query-turn contract：query-turn-compiler-m1d-v1
```

**validator 公共原语重构 `f70710f`（2026-09-03）** 触碰了 `trajectory/`、`lineage/`、`query_turns/`：把三个 validator 各自复制的物理层原语抽到 `trajectory/run_validation.py`，`artifact_entry_dicts` 收拢到 `trajectory/artifacts.py`；issue code/location/message 逐字不变。它已在干净克隆 `1de39ae` 上重新验收：重构后的三个 validator 对冻结 run `6be45e01…` / `9ff708d9…` / `84d826b3…` 均 `ok=true` 且计数不变；重构后重建 M1C、M1D 分别复现 `9ff708d9…`、`84d826b3…` 且排除 `run_receipt.json` 后逐字节相同（[`r01-user-text-projection-validation.md`](r01-user-text-projection-validation.md) §6）。上述三个冻结点的正式结论因此继续有效，其"代码无变化"的比对基线相应改为：`trajectory/` 相对 `f70710f`、`lineage/` 相对 `f70710f`、`query_turns/` 相对 `f70710f`。

M2 前置 `UserTextProjection` v1 正式全量运行绑定的代码冻结点：

```text
Git commit：1de39aeda894b55cc84d1fb8f8923e2b4c57380f
Git tree：8fbd9729996bd1b2621ed27092db6cf3f1ef274a
dirty：false
projection contract：user-text-projection-v1
```

交接文档自身会形成后续纯文档提交；新会话必须用 `git log` 确认 HEAD 是上述冻结点的后代，并确认 `1de39ae` 之后没有未重新验收的 `src/traceforge/trajectory/`、`src/traceforge/lineage/`、`src/traceforge/query_turns/`、`src/traceforge/source_projection/`、`pyproject.toml` 或 `uv.lock` 变化。

## 6. M1 已实现能力

M1A/B 位于 `src/traceforge/trajectory/`（字段与算法只在 `r01-processing-spec.md` 定义）；M1C 位于 `src/traceforge/lineage/`（`m1c-processing-spec.md`）；M1D 位于 `src/traceforge/query_turns/`（`m1d-processing-spec.md`）；M2 前置 `UserTextProjection` 位于 `src/traceforge/source_projection/`（`m2-source-projection-spec.md`）。四个模块的 validator 共用 `trajectory/run_validation.py` 的物理层原语（manifest 自洽、逐文件摘要、目录清单、回执/provenance、控制文件路径扫描、canonical JSONL 读入、双向 bijection），各自只保留语义层。M1A/B 能力边界如下：

- 单一显式 restored-long source adapter，不对任意 JSON 猜格式；
- 两遍流式扫描、stat/digest/字节数/行数闭合和逐行来源账本；
- 内容寻址 run、稳定 ID、canonical JSON/JSONL、原子发布和 Git provenance receipt；
- `NormalizedCapture`、`RequestBoundary` 与 occurrence-preserving EventLog；
- assistant decision、tool call、tool result 拆分，ActionBatch 语义固定为 `UNKNOWN`；
- pairing 只按显式 `tool_call_id`，异常或重复组不生成伪精确 matched 边；
- 五类 event payload 由唯一 typed reader 解释；
- 多轴质量状态，不用一个含糊的 `valid`；
- 任意深度 reasoning 原文递归摘要，Data URL 正文不进入派生产物；
- private 事件表与 public 聚合报告物理分离；
- 独立 validator 重算 boundary、ActionBatch、pairing、quality、report、typed payload 与隐私不变量。

M1D（`query-turn-compiler-m1d-v1`）在已发布 M1B run 之上：先跑 M1B 权威 validator 再消费；以 `sequence_number` 升序的**可观测事件流**为工作对象，boundary 只作证据锚点；键于 assistant 事件建 `AgentStep`，工具观测**按 `ToolPairingRecordV3` 配对归属**而非位置；可定位 USER 段起 `OBSERVED_ROOTED` 回合，窗口以 assistant 起头则起 `PREFIX_ROOTED` 回合（根在不可定位前缀，显式暴露）；末步有 ActionBatch 即 `INCOMPLETE`、否则按 typed reader 冻结标量判 `TEXT/EMPTY_OUTCOME`；capture 内相邻回合连 `STRUCTURAL_NEXT_TURN` 边；每 capture 一条记账（前缀/观测逐 kind 计数、孤儿观测、compaction）。零模型调用、零语义关系。validator 为两层信任边界：篡改检测（同一纯 fold 重建 + 六表双向 bijection）与不经 fold 的正交不变量（观测事件分区守恒、记账对账、末 assistant 与 M1B `terminal_status` 交叉核对、报告由表重算）。

`UserTextProjection`（`user-text-projection-v1`）在已发布 M1B run（+ 可选已发布 M1D run）之上：先跑上游权威 validator 再消费；对**每条 USER 事件**恰产一条 `UserTextAnnotationV1`——`locality`（`event_scope` 一一映射为 `OBSERVED` / `PREFIX_UNLOCALIZED`）、`content_form`（透传 typed reader / 隐私 envelope 种类：`TEXT_STRING` / `TEXT_WITH_DATA_URL_SEGMENTS` / `DATA_URL_SUMMARY` / `CONTENT_BLOCKS`）、`text_class`（去空白后的开头是否以**冻结白名单**内的开标签起头：`PLAIN_USER_TEXT` / `EMPTY_TEXT` / `HARNESS_CONTEXT`(A7) / `HARNESS_CAPABILITY`(B2) / `CONTROL_SIGNAL`(C3) / `UNKNOWN_TAGGED` / `NO_LEADING_TEXT`）、`leading_tag`（仅白名单字面量；`UNKNOWN_TAGGED` 恒空）、绑定 M1D 时观测事件回指 `user_block_id`。判定窗口 = `lstrip()` 后前 256 码点，文法 `^<([A-Za-z_][A-Za-z0-9_.:-]*)(?=[\s>/])`，大小写敏感；分类只有一个实现 `contracts.classify_leading_text`。零模型调用、零语义推断；产物不含任何正文；公共报告只有固定 allowlist 的 23 项计数（含门④三个诚实分母）。validator 三参（绑定 M1D 时 oracle 必填、未绑定时不得提供），两层信任边界：同一纯 fold 的双向 bijection 抓篡改；不经 fold/分类函数的正交不变量（USER 事件一一覆盖、来源事实逐条一致、`leading_tag` 闭合、类别与开头结构相容、回指与 UserBlock 索引及 capture 一致、报告由表重算、`captures_with_observed_plain_user_text ≤ 有 UserBlock 的 capture 数`）抓派生缺陷。

## 7. 本轮审计闭环

v1、v2 验收报告的正式结论均已撤销，只保留历史正常路径事实。v3 闭合三类 P1：

1. typed payload、terminal quality/report 与 tool arguments pointer 可同步重签假绿；
2. URL、路径、query 或凭据形式的 dataset ID 可进入公共报告；
3. `NAME_MISMATCH`、`RESULT_BEFORE_CALL`、`INVALID_CALL_ARGUMENTS` 等异常 1:1 组仍生成 `matched_*`。

正式验收前的独立红队又发现同属第 1 类的 Data URL envelope 变体：把内外审计长度同步伪造为 0 可把非空终态伪报为空。代码冻结提交 `c3c0a8f` 增加两个独立不变量：合法 Data URL envelope 必须为正长度；terminal 空非空由 value 形态重算，而不是由审计长度决定。纯 Data URL、混合文本、零长度和正长度绕过均已 fail-closed。

对抗审计另发现两项**带来源接受、暂不修改冻结代码**的已知项，登记在 [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md)：R1（隐私脱敏续行判定只认 CR/LF，裸空格/制表符致 base64 尾段 fail-open；R01 未触发、产物 0 base64）与 R4（截断轴把源自报 `input_truncated=False` 渲染成 `OBSERVED_NOT_TRUNCATED`，属规格 §5.5/§7 已批准的设计张力）。两项均未使任一验收门变红；任何收紧都必须走单独 reopen 与重新验收。2026-09-01 会话已把 gate 4/6 及 gate 1/5/7 现场升级为 live 亲证，证据见 [`r01-m1-v3-validation.md`](r01-m1-v3-validation.md) §8。

## 8. 正式验收证据

### 8.0 M1A/M1B v4（2026-09-03，当前有效）

```text
run ID：6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed
artifact manifest SHA-256：179b82efca85fc6132bfa806d45b081729b0d5627878b0d517e978070fe99eb1
运行 A：回执 82.359419 秒，RSS 37,945,344 bytes
运行 B：回执 83.436668 秒，RSS 37,851,136 bytes
validator A/B：ok=true；10 files；1,683 lines；175,858 events
递归 diff：排除 run_receipt.json 后无差异；与审核方会话单跑产物逐字节一致
反向核对：v4 validator 对 v3 run 519a86d3… 报 SCHEMA/CONTRACT 版本不匹配（预期）
v3→v4 字节差异逐文件归因于 schema 升级、枚举/字段改名，无业务逻辑变化
```

```text
artifacts/r01/6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed/
artifacts/r01/acceptance/6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed/
```

长期可提交摘要见 `r01-m1b-v4-validation.md`。v3 run `519a86d3…`（manifest `9dcdb083…`）与其证据保留为历史（`r01-m1-v3-validation.md`）。

### 8.1 M1C v2 重绑定 v4 run（2026-09-03，当前有效）

```text
lineage run ID：9ff708d98773a175f9552b09ea1171f16a6ba3bbe4a4ffbcc4fdec2612acb17b
绑定 M1B run：6be45e01…（manifest SHA-256 179b82ef…）
lineage artifact manifest SHA-256：83934c6c4f9366dd7046e136161f78367a082f5748eb404532fa4816cef9eaeb
运行 A：112.479697 秒，RSS 244,387,840 bytes
运行 B：114.908946 秒，RSS 244,424,704 bytes
validator A/B：ok=true；5 files；SHARED 1,671 / SUCCESSOR 4,994 / DUPLICATE 0，与 §4.4 逐项一致
递归 diff：排除 run_receipt.json 后无差异
相对 978c0347…（绑定 v3 run）：全部文件字节不同（ID 以 m1b_run_id 命名空间化），剥离 ID 后节点/边集合完全相同
```

```text
artifacts/r01/lineage/9ff708d98773a175f9552b09ea1171f16a6ba3bbe4a4ffbcc4fdec2612acb17b/
artifacts/r01/acceptance_m1c/9ff708d98773a175f9552b09ea1171f16a6ba3bbe4a4ffbcc4fdec2612acb17b/
```

长期可提交摘要见 `r01-m1c-validation.md` §0′；旧 run `978c0347…` 保留为历史。§7 登记的两项环境已知项仍有效：慢盘上 `git status` 超过 provenance 的 5 秒超时会使就地 run 被 validator 拒绝（正式 run 一律在本地盘干净克隆上执行）；验收机仓内 `return_data/.../R01.jsonl` 为不完整副本，全量真源路径见 §4。

### 8.2 M1D v1（2026-09-03，当前有效）

```text
query-turn run ID：84d826b3d7abddffb5f8fde28d7e5592590720bd6909c24a8b17b7f0c70b66ea
绑定 M1B run：6be45e01…（manifest SHA-256 179b82ef…）
M1D artifact manifest SHA-256：56a3befe44bec087a52aaa680f592eb71e196c7d7bd946e47c172540cb190bfe
运行 A：回执 211.258606 秒，RSS 244,621,312 bytes
运行 B：回执 202.558055 秒，RSS 244,928,512 bytes
validator A/B（双参）：ok=true；8 files；13 项计数与审核向量及 v3 run 上的 M1D 计数逐项一致
递归 diff：排除 run_receipt.json 后无差异；五张业务表与审核方会话产物逐字节相同
反向核对：以 v3 M1B run 作 oracle → M1B_INPUT_INVALID（fail-closed）
测试：273 passed（M1D 55）；Ruff lint/format：通过
```

```text
artifacts/r01/query_turns/84d826b3d7abddffb5f8fde28d7e5592590720bd6909c24a8b17b7f0c70b66ea/
artifacts/r01/acceptance_m1d/84d826b3d7abddffb5f8fde28d7e5592590720bd6909c24a8b17b7f0c70b66ea/
```

长期可提交摘要见 `r01-m1d-validation.md`。真实 run 和证据均被 Git 忽略。

### 8.3 M2 前置 `UserTextProjection` v1（2026-09-03，当前有效）

```text
projection run ID（绑定 M1D）：47cfac20b89ed066f388da6679cb53f745e6a0392cd5f6e459e869c7076f689c
绑定 M1B run：6be45e01…（manifest SHA-256 179b82ef…）；绑定 M1D run：84d826b3…（manifest SHA-256 56a3befe…）
projection artifact manifest SHA-256：b45193276fe0a83d45bf5fa8bd4bc8e6edc303aad493ec3c32fc33894a01ceab
projection run ID（未绑定 M1D）：9dc26f2fc2ae2ce50e002834597142b4accf5c0f7e410ff798c691b4fa05c627（注解表除 user_block_id 外逐字节相同，报告相同）
运行 A：回执 397.426230 秒，RSS 257,028,096 bytes
运行 B：回执 398.604551 秒，RSS 256,225,280 bytes
运行 C（未绑定）：回执 193.778914 秒，RSS 245,571,584 bytes
validator A/B/C（三参）：ok=true；3 files；23 项计数与 12 个标签计数与规格 §6 向量逐项相等
递归 diff：A/B 排除 run_receipt.json 后无差异
反向核对：绑定 run 缺 M1D oracle → M1D_RUN_REQUIRED；未绑定 run 给 M1D oracle → M1D_BINDING_MISMATCH；v3 M1B run 作 oracle → M1B_INPUT_INVALID + M1D_INPUT_INVALID
重构 f70710f 重新验收：三 validator 重验冻结 run 全 ok；重建 M1C/M1D 复现同 run ID、字节相同
测试：352 passed（source_projection 77 + CLI 2 新增）；Ruff lint/format：通过
```

```text
artifacts/r01/source_projection/47cfac20b89ed066f388da6679cb53f745e6a0392cd5f6e459e869c7076f689c/
artifacts/r01/source_projection/9dc26f2fc2ae2ce50e002834597142b4accf5c0f7e410ff798c691b4fa05c627/
artifacts/r01/acceptance_utp/47cfac20b89ed066f388da6679cb53f745e6a0392cd5f6e459e869c7076f689c/
```

长期可提交摘要见 `r01-user-text-projection-validation.md`。真实 run 和证据均被 Git 忽略。

## 9. 当前关键数据事实

| 观察 | 结果 | 含义 |
| --- | ---: | --- |
| capture | 1,683 | 全部有 M1 结构终态 |
| RequestBoundary occurrence | 9,561 | 跨 capture 存在明显重叠 |
| 唯一 source request | 6,301 | boundary 不能直接当独立 request 样本 |
| EventOccurrence | 175,858 | 事件类型与 scope 守恒 |
| 严格一对一 pairing | 49,800 | 其余组保留异常或未观测状态 |
| 含未观测 result 的 capture | 1,248 | `COMPLETE` 不等于工具或任务成功 |
| comparison partition | 956 | 只能作为阻塞式候选提示 |
| 保守结构候选 | 353 | 仍然不是 task，必须先做 lineage 与 QueryTurn |
| `SHARED_SOURCE_REQUEST` 边（M1C） | 1,671 | 全部为"共享前缀后分叉"的兄弟 capture，0 例跨候选组 |
| `EXPLICIT_REQUEST_SUCCESSOR` 边（M1C） | 4,994 | 请求级真森林：入度 ≤ 1、最大出度 13、无环 |
| `COMPLETE_DUPLICATE_CAPTURE` 边（M1C） | 0 | 契约保留，R01 无完整重复 capture |
| `QueryTurn`（M1D） | 2,610 = 1,683 `PREFIX_ROOTED` + 927 `OBSERVED_ROOTED` | 每 capture 恒一个根在前缀的首回合；仅 927 个回合有可定位用户根 |
| `AgentStep` / `UserBlock` / 结构边（M1D） | 14,375 / 927 / 927 | AgentStep 是唯一始终可定位的单元；边数 = 回合数 − capture 数 |
| `COMPLETE` / `INCOMPLETE` 回合（M1D） | 1,200 / 1,410 | R01 无 `EMPTY_OUTCOME`；INCOMPLETE 全部为末步工具调用步或 USER-only 回合 |
| 孤儿工具观测 / 含 compaction capture（M1D） | 324 / 119 | 孤儿=配对到前缀调用的观测结果，集中在少数 capture |
| USER 事件（`UserTextProjection`） | 14,407 = 1,182 `OBSERVED` + 13,225 `PREFIX_UNLOCALIZED` | 每条恰一条结构注解；R01 无 `DATA_URL_SUMMARY`/`CONTENT_BLOCKS` 形态，2 条分段隐私 envelope 首段为文本 |
| `PLAIN_USER_TEXT` / Harness 注入 / 控制信号 / 未知标签 / 空正文 | 11,694 / 2,559（context 2,501 + capability 58）/ 96 / 54 / 4 | 约 19% 的 USER 事件按结构不是用户文本；`skill`（41 次）等 54 条未知标签只计数、不落标签名 |
| 普通用户文本的三个分母 | 1,680 / 1,350（只在前缀）/ 330（观测窗口内） | 只用可定位证据时 `ObservedTaskDistribution` 分母 = 330（≤ M1D 有 UserBlock 的 342 个 capture） |

不要将 `processing_status=COMPLETE` 解读为任务完成，也不要把 353 个结构候选直接交给任务合成。M1C 的边是 Control 侧来源解析信息，不进入任何 Public 视图，也不用于硬去重。M1D 的回合是**结构**分组：`PREFIX_ROOTED` 回合的用户意图不在观测窗口内，`root_status` 必须随回合一起传给下游。

M1D 规格 §0 的结构探针与正式 run 共同给出对 M2 极关键的事实：14,407 个 USER 事件中 13,225 落在不可定位的 `PRE_FIRST_OBSERVED_TERMINAL` 前缀；100% capture 存在不可定位前缀，80% 的 capture 观测窗口内没有可定位 USER，只有 927 个回合有可定位用户根。`UserTextProjection` 正式 run 把这一事实量化为可消费的注解：1,350 个 capture 的普通用户文本**只**在前缀、330 个在观测窗口内有普通用户文本。M2 使用前缀证据必须携带 `intent_locality=PREFIX_ONLY` 单列（规格 §2.5），否则要么分母塌陷到 330，要么把不可定位证据混进有根 Episode。

## 10. 当前硬停止线

未经下一阶段规格审核，不得实现或宣称存在：

- Grade-B `NORMALIZED_VISIBLE_PREFIX_OF`、已删除的 `IDENTICAL_RAW_REQUEST_HASH` 或任何非 3 类 Grade-A 的 lineage 关系；
- 任何以上游不透明摘要（`raw_request_hash`、`target_hash`、`input_truncated` 等自报值）为依据的关系或 `OBSERVED_*` 状态；
- 以 M1C 边为依据的 capture 硬去重、合并或"选最长丢其余"；
- 任何语义关系（`CONTINUES/REFINES/…`）、跨 capture 线程合并、前缀内容的语义还原，或任何 TaskEpisode（M1D 只产 `STRUCTURAL_NEXT_TURN`）；
- ObservedTaskDistribution 或 EnvironmentExposureProfile；
- 业务 Domain、World、Truth、Reference、Verifier；
- 可解性、难度、模型边界或自动纠正；
- Harbor、AGS、Hermes 或 TokenHub runtime integration。

Harbor 是将来 `RunnableTaskWorldCandidateBundle` 的 rollout 执行层，不是 TraceForge core，也不是当前 M1 的前置依赖。Harbor 部分由项目负责人另行负责。

## 11. 下一模块：M2 规格起草（`TaskEpisode` 与画像）

M1C 的四门（候选组仅 `BLOCKING_HINT_ONLY`、Grade-A 组盲、validator 独立核验分区、`raw_request_hash` 格式契约）与 M1D 的五门（纯确定性零 LLM、只在观测窗口分类、结构信号不读正文、工具观测按配对归属、Public/Control 隔离）分别在 [`m1c-processing-spec.md`](m1c-processing-spec.md) §3、[`m1d-processing-spec.md`](m1d-processing-spec.md) §3 冻结并经各自验收报告验收，此处不再复述。

**M1B v4 reopen 已闭合（2026-09-03）。** 四项已知项归两个根因一次处理：(a) 上游自报值不得渲染成观测（R4，`SOURCE_REPORTS_*`）；(b) 契约不保留无生产者的状态、业务规则只有一个定义（R7 删 `PARTIAL`、R5 严格匹配谓词抽为共用纯函数、R8 改名 `visible_payload_envelope_utf8_byte_length`）。在验收机全量真源上两次重编译复现 run `6be45e01…`，见 §8.0；M1C v2 随之重绑定（§8.1）；M1D 在 v4 run 上的 13 项计数与 v3 run 完全一致，证实 v4 对 M1D 字节中性。

**M1D 评审整改已闭合（2026-09-03，规格 v0.3）。** [`m1d-review-20260902.md`](m1d-review-20260902.md) 的 D1–D5 与 §3 三项决定全部处置：validator 改为两层信任边界（篡改检测调用同一纯 fold；fold 缺陷由不经 fold 的正交不变量层检出，含 D1 分区守恒），删除 `processing_status` 透传与 `QUARANTINED` 死分支（D4/D5），`parallel_semantics` 无 batch 取 `null`（D2），accounting 字段以实现为准（D3），终态交叉核对对象改为末 assistant（D-f），配对重复不设 tripwire、空观测流以防御性测试固定（§3）。该评审文档保留为存档。

**来源投影规格评审已完成一轮（2026-09-03，规格 v0.2）。** [`m2-source-projection-review-20260903.md`](m2-source-projection-review-20260903.md) 对照 M1B v4 代码给出 P1–P8 修正与 D3/D5–D9 决定，已全部写入 [`m2-source-projection-spec.md`](m2-source-projection-spec.md) v0.2：删除无生产者的 `QUARANTINED`（M1B validator 已对每个事件跑过 typed reader；"非字符串 value"是合法隐私 envelope），改为 `content_form` 透传 + `NO_LEADING_TEXT`；冻结开头标签文法与 256 码点判定窗；`UNKNOWN_TAGGED` 恒不落盘标签名（内容安全）；M1D run 可选但"提供即必须全部可解析"；报告三个分母并与 M1D 的 342 个有 `UserBlock` 的 capture 构成跨模块不变量；validator 与 M1D 同构的两层信任边界；白名单准入三规则（`task`/`image`/`irc` 等通用名词不准入）；D3 缓做 `SourceAnnotationProjection`，理由收敛为"不破坏 M2 两次独立提取的独立性"。[`r01-processing-spec.md`](r01-processing-spec.md) §8.2/§8.3 已同步修订（M1D 只做结构；M2 前置硬门 = `UserTextProjection` 必做，`SourceAnnotationProjection` 在 M2 首次消费 `domain_meta` 前必做）。

**规格已冻结为 v0.3（2026-09-03）。** 规格 §8 只读探针已在 v4 run `6be45e01…` 上执行：USER 14,407 = 前缀 13,225 + 观测 1,182；普通文本 11,694（含 2 条分段隐私 envelope）、空正文 4、开标签 2,709（15 个 ≥2 次标签名 + 7 个单例）；白名单冻结为 A 7 / B 2 / C 3，`skill`（41 次）依准入规则 ② 暂不准入并登记为扩展候选 D10，`ATTACHMENT_MARKER` 删除，新增 `EMPTY_TEXT`；三个分母 1,680 / 1,350 / 330（330 ≤ M1D 有 `UserBlock` 的 342 个 capture）。完整验收向量见规格 §6。

**`UserTextProjection` 已实现并正式验收（2026-09-03，提交 `1de39ae`）。** 按 [`../AGENTS.md`](../AGENTS.md) 测试先行：77 项模块测试 + 2 项 CLI 测试覆盖规格 §6 的全部用例清单；干净克隆双跑复现 run `47cfac20…`（绑定 M1D）与 `9dc26f2f…`（未绑定），23 项计数与 12 个白名单标签计数与规格 §6 向量逐项相等，1,182 个观测 USER 事件全部回指 UserBlock，330 ≤ 342（§8.3，[`r01-user-text-projection-validation.md`](r01-user-text-projection-validation.md)）。同批重构 `f70710f`（validator 公共原语）已对 M1B/M1C/M1D 重新验收（§5）。规格 §7 D10 的扩展候选 `skill` 保持 `UNKNOWN_TAGGED`，任何白名单扩展只能经修订规格 + §8 离线探针；`SourceAnnotationProjection` 按 D3 缓做，在 M2 首次消费 `domain_meta` 前必做。

**M2 拆为四个子模块，一次只做一个（2026-09-03 决定）。** 原计划"起草一份 M2 总规格"过大：[`overall-plan.md`](overall-plan.md) §4.6/§5/§6/§7 合在一起既含确定性结构事实、又含首次模型调用、又含跨 capture 聚合，任何一处返工都会拖住整块，也无法满足 M2 验收条"结构事实由确定性代码产生"的可核验性。沿用 M1C/M1D/`UserTextProjection` 已验证的节奏——独立规格 → 独立包 → 同构两层 validator → 干净克隆双跑验收——拆为：

| 序 | 子模块（契约） | 性质 | 输入 | 产出 | 单独成模块的理由 |
|---|---|---|---|---|---|
| ① | `TurnEvidence`（`turn-evidence-v1`） | 确定性、零模型 | 已校验的 M1B + M1D + `UserTextProjection` run | 每个 `QueryTurn` 一条证据包：意图证据事件 ID 集（仅 `PLAIN_USER_TEXT`）与 `intent_locality`；AgentStep 结构特征（步数、调用/观测/未解析计数、工具名集合、并行语义）；终态；中断线索（`CONTROL_SIGNAL`）；环境暴露标签（`HARNESS_*`）；附件位；按用途的 eligibility（`task_profile`/`environment_profile`/`reconstruction`，固定函数 + 原因码，plan §5） | 结构事实与模型语义彻底分开；是 ②③④ 的唯一输入，模型层只见证据包、不见原始 run |
| ② | `EnvironmentExposureProfile`（`environment-exposure-v1`） | 确定性聚合，只出计数 | ① run（+ M1B 工具配对/schema 冲突标量） | plan §6.2 的 R01 observed 环境暴露分布：declared/called/observed 工具角色、观测 empty/error/truncated 比例、harness 指纹、compaction、显式不可观测性声明 | M3 World 合成需要它且不依赖任何语义抽取；先交付确定性价值 |
| ③ | `SemanticExtraction`（`semantic-extraction-v1`） | **首个模型模块**，封闭标签 | ① 中 `task_profile ∈ {ELIGIBLE, PARTIAL}` 的回合证据包（正文经 M1B 事件 ID 解引用，只在模型边界内读） | 每回合 task family/domain/交付形式/约束种类（封闭枚举）+ 相邻回合语义关系（plan §4.6 八值）；两次独立抽取，不一致或无证据 → `AMBIGUOUS`/abstain；每条结论只引事件 ID | 全部模型风险隔离于此：冻结提示与输出 schema、模型/温度/缓存键、可从缓存离线重放；validator 只能核验 schema/来源引用/两次一致性，语义正确性显式声明为抽样人工评审门 |
| ④ | `TaskEpisode` DAG + `ObservedTaskDistribution`（`task-episodes-v1`） | 确定性组合与聚合 | ① + ③ run（+ M1C Grade-A 组作 lineage 去重分母） | plan §4.6 Episode DAG、§6.1 分布（`PREFIX_ONLY` 单列；unknown/abstain/eligible 分母显式） | 组合规则与聚合口径独立于模型输出演化 |

前置决定与缓做：③ 之前必须先定**模型接入**（哪个模型、能否离线缓存重放、预算与调用上限）——这是唯一需要用户拍板的外部依赖，①② 不受其阻塞；`SourceAnnotationProjection`（D3）只在 ③ 验收后作事后对照 oracle 时才需要；`ReconstructionCandidate`（plan §7）属 M2/M3 边界，留到 ④ 之后。

**① 规格草案已起草（v0.1，[`m2-turn-evidence-spec.md`](m2-turn-evidence-spec.md)）**：§0 三条实测地基来自冻结 run 的只读探针（2,610 回合 = 1,683 PREFIX_ROOTED + 927 OBSERVED_ROOTED；927 中 885 有 `PLAIN_USER_TEXT` 意图证据、32 只有 Harness 注入、10 只有控制信号；1,680 个 capture 的前缀共 10,802 条用户文本是 80% capture 的唯一意图来源；终态缺失系统性——1,211 capture 以 `TOOL_CALL_PENDING` 结束）；契约一张表（主键沿用 `query_turn_id`，闭合值域，不存工具名）；eligibility 只产 `task_profile`/`environment_profile` 两用途 + 8 个原因码的固定函数（`reconstruction` 在结构事实下恒等于 task ELIGIBLE，无信息量，不产）；§6 验收向量已由探针按固定函数预演（task：548/337/42 与 0/1,680/3；environment：230/675/22 与 416/1,267/0）。**下一步（唯一）**：评审并冻结该草案（§11 四个待定小决策 E-a–E-d 需拍板），冻结后才写代码（[`../AGENTS.md`](../AGENTS.md) 规格先行）。以下已冻结前置约束对 ①–④ 全部有效：

- 输入单元 = M1D `QueryTurn`（带 `root_status`）+ M1B 事件冻结标量 + `UserTextProjection` 的带来源、带 `text_class`/`locality` 的 USER 事件引用；M1C 边只作 Control 侧来源解析提示（`BLOCKING_HINT_ONLY`），不作语义依据；
- 消费约定按 [`m2-source-projection-spec.md`](m2-source-projection-spec.md) §2.5：只有 `PLAIN_USER_TEXT` 可作任务意图证据；`HARNESS_*` 只作环境暴露证据；`CONTROL_SIGNAL` 只作中断线索；`EMPTY_TEXT`/`UNKNOWN_TAGGED`/`NO_LEADING_TEXT` 只计数；由 `PREFIX_UNLOCALIZED` 证据得出的 Episode/意图必须携带 `intent_locality=PREFIX_ONLY` 并在 `ObservedTaskDistribution` 中单列；M2 不得按绝对路径重新解析原始 JSONL；
- ③ 是首个引入模型调用的模块：规格必须先定义提示与输出的冻结 schema、抽取结果的来源引用（只引 ID 不复制正文）、确定性/可复现要求（模型、温度、缓存键）、与上游"任务摘要"的独立性（D3：上游标签只能作事后对照 oracle）、以及独立 validator 能核验什么、不能核验什么（语义正确性不可机械核验，须显式声明为抽样人工评审门）；
- 硬停止线 §10 在 ① 规格冻结前不变。

## 12. 参考资源采用边界

| 资源 | 当前用途 | 禁止事项 |
| --- | --- | --- |
| 旧 `seed2traj/search_audit` | `PRINCIPLE_ONLY`：来源账本、canonical artifact、显式位置/tool ID、测试组织 | 不迁移 rubric、人工 review、badcase、LLM judge、搜索停止逻辑 |
| AgentRx | 后续选择性重写 typed 状态、invariant 与 failure lifecycle | 不复用其 R01 converter，不用动态 invariant 或 LLM Judge 生成 Truth |
| Agent-World、ASTRA | M3 的 world-first、工具/证据图和联合演化原则 | 不前移到 M1，不声称恢复用户原环境 |
| TRACE、EnvHarness | M4/M5 的 self-test、mutation 和难度校准原则 | 不在当前阶段调用模型或生成训练任务 |
| `轨迹诊断.pdf` | M2 之后的证据视图和失败归因参考 | 不把归因标签写回 M1 事实层 |
| 腾讯 AGS/Hermes/TokenHub 指南 | 后续 rollout 执行边界 | 不进入 core，不替代 Truth/Verifier |

AgentHER、CSO、GameCraft-Bench 在当前本地快照中没有可审计、可采用的完整依据，不作为现阶段实现来源。所有参考资源均在 `/Users/wujian1/Downloads/seed2traj/refer_repo` 只读使用，TraceForge 运行时不得依赖该路径。

## 13. 新会话开工检查表

先执行只读检查：

```bash
git status --short
git log --oneline -5
git diff 1de39aeda894b55cc84d1fb8f8923e2b4c57380f..HEAD -- src/traceforge pyproject.toml uv.lock
.venv/bin/pytest -p no:cacheprovider -q
.venv/bin/ruff check --no-cache .
.venv/bin/ruff format --no-cache --check .
uv lock --check --offline --no-cache
.venv/bin/python scripts/validate_m1_run.py \
  artifacts/r01/6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed
.venv/bin/python scripts/validate_m1c_run.py \
  artifacts/r01/lineage/9ff708d98773a175f9552b09ea1171f16a6ba3bbe4a4ffbcc4fdec2612acb17b \
  artifacts/r01/6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed
.venv/bin/python scripts/validate_m1d_run.py \
  artifacts/r01/query_turns/84d826b3d7abddffb5f8fde28d7e5592590720bd6909c24a8b17b7f0c70b66ea \
  artifacts/r01/6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed
.venv/bin/python scripts/validate_user_text_projection_run.py \
  artifacts/r01/source_projection/47cfac20b89ed066f388da6679cb53f745e6a0392cd5f6e459e869c7076f689c \
  artifacts/r01/6be45e01cb97b14ce8cfeeed4b0860097a7006619a83e9a32f1284ee5775c8ed \
  --m1d-run artifacts/r01/query_turns/84d826b3d7abddffb5f8fde28d7e5592590720bd6909c24a8b17b7f0c70b66ea
```

预期：`src/traceforge/` 相对 `1de39ae` 无变化（`trajectory/`、`lineage/`、`query_turns/` 的最后一次变化是重构 `f70710f`，已在 §5 重新验收）；测试全部通过（本检查点为 352 项）；M1 validator 返回 `ok=true`、10 files、1,683 lines、175,858 events；M1C validator 返回 `ok=true`、5 files、计数与 §8.1 一致；M1D validator 返回 `ok=true`、8 files、13 项计数与 §8.2 一致；投影 validator 返回 `ok=true`、3 files、23 项计数与 §8.3 / 规格 §6 一致。在 AFS 慢盘上，四个 validator 各需 1–7 分钟（M1C/M1D 内含对 706 MB 上游 run 的权威重验；投影绑定 M1D 时该重验发生两次），属先校验后消费的必要成本。

provenance 单条 git 命令超时已由 5 秒放宽到 30 秒（`trajectory/provenance.py`，2026-09-03；AFS 上 `git status` 实测 4–8 秒，原阈值使就地 run 回执恒 `available=false` 而被 validator 拒绝，见 [`r01-m1c-validation.md`](r01-m1c-validation.md) §7）。该改动只影响 `run_receipt.json` 的来源字段，不触及任何内容寻址产物。正式 run 仍一律在本地盘干净克隆上执行——干净克隆是验收纪律（保证执行的代码就是提交的代码），不是对超时的绕过。

## 14. 明确禁止项

- 不在 compiler 或通用 validator 中硬编码 R01 路径、摘要、统计或样本内容；
- 不把 capture、boundary、thread 或保守候选直接当 task；
- 不声称恢复真实用户环境或无偏生产分布；
- 不使用 rollout 多数票生成 Ground Truth；
- 不让任何下游读取或依赖旧 `reasoning_content` 原文；
- 不让 Harbor/AGS 反向依赖或污染 core；
- 不提交真实回流、完整 artifacts、模型缓存、密钥或参考仓库；
- 不处理 `claw-eval`。
