# 当前状态与交付标准

更新日期：2026-09-28；本轮基于 5f3dc84 整合真实首跑发现的 Intent 路径候选、绑定反馈和角色说明修复；完整离线回归对应源码 0d88df1。重建和 solver rollout 保持分离。v34（运行源码 8891af3）及更早运行均为历史记录，不能代表最新修复版本的交付结果。流程与源码导航分别见 [原始会话流程](raw-session-pipeline.md)、[阅读地图](rebuild-live-map.md)。

本次补充独立 rollout 验收前的 runtime 核对：`execute-rollout` 与 `read-results` 认证前共用已有哈希检查，仅核对计划记录的 `agent.py`、`capture.py`、`evidence.py`、`validator.py` 四个文件。记录文件修改或缺失时，在认证和派生产物写入前拒绝；没有 runtime metadata 的旧计划与只导出冻结输入的 `publish_rollout_bundle` 保持原行为。此项来自静态缺口，未观察到真实运行因该缺口发生误验收。相关确定性回归 **72 passed（0.99 秒）**，Ruff E9/F63/F7/F82 和差异检查通过；该项源码验证未运行模型或 AGS，真实端到端验收仍未完成。

最新真实首跑使用 5f3dc84，位于 artifacts/pipeline-debug-20260928/live-r04-2179-01/：Session 与 Intent 各完成一次模型调用，但 Intent 因 BINDING_FILE_PATHS_REQUIRED:obl-001 返回 REVIEW。进程退出码 0 只表示正常结束，Completion/Sufficiency/Verifier/RED 均未运行，AGS 调用为 0。真实任务目标恢复正确；用户未直接点名文件，关键初态源码为 PARTIAL，却被调用方的 COMPLETE-only 候选过滤排除，模型原答复的文件绑定也为空。校验失败也没有反馈给 Intent，Verifier 的轮次预算不能修复此上游缺口。

对应源码修复让已有可信初态 PARTIAL 正文的路径参与任务绑定，保留原完整度和 Replay 修改屏障；只对已完成结果中的绑定合同错误反馈一次，不重写任务、不删义务、不降级已有 FILE。首轮和纠正轮的请求/轨迹分别保存在原 private/ 与 binding-repair/private/，agent.attempts 记录各轮状态、错误和目录。上下文工具说明与只读权限已统一。最新修复仍需同一条真实会话重新运行确认，不能回写首跑结果或把离线测试当作端到端通过。

整合源码 0d88df1 的完整非 live 回归：1140 passed、5 skipped、2 deselected，103.91 秒；5 项 warning 为既有 httpx verify 参数弃用提示。Ruff E9/F63/F7/F82 与差异检查通过。后续提示措辞补充明确：可用观察到的路径定位原任务对象，但不能把原 agent 操作扩写成用户新要求；不改变 Replay/绑定校验规则。最终提示补充的相关回归另有 90 passed（0.60 秒），限定 Ruff 与差异检查通过。

**当前没有一条经过可信完整验收的端到端结果，尚不具备稳定批量交付的证据。** 下述六类问题已落实对应源码修复；修复后的真实产物质量仍待新运行确认。历史任务包、RED 或真实轨迹不能代替新代码的完整验收。

历史整合版本 `610254b` 的全量回归：950 passed、7 skipped、5 warnings（均为已有 httpx verify 参数弃用提示），耗时 94.83 秒；Ruff E9/F63/F7/F82 与差异检查通过。真实 AGS 上传子目录写入回归另行通过；这些证据不等于 solver 端到端验收。日志与 line41 缺失输入收据见 artifacts/source-repair-audit-20260928/。

整合源码 `03e865a` 的全量离线回归：**964 passed、7 skipped、5 warnings，118.30 秒**；Ruff E9/F63/F7/F82 与差异检查通过。该版本增加明确的语义审查阶段上下文：重建检查验收机制，真实最终响应留到 rollout 后验收；不能用响应机制就绪抵消 FILE 验证器缺陷。本轮没有启动新模型运行，v34 保持历史 REVIEW。

## 当前工作顺序

1. 先统一代码与检查版本，避免并发修改主目录。
2. 对真实样本逐模块检查任务、环境和 verifier 的实际内容；缺陷只在负责模块修复，交付通过校准的 Harbor bundle。
3. 单独启动冻结 bundle 的沙盒 rollout，核对轨迹、评分和最终响应。
4. 单条产物合格后再批量处理 R04/R05；不同时扩散依赖安装、解析适配或自动回修框架。

当前采用固定分工：源码修复会话负责模块修复、离线回归、整合与推送；真实调试会话负责冻结版本上的真实数据、模型、AGS 执行和逐阶段产物质检。调试发现问题后回传原始证据与预期差异，再由源码会话修复；不并发修改同一主目录。真实重建与 solver rollout 分开执行。

## 2026-09-28 并行工具结果回放修复

真实调试报告在冻结 R04 line 2179 发现：旧适配丢失并行 exec 的结果块边界和子命令 workdir/shell，PowerShell 固定 UTF-8 初始化又被误判为写入，导致已有文件正文无法进入 Replay。该报告基线为 610254b，相关适配在 11556ef 尚未改变。

本次源码修复保留原始结果槽位及失败状态，静态关联可证明顺序的 Promise.all 输出，区分完整结果、仅 stdout、退出码与 stdout 的固定 JSON 投影，并保留 input_text 等明确文本块。包含退出状态的结果仅接收已完成且成功的子命令；仅 stdout 的旧格式不虚构退出状态。坏 JSON 只留下本槽位诊断，不挪用后续块；统一原生文件工具与 shell 的 workspace 坐标，避免同名文件混淆或修改屏障失配。固定 UTF-8 初始化不再误报写入；限行和切片保持 PARTIAL，搜索、通配符拼接、字节数字等非文件正文不物化。并行读写没有可靠先后证据时保留初态边界，不把输出数组顺序当作执行时序。

这些是协议和证据恢复修复，不是原始源码补全。原始正文中的脱敏占位符、无效标识符及截断代码继续原样保留，由后续 Completion/Sufficiency 判断和处理。当前静态适配只支持可证明参数与输出对应的包装；不执行 JavaScript，也不猜测动态命令的结果归属。冻结 863b532 的真实 R04 line 2179 Replay 已由独立调试会话核对：10 个事件得到 19 个操作，恢复文件由 0 个增至 6 个，目录、坏 JSON 后的槽位和落盘字节均一致。原始脱敏破坏、乱码与限行仍存在；这不是可执行环境或端到端通过结论。报告保留在 artifacts/pipeline-debug-20260928/replay-r04-2179-863b532/REPORT.md。

本次修复最终非 live 全量回归：**1054 passed、5 skipped、2 deselected、5 warnings，86.54 秒**；警告仍为既有 httpx 参数弃用。旧具名输出恢复与新增并行适配定向回归另有 34 项通过；Ruff E9/F63/F7/F82 及差异检查通过。本轮没有模型或 AGS 业务执行，离线回归不作为端到端通过证据。

## 2026-09-28 真实反馈后的集中修复

- 终端读取行段：将静态可定位的 Get-Content、TotalCount/Head 及单一 First/Skip/SkipLast 片段传入已有 read_segment、Completion 上下文和源码片段产物。保留原文、来源槽位及未知总长度；尾部起点或复合选择不能证明时不猜测。冲突仍记录为冲突，不自动用后读替换初态，修改后片段不公开为初态。
- Completion 公开证据：真实纯工具检查证实末尾 pending apply_patch 参数可被 read_evidence 读到。现从公开 timeline、索引和 excerpt 引用统一排除明确未返回的调用，保留原始私有轨迹；同 ID 的唯一有效完成结果继续可用。
- 独立 rollout：read-results --plan-dir 绑定冻结计划、执行收据、trial 输入和隐藏任务验收合同，执行已有 Hermes 认证，并与内联流程共用最终回复收据及成功判定。读取只更新派生认证和结果，不重跑 agent、不改 workspace/轨迹；独立 PASS 不回写重建、RED 或 SFT 状态。接口与副作用见 [Harbor/AGS 边界](harbor-ags-boundary-adapter.md)。

组合源码 54a7cfe 的最终非 live 全量回归：**1103 passed、5 skipped、2 deselected、5 个既有 httpx 弃用警告，75.66 秒**；相关源码与测试的 Ruff E9/F63/F7/F82、差异检查通过。未运行模型或 AGS；真实 863b532 质检记录不能代替此版本的重建及 rollout 验收。

本轮源头修复由主会话集中整合，辅助会话和代理在隔离分支工作；真实调试继续使用冻结版本和独立产物目录。最新组合源码仍需真实样本复跑。原文损坏如何处理必须结合用户任务：目标修复留给 solver，采集损坏与缺失上下文由补全模块判断；不把 AST 失败直接当作不可重建。

## 2026-09-27 源码修复与待验证事项

| 环节 | 已落地的源码行为 | 真实验证仍需确认 |
| --- | --- | --- |
| 任务与环境绑定 | 区分用户要求、示例及路径来源；分开初始输入与目标输出。依据回放根和用户绝对路径统一 workspace 坐标，公开指令与响应验收复用同一映射 | 新 Intent/Completion 不再出现描述词、示例路径或坐标错位导致的虚假必需文件 |
| 原始轨迹解析 | 已解包 read 的 hash 行号；显式 raw 保留原文，缺失文件错误不再物化为源码 | 新 replay 与 workspace 正文符合原始证据；旧产物不回写 |
| agent 工具交互 | 缺证据编号的首次写入仍被拒绝，但后续补正不再使整次执行永久失败 | 真实 agent 能纠正参数并完成；完整观测与未知证据保护仍生效 |
| Completion → Sufficiency | 有效候选最多追加两轮定向修复；仅缺探针时只重评。保留原始观测、候选快照和失败收据，无进展或耗尽后真实退出；运行声明可纠正而非永久合并冲突版本 | 实际缺口能被合理补齐，探针覆盖任务所需能力，且没有预解任务。详见 [环境反馈闭环](environment-repair.md) |
| Verifier / RED | 在现有 Verifier 修复轮内独立审查测试、参考解、mutation 及响应义务映射，将具体反例返回生成器；强调实质行为和真实输入 | 新 verifier 能拒绝格式正确但结论错误的产物，也能接受合理正确结果；独立模型审查与 RED 数值均不能单独证明语义正确 |
| 最终响应验收 | 分离 rollout 的响应采集许可与最终验收资格；保留原始公开输出格式，在真实轨迹产生后按显式合同检查响应、实际报告、摘要和路径。不再提前要求尚未产生的响应证据，也不继承用户未声明的旧报告字段 | 真实 trial 完成后证据绑定、格式及支持的内容一致性检查闭合；不支持或未覆盖的义务继续保持未验证 |

历史源码 `6b578e7` 的全量离线回归：`796 passed, 6 skipped, 5 warnings in 91.43s`；`ruff check --select E9,F63,F7,F82` 与 `git diff --check` 通过。当时的主工作目录及 GitHub/GitLab 同步至 `805e481`（源码为 `6b578e7`）。这些检查不等于真实端到端验收。

冻结 R04 line 41 的 v32 使用源码 `6b578e7`，产物位于 `artifacts/r04-line41-source-fixes-v32/`，已自然退出，状态为 Completion REVIEW。五次写入返回 `sandbox write failed`；旧工具日志没有保留退出码和 stderr，不能从旧记录直接还原底层异常。没有进入 RED 或 rollout。后续真实 AGS 复现上传目录权限问题并修复；v34 的真实 Completion 写入也已成功，确认写权限阻塞已解除，但不证明写入内容合格。

在独立真实 AGS 沙盒中已复现：上传子目录为 root 所有，普通 user 写入失败；整合目录所有权和错误诊断修复后，同一读写回归通过，原始正文覆盖仍被拒绝，沙盒清理完成。相关离线回归 24 项通过。两份失败/成功日志保存在 `artifacts/source-repair-audit-20260928/`。

两个缺失文档的原始读取均为 `File not found`。模型生成的 plan.md、progress.md 是推断内容，不能算原始正文恢复；已知不存在与未捕获正文必须分开，不能为了满足路径门禁补造历史事实。主线已保留 ABSENT 保护：line41 静态重放标记两个缺失路径，Completion 返回 REVIEW、模型调用为 0。该行为也会提前退出，无法进入后面的 TaskFit 变体判断；是否可在其余真实环境上保留任务目标并生成变体，仍需单独处理，不能通过伪造缺失初态解决。

v32 中将源码 `self.state.lock()` 等标识符误识别为支持文件的问题已由 `e6aa97a` 修复，同一输入重算消除 22 个假缺口。

v33（源码 `e6aa97a`，`artifacts/r04-line41-source-fixes-v33/`）自然结束于 Intent。根 JSON 多出引号，解析器误取内部 acceptance_report 后报告缺少 task。`fd41fdc` 已禁止损坏根对象回退到内部片段，并允许原角色单次格式纠正。v34 已通过 Intent，但没有触发纠错分支，不能当作该分支的真实验证。

v34 于 **2026-09-28 05:18:28.849970 UTC** 启动（源码 `8891af3`），现已自然结束，目录为 `artifacts/r04-line41-reconstruction-v34/`。manifest 和 stage_metrics 一致记录：Intent、Completion、Sufficiency 为 READY，TaskFit 为 READY_ORIGINAL，最终停在 **Verification REVIEW**。只请求 `--execute-red`，未请求 `--execute-rollout`；实际未进入 RED，没有交付 Harbor bundle，也没有 solver 轨迹。

四个前置阶段状态通过，仍不能认定产物质量合格：

- v34 未包含当前已知缺失保护，仍将 plan.md、progress.md 标为 MODEL_COMPLETED；写入成功只确认权限修复。
- Sufficiency 没有收到可信回放分段及当前候选的充分上下文；root_session.rs 共 4348 行，workspace 只物化了第 1–1191 行，而原始证据另有相关后段。
- 第一轮 Verifier 参考脚本有语法错误；第二轮语义审查发现引用被错误限制在 changedFiles，以及任意正文中的 APPROVED/CHANGES_REQUIRED 会被误当作结论。不能直接放行该候选。
- 响应审查混淆了机制覆盖与执行验收：obl-002 摘要机制确实遗漏，obl-003 已有 acceptance_report 合同，却因文件 pytest 未检查最终回复而被拒。重建应准备并审查机制，实际响应留到 rollout 后验收。

`be1c1e8` 的可信分段与当前候选事实传递、`cacc47d` 的 Verifier 响应合同补全已合入 `f4c495c`；保留主线 source-excerpts、修改屏障、匿名与重复证据过滤以及原始响应契约约束。整合版本的回归结果见页首；尚未真实重跑，不改写 v34 的结果。

隐藏 task_acceptance、RED-only READY 与响应待验分离、独立 rollout 预算保存均已整合。待验义务不清除，未执行真实 rollout 时 sft_eligible 与 certification_closed 保持 false。独立 read-results 已通过 --plan-dir 绑定冻结输入与执行收据，复用内联的 Hermes 认证和响应合同验收；尚未用真实 rollout 验证本次接入。

仍存在的具体能力缺口：`dependencies/runtime_constraints` 目前只作声明保存，没有统一自动安装机制。缺包必须以真实探针和后续运行说明；声明了依赖不代表依赖已安装。当前没有新增安装框架或把自由文本依赖变成新的确定性准入门禁。只读审查也不应因缺少编译入口而被自动判定不可执行。

## 目标产物

**重建阶段**负责从原始 session 生成合理的 task、足够真实且可使用的 task-start workspace，以及能区分正确与错误结果的 verifier，并通过 RED 校准后交付独立 Harbor bundle。RED 执行初态、参考解与 mutation 等校准，不等于 solver 真实解题；重建完成无需先产生 solver 轨迹。

**solver rollout 阶段**单独读取冻结的 bundle，在沙盒中真实解题，产出完整 trajectory、最终结果和评分，关联回输入 bundle。任务包存在、RED 通过或已启动 solver，均不能代替这一步的真实执行结果。

已有独立 `prepare-rollout → execute-rollout → read-results --plan-dir ...` 路径，可读取 `harbor_bundle/task`。独立结果读取核对 plan、dataset、执行收据及每个 trial 的任务绑定，调用既有 Hermes 认证，并与内联流程共用 `harbor_ags.response_acceptance` 的最终响应验收。结果写入 plan 目录的 `rollout_results.json`，其中 `acceptance.status=PASS` 只表示本次 rollout 通过；不回写重建状态，不单独声明 RED、SFT 或完整认证关闭。本次接入仅经合成 fixture 与离线回归，真实两阶段交付仍待验证。

```text
harbor_bundle/
├── task/
│   ├── task.toml
│   ├── instruction.md
│   ├── workspace/        # agent 可写的 task-start 环境
│   ├── environment/      # Harbor/AGS 运行环境声明
│   ├── solution/         # 仅验收端使用的参考，不提供给 agent
│   └── tests/            # verifier、control、rubric
├── dataset.toml
└── artifact_manifest.json
```

bundle 是交付入口；运行轨迹单独位于 Harbor job trial 的 `agent/trajectory.full.json`。运行清单应关联 bundle、输入哈希、trial、verdict/reward、轨迹与 cleanup 证据，避免只交付报告或散落的调试文件。

## 已核实的运行证据

以下路径相对于部署机项目根目录 `/mnt/afs_toolcall/wujian1/Projects/workspace/TraceRconstruction`。保留历史证据原位，不用新规则回写旧结果。

| 运行 | 已有证据 | 结论和限制 |
| --- | --- | --- |
| `artifacts/r04-line41-e2e-v28/` | 已有真实 `trajectory.full.json`。任务 `rawtask_6b7d0cf92a8e2b17` 的 `hermes-replay-v5` 两轮完成且无基础设施错误，但 reward 为 0/0；v6/v7 也为两轮 0/0，v9 已有一轮 reward 0 | `verification.json` 为 REVIEW、`sft_eligible=false`，包含 acceptance-report block count、NON_FILE 响应未验和解题复验失败。证明有真实尝试和轨迹，不证明合格交付 |
| `artifacts/r04-line41-e2e-v30/` | Intent、Completion、Sufficiency 产物；`verification/round-01.json` 为 PASS，NOP/oracle/mutation 数值完成校准；Hermes 实际进入沙盒执行 | Verifier 的报告格式/关键词检查不足以证明评审语义。Hermes 中断后缺少 `hermes-result.json`，导致 `TrajectoryCaptureError`；已见 SIGTERM 处理记录，不能据此断定信号发送者。job 保留一轮错误、一轮待运行，非完整验收 |
| `artifacts/r04-line41-e2e-v31/` | 已生成任务、环境合同、TaskFit；`verification/round-01.json` 为 PASS | RED 完成后由本次调试代理主动停止；不是模型自然结束或只有 NOP 结果。终止后也出现缺少 `hermes-result.json` 的 capture error，不应将主动停止后的异常另算成自然失败。环境探针、任务绑定及验证语义仍需修正 |
| `artifacts/r04-line41-source-fixes-v32/` | 原始 session 的 segmentation、Replay、Intent 及 Completion 运行证据；真实 AGS 写入出现 `PermissionError`，另有 22 个误提取的 lock 支持文件缺口 | 自然结束于 Completion REVIEW，后续阶段未完成。对应源码修复已合入 `e6aa97a`；v34 的真实 Completion 写入已确认权限修复；不回写旧结果 |
| `artifacts/r04-line41-source-fixes-v33/` | segmentation、Replay 及 Intent 原始输出；根 JSON 含多余引号，解析器误取内层对象后报告缺少 task | 自然结束于 Intent，无 Completion、RED 或 solver rollout。`fd41fdc` 已修复解析与一次格式纠正；v34 已通过 Intent，但不代表其纠错分支已被实际触发 |
| `artifacts/r04-line41-reconstruction-v34/` | Intent、Completion、Sufficiency READY，TaskFit READY_ORIGINAL；真实 Completion 写入成功，第二轮语义审查为 REVISE | 自然结束于 Verification REVIEW；源码分段上下文与 verifier 语义仍有质量缺口，无 RED、交付 bundle 或 solver 轨迹。修复已整合，尚待真实复验 |

v28 的一份可核对 job 为：
`tasks/rawtask_6b7d0cf92a8e2b17/verification/jobs/hermes-replay-v5/29843d809d1016d25e59d0428698fc16905dbec0b11676a6711a1d95c095b879/result.json`。

v30/v31 使用已有 records 的 `reconstruct run` 路径，不等于完整原始 session 的 `raw-run` 已获端到端通过。旧产物也不代表最新源码的运行结果。

line 41 的任务是只读代码审查：阅读 brief、先前报告和相关源码，写出评审报告并返回规定响应。**缺少 Cargo.toml 本身不能证明任务不可重建。** 应判断待审源码与上下文是否足够、文件和报告操作是否可执行、结论是否能被独立验收；只有任务确实需要编译时才要求相应构建能力。

## 2026-09-24 历史问题基线

| 模块 | 已确认的问题 | 对产物的影响 |
| --- | --- | --- |
| Intent / 环境绑定 | 路径提取会把 `Critical/Important/`、`panic/cancellation/` 等描述词，以及全文中的示例路径纳入候选路径。v31 任务合同已经出现这类污染 | Completion 可能为了满足错误绑定创建无关目录或占位文件；必须修复信息提取与任务绑定，不能统一归因于数据不足 |
| Completion / Sufficiency | 当时先生成候选，再逐个评估，没有后续充分性或执行问题返回 Completion 的跨模块修复闭环 | 能报告缺口，但还不能稳定地修复缺口并重新交付合格环境 |
| 环境执行验收 | 上下文充分性与探针结果已分开；真实 rollout 要求 load/reset/dependency 收据。三个探针成功只证明实际检查过的能力 | 不能用固定探针数量证明任意任务可解；探针应对应任务需要，审查任务不能被默认要求编译工程 |
| Verifier / 最终响应 | 真实 rollout 模式在启动前阻断所有未验 NON_FILE 义务，其中包括 acceptance-report；既有 response receipt 却要等 rollout 后才能生成 | 存在先后顺序冲突。最终响应验收尚未形成完整闭环，不能声称所有义务已经覆盖 |
| RED / 内容质量 | NOP 失败、oracle 成功、mutation 失败能够检查候选验证器的区分能力，但当前样本的报告语义验证仍弱 | RED 数值 PASS 不等于报告结论正确、环境真实或任务合理 |
| rollout 生命周期 | 已加入子进程会话隔离，降低普通 SSH 断开影响 | 隔离不能防止显式 SIGTERM，也不能代替一次完成并保全轨迹的真实复验 |

以上表格描述 `90212de` 时的历史问题，不是当前源码清单。对应修复见页首 2026-09-27 表格；旧运行记录保持原状，修复是否改善真实交付仍须新运行确认。

## 状态字段如何理解

- Completion 的 `READY`：候选通过该阶段契约，不证明环境可用或任务可解。
- Sufficiency 的 `READY`：模型及相应结构检查认为上下文足够，执行探针另存。
- `environment_contract.status/context_status`：上下文状态；`execution_readiness=PROBED` 才表示要求的探针收据完整。`execution_status=EXECUTABLE` 是派生标签，不是独立证明。
- `task_fit.decision=READY_ORIGINAL`：已有环境与原任务的义务映射可支持继续验证，不表示目标已经实现。
- `verification.status=READY`、`calibration=PASS`：重建验证器通过 RED，真实 rollout 另查。具备完整响应合同和通过语义审查的待验响应义务，可保持重建 READY，但继续留在 `unverified_obligations`，不能因此关闭认证。
- `sft_eligible=true`、`certification_closed=true`：代码要求真实复验、质量门禁和义务覆盖都完成。仍应核对实际文件与语义，不能只读一个布尔值。

环境上下文 READY 与执行 FAILED 可以同时出现，这是两种判断，不能直接视为合同自相矛盾。当前执行门禁检查的是 `execution_readiness`，而不是独立读取 `execution_status`。

## 数据与批处理状态

现有 `artifacts/r04-r05-all-sessions-v1/source_manifest.json` 为 READY、`coverage_complete=true`，记录 R04 6535、R05 1694，共 **8229 个 session**；清单中两组记录数、字节数、SHA256 与来源 distribution 一致，非法 JSON 行和重复物理行计数均为零。

冻结输入在 `return_data/four_batch/frozen_r04_r05/`，批次清单在 `artifacts/r04-r05-all-sessions-v1/sessions.jsonl`。这是现有 source_manifest 的记录，不代表本次重新扫描全部数据，也不代表 8229 条已完成重建。`return_data/four_batch/by-rubric/` 中旧 R04/R05 副本仍被截断，不能用来代替冻结全量输入；这一限制不适用于已经冻结并通过覆盖核对的副本。

## 查看一条结果的顺序

1. `reconstruction_manifest.json`、`stage_metrics.json`：任务列表、执行过的阶段、停止原因。
2. `intent/intent.json`、`tasks/<task_id>/task_contract.json`：任务是否保留用户目标；输入路径、输出路径和义务是否合理。
3. `tasks/<task_id>/replay.json`、`completion/completion.json` 和候选 workspace：观察事实与生成内容是否分开，关键源码是否可用，有无错误页或占位符。
4. `sufficiency/*/sufficiency.json`、`environment_repairs/*/repair_audit.json`、`environment_contract.json`、`task_fit.json`：缺口、真实探针、义务与环境映射。探针覆盖的是否就是任务需要的能力。
5. `verification/round-*.json`、`verification.json`：RED 执行及失败诊断；检查 verifier 内容，不只看 PASS。
6. `verification/deliverables/hermes-replay/harbor_bundle/`：任务、初态、参考和测试的可见边界。编排未完成时该目录可能不存在。
7. `verification/jobs/` 下的 trial 和 `agent/trajectory.full.json`：真实命令、模型响应、最终输出与验收是否一致，是否正常结束并保全证据。

历史结果用来定位缺陷；新交付必须从修复后的源码重新运行，不能手改旧 reward、补造轨迹或把参考结果当作真实 agent 输出。

## 2026-09-24 源码整合与清理

主工作目录已从 `e1feb64` 快进到包含上游修复与 response receipt 的整合代码。本次离线验证对应源码提交 `b149265`；后续维护文档提交不改源码。

- 已修复 read 输出的 hash 行号解析；显式 raw 读取保留原始字节，不再重复解包；缺失文件错误不作为源码。原始轨迹与旧 workspace 不回写。
- 已允许 Completion 在漏传证据编号后补正工具参数；第一次无证据的写入仍被拒绝，保护文件等权限限制保持生效。
- 已同步 rollout 与 capture 时限，并真实记录 EXECUTING / COMPLETED / FAILED / TIMEOUT / ABORTED；异常记录不宣称沙盒已经清理。
- 删除两个过时导出脚本、一个一次性调试脚本和无调用充分性兼容函数及其镜像测试，共删除 478 行。
- 清理 47 项可再生缓存、重复日志和已完成修改的一次性编辑文件，合计 11,345,110 字节。保留真实数据、配置、运行证据和交付包。

主目录原有未提交改动完整保存在本地 Git 引用 `refs/backup/pre-integration-20260924`（`95ce199`，20 个已跟踪文件、3 个未跟踪文件）。经过审查的修复已迁入；包含新上游 schema 硬门禁的 `task_instruction.py`、旧 TaskFit 严拒规则和临时 sys.path 修改没有直接覆盖当前实现。备份不作为生产实现，也未上传远程。

离线回归：`690 passed, 5 skipped, 5 warnings in 91.68s (0:01:31)`。`ruff check src tests scripts --select E9,F63,F7,F82 --no-cache` 与 `git diff --check` 通过；这不表示全部 Ruff 规则已通过。详细记录在部署机 `artifacts/maintenance-20260924/`，产物索引在 `artifacts/README.md`。

2026-09-24 这次维护没有发起新的模型 rollout。上述 690 项通过只对应当时源码；2026-09-27 后续修复的回归与真实验证状态见页首，不能沿用历史数字宣布新版本通过。
