# TraceForge 开发会话交接

版本：M1C 检查点

日期：2026-09-02

状态：M1A、M1B v3 与 M1C 正式通过；已验收代码停止在 M1C；M1D 规格评审稿，实现未验收

## 1. 本文用途

本文是新开发会话的当前检查点和权威索引，不替代总体计划或模块规格。新会话按以下顺序阅读：

1. [`../AGENTS.md`](../AGENTS.md)：唯一开发规范；
2. 本文：当前事实、停止线和下一步；
3. [`background-and-goals.md`](background-and-goals.md)：背景、目标和主张边界；
4. [`overall-plan.md`](overall-plan.md)：完整架构、模块和阶段门；
5. [`r01-processing-spec.md`](r01-processing-spec.md)：M1 精确输入、算法、契约与验收；
6. [`r01-m1-v3-validation.md`](r01-m1-v3-validation.md)：M1A/M1B 正式验收证据；
7. [`m1c-processing-spec.md`](m1c-processing-spec.md) 与 [`r01-m1c-validation.md`](r01-m1c-validation.md)：M1C 契约与正式验收证据；
   [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md)（R1–R8）与 [`m1c-known-items.md`](m1c-known-items.md)（K1–K3）：已知项登记；K1/K2 已由 M1C v2 从根因关闭，**上游不透明摘要不入任何关系证据**是 v2 起的通用原则；
8. [`m1d-processing-spec.md`](m1d-processing-spec.md)：M1D 规格评审稿；[`m1d-review-20260902.md`](m1d-review-20260902.md)：对未提交 M1D 实现的评审意见；
9. [`reference-repositories.md`](reference-repositories.md) 与 [`implementation-sources.md`](implementation-sources.md)：参考逻辑和迁移边界。

如果本文与模块规格冲突，以 `r01-processing-spec.md`（M1A/B）和 `m1c-processing-spec.md`（M1C）的契约为准；如果与开发纪律冲突，以 `AGENTS.md` 为准。

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
    D --> S{{当前 STOP}}
    S -. 规格评审中 .-> E[M1D QueryTurn]
    E --> F[M2 TaskEpisode 与画像]
    F --> G[M3 Task-World 联合合成]
    G --> H[M4 认证、rollout 闭合与定向修复]
    H --> I[M5 六维难度校准]
    I --> J[CertifiedTaskWorldRelease]
```

当前只完成实线部分。M1D 的 `src/traceforge/query_turns/` 与 `tests/test_m1d_*.py` 在 2026-09-02 由并行会话开始实现，尚未提交、未验收，不在本检查点的正式结论内。仓库中没有 M2、World、认证、难度或 Harbor 的空壳实现。

## 4. 仓库与冻结输入

```text
本地仓库：/Users/wujian1/Downloads/traceforge
远程：git@gitlab.sh.sensetime.com:wujian1/traceforge.git
分支：main
R01：/Users/wujian1/Downloads/seed2traj/return_data/four_batch/by-rubric/R01.jsonl
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

M1A/M1B 正式全量运行绑定的代码冻结点：

```text
Git commit：c3c0a8fb6ed9927d5bebba55c616e1ef524c653b
Git tree：5438c8d3e421007bcc400c30c47862945caaae55
dirty：false
TraceForge：0.3.0
compiler contract：trajectory-compiler-m1ab-v3
ToolPairing schema：traceforge.tool-pairing.v3
```

M1C 正式全量运行绑定的代码冻结点（`src/traceforge/trajectory/` 相对 `c3c0a8f` 无变化）：

```text
Git commit：fcff8cf88e7edcd645484318fd8bd12de50b7af7
Git tree：482b2a72c4b3b17bdb15c143f06bb5c1b48bb416
dirty：false
lineage contract：lineage-compiler-m1c-v2
```

交接文档自身会形成后续纯文档提交；新会话必须用 `git log` 确认 HEAD 是上述冻结点的后代，并确认 `fcff8cf` 之后除 M1D 新增文件外没有未重新验收的 `src/trajectory/`、`src/lineage/`、`pyproject.toml` 或 `uv.lock` 变化。

## 6. M1 已实现能力

当前实现位于 `src/traceforge/trajectory/`，具体字段和算法只在 `r01-processing-spec.md` 定义。能力边界如下：

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

## 7. 本轮审计闭环

v1、v2 验收报告的正式结论均已撤销，只保留历史正常路径事实。v3 闭合三类 P1：

1. typed payload、terminal quality/report 与 tool arguments pointer 可同步重签假绿；
2. URL、路径、query 或凭据形式的 dataset ID 可进入公共报告；
3. `NAME_MISMATCH`、`RESULT_BEFORE_CALL`、`INVALID_CALL_ARGUMENTS` 等异常 1:1 组仍生成 `matched_*`。

正式验收前的独立红队又发现同属第 1 类的 Data URL envelope 变体：把内外审计长度同步伪造为 0 可把非空终态伪报为空。代码冻结提交 `c3c0a8f` 增加两个独立不变量：合法 Data URL envelope 必须为正长度；terminal 空非空由 value 形态重算，而不是由审计长度决定。纯 Data URL、混合文本、零长度和正长度绕过均已 fail-closed。

对抗审计另发现两项**带来源接受、暂不修改冻结代码**的已知项，登记在 [`m1ab-v3-known-items.md`](m1ab-v3-known-items.md)：R1（隐私脱敏续行判定只认 CR/LF，裸空格/制表符致 base64 尾段 fail-open；R01 未触发、产物 0 base64）与 R4（截断轴把源自报 `input_truncated=False` 渲染成 `OBSERVED_NOT_TRUNCATED`，属规格 §5.5/§7 已批准的设计张力）。两项均未使任一验收门变红；任何收紧都必须走单独 reopen 与重新验收。2026-09-01 会话已把 gate 4/6 及 gate 1/5/7 现场升级为 live 亲证，证据见 [`r01-m1-v3-validation.md`](r01-m1-v3-validation.md) §8。

## 8. 正式验收证据

```text
run ID：519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d
artifact manifest SHA-256：9dcdb083bd202157ab8c9e90b8c660a0e18130c62d65cafe743a7c27a5549876
运行 A：42.648523 秒，RSS 116,637,696 bytes
运行 B：42.540646 秒，RSS 114,098,176 bytes
validator A/B：ok=true；10 files；1,683 lines；175,858 events
递归 diff：排除 run_receipt.json 后无差异
测试：160 passed
Ruff lint/format、离线 lock、Git diff check：通过
```

本地正式 run：

```text
artifacts/r01/519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d/
```

两次 receipt、manifest、validator 输出、diff 结论和原始 RSS 日志保存在：

```text
artifacts/r01/acceptance/519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d/
```

真实 run 和证据均被 Git 忽略。长期可提交摘要见 `r01-m1-v3-validation.md`。

### 8.1 M1C 正式验收证据（v2，2026-09-03）

```text
lineage run ID：978c0347b4198223cada09ccfd7bf55cf50a42a0ee7a049d55feedf793c7e12e
绑定 M1B run：519a86d3…e06d1d（manifest SHA-256 9dcdb083…）
lineage artifact manifest SHA-256：837c9da7f89b16658f4eacca7ea04a0dd851c42abd9f93f2e094bddcfd9450f9
运行 A：116.317097 秒，RSS 244,486,144 bytes
运行 B：118.341063 秒，RSS 243,265,536 bytes
validator A/B：ok=true；5 files；计数与 m1c-processing-spec §4.4 逐项一致
递归 diff：排除 run_receipt.json 后无差异
lineage 相关测试通过；Ruff lint/format：通过；v2 相对 v1 产物恰好只少 41 条 IDENTICAL 边
```

本地正式 run 与证据：

```text
artifacts/r01/lineage/978c0347b4198223cada09ccfd7bf55cf50a42a0ee7a049d55feedf793c7e12e/
artifacts/r01/acceptance_m1c/978c0347b4198223cada09ccfd7bf55cf50a42a0ee7a049d55feedf793c7e12e/
```

长期可提交摘要见 `r01-m1c-validation.md`，其中 §7 登记两项环境已知项：慢盘上 `git status` 超过 provenance 的 5 秒超时会使就地 run 被 validator 拒绝（正式 run 因此在本地盘干净克隆上执行）；验收机器上的 `return_data/.../R01.jsonl` 为不完整副本，任何需要原始 JSONL 的阶段开工前必须先取回完整文件并核对 SHA-256。

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

不要将 `processing_status=COMPLETE` 解读为任务完成，也不要把 353 个结构候选直接交给任务合成。M1C 的边是 Control 侧来源解析信息，不进入任何 Public 视图，也不用于硬去重。

M1D 规格 §0 的结构探针另给出对 M2 极关键的事实：14,407 个 USER 事件中 13,225 落在不可定位的 `PRE_FIRST_OBSERVED_TERMINAL` 前缀；80% 的 capture 观测窗口内没有可定位 USER。M2 的 `SourceAnnotationProjection` 若不显式允许以带"不可定位"标记的方式读取前缀 USER 正文作为任务意图证据，`ObservedTaskDistribution` 的分母将只剩约 342 个 capture。

## 10. 当前硬停止线

未经下一阶段规格审核，不得实现或宣称存在：

- Grade-B `NORMALIZED_VISIBLE_PREFIX_OF`、已删除的 `IDENTICAL_RAW_REQUEST_HASH` 或任何非 3 类 Grade-A 的 lineage 关系；
- 任何以上游不透明摘要（`raw_request_hash`、`target_hash`、`input_truncated` 等自报值）为依据的关系或 `OBSERVED_*` 状态；
- 以 M1C 边为依据的 capture 硬去重、合并或"选最长丢其余"；
- 已验收的 QueryTurn，或任何 TaskEpisode；
- ObservedTaskDistribution 或 EnvironmentExposureProfile；
- 业务 Domain、World、Truth、Reference、Verifier；
- 可解性、难度、模型边界或自动纠正；
- Harbor、AGS、Hermes 或 TokenHub runtime integration。

Harbor 是将来 `RunnableTaskWorldCandidateBundle` 的 rollout 执行层，不是 TraceForge core，也不是当前 M1 的前置依赖。Harbor 部分由项目负责人另行负责。

## 11. 下一模块：M1D 评审与验收，随后冻结 M2 前置

M1C 的四门（候选组仅 `BLOCKING_HINT_ONLY`、Grade-A 组盲、validator 独立核验分区、`raw_request_hash` 格式契约）已在 [`m1c-processing-spec.md`](m1c-processing-spec.md) §3 冻结并经 [`r01-m1c-validation.md`](r01-m1c-validation.md) 验收，此处不再复述。

M1D 当前状态：规格 [`m1d-processing-spec.md`](m1d-processing-spec.md) v0.2 为评审稿，§12 留有 D-a～D-d 四个小决策；`src/traceforge/query_turns/` 由并行会话开始实现。M1D 的验收门与 M1C 同构：在冻结 M1B run（可选加 M1C run）上两次独立构建、独立多参 validator、观测/前缀守恒、AgentStep 双向 bijection、capture 末 turn 与 `CaptureQualityV2.terminal_status` 交叉核对、两层隐私、RSS 停止线。M1D 验收报告落 `docs/r01-m1d-validation.md` 后才更新本文停点。

**M1B v4 reopen（已批准；代码已落于提交 `8f6f65c`，验收后置）。** 代码级审计的四项 M1B 已知项归为两个根因，一次 reopen 全部处理：(a) 上游自报值不得渲染成观测——`input_truncation_status` 的枚举值改为 `SOURCE_REPORTS_TRUNCATED / SOURCE_REPORTS_NOT_TRUNCATED / UNKNOWN`（R4）；(b) 契约不保留无生产者的状态、业务规则只有一个定义——删除 `ProcessingStatus.PARTIAL`（R7，三态 eligibility 属 M2）、严格匹配谓词抽为 `contracts.py` 纯函数供 compiler 与 validator 共用（R5）、`visible_payload_utf8_byte_length` 改名为 `visible_payload_envelope_utf8_byte_length`（R8）。前三项改产物字节或契约版本，compiler contract 升为 `trajectory-compiler-m1ab-v4`。本机已完成代码、153 项 M1A/B 测试与 lineage 回归；**正式验收必须在有完整 R01（560,481,884 字节、SHA `3832d8aa…`）的机器上两次全量重编译**，在此之前 `c3c0a8f` 的 v3 结论继续有效、v4 代码不得宣称已验收。**连带影响**：v4 validator 对 v3 冻结 run `519a86d3…` 按设计报 `SCHEMA_VERSION_MISMATCH` / `COMPILER_CONTRACT_VERSION_MISMATCH`（§13 检查表中该项在 v4 验收前预期失败）；M1C v2 的正式 run `978c0347…` 绑定的是 v3 M1B run，v4 重编译后须用新 M1B run 重跑 M1C（无代码变化，仅输入身份变化，产生新 lineage run ID）并补一次 M1C 验收。

进入 M2 前必须冻结最小、带来源的 `SourceAnnotationProjection` 或只读 `SourceResolver`（[`r01-processing-spec.md`](r01-processing-spec.md) §8.3）。该规格必须直面 §9 末段的事实：前缀 USER 正文要能以显式"不可定位"来源标记进入任务意图证据，否则 M2 分母塌陷；同时 M2 不得按绝对路径私下重新解析原始 JSONL。

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
git diff c3c0a8fb6ed9927d5bebba55c616e1ef524c653b..HEAD -- \
  src/traceforge/trajectory pyproject.toml uv.lock
git diff fcff8cf88e7edcd645484318fd8bd12de50b7af7..HEAD -- src/traceforge/lineage
.venv/bin/pytest -p no:cacheprovider -q
.venv/bin/ruff check --no-cache .
.venv/bin/ruff format --no-cache --check .
uv lock --check --offline --no-cache
.venv/bin/python scripts/validate_m1_run.py \
  artifacts/r01/519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d
.venv/bin/python scripts/validate_m1c_run.py \
  artifacts/r01/lineage/978c0347b4198223cada09ccfd7bf55cf50a42a0ee7a049d55feedf793c7e12e \
  artifacts/r01/519a86d3f48add7c37262b05db792d790aa49fccf67b7b92bbea2784e8e06d1d
```

预期：`lineage/` 相对 `fcff8cf` 无运行时代码变化；`trajectory/` 相对 `c3c0a8f` 有且仅有 `8f6f65c`（M1B v4，待验收）的变化；测试全部通过（M1C 检查点为 230 项，不含并行会话的 M1D 新增测试）；M1 validator 对 v3 冻结 run 在 v4 验收前**预期失败**于 schema/contract 版本不匹配（v4 重编译后应返回 `ok=true`、10 files、1,683 lines、175,858 events）；M1C validator 返回 `ok=true`、5 files、计数与 §8.1 一致。

若工作区含未提交的 `query_turns/` 或 `test_m1d_*.py`，它们属于进行中的 M1D 实现，不改变本检查点结论；不得把它们与 M1C 或文档改动混入同一提交。

## 14. 明确禁止项

- 不在 compiler 或通用 validator 中硬编码 R01 路径、摘要、统计或样本内容；
- 不把 capture、boundary、thread 或保守候选直接当 task；
- 不声称恢复真实用户环境或无偏生产分布；
- 不使用 rollout 多数票生成 Ground Truth；
- 不让任何下游读取或依赖旧 `reasoning_content` 原文；
- 不让 Harbor/AGS 反向依赖或污染 core；
- 不提交真实回流、完整 artifacts、模型缓存、密钥或参考仓库；
- 不处理 `claw-eval`。
