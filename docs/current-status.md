# 当前状态与交付标准

更新日期：2026-09-27。历史运行保留其原始结论；当前整合修复与新运行状态分开记录。本页集中维护运行状态，流程与源码导航分别见 [原始会话流程](raw-session-pipeline.md)、[阅读地图](rebuild-live-map.md)。

**当前没有一条经过可信完整验收的端到端结果，尚不具备稳定批量交付的证据。** 已经有真实沙盒执行、环境候选、验证器、RED 校准和真实解题轨迹；仍需修复模块之间的语义和反馈关系，并用新运行验证产物质量。增加阻断条件只能避免误报成功，不能替代环境和验证器的改进。

## 目标产物

每条可交付任务应包含合理的独立任务、足够真实且可使用的 task-start 环境，以及能区分正确与错误结果的 verifier。随后真实 agent 在该环境执行，保存完整轨迹与验收证据。重建与 rollout 是两个阶段，任务包存在不表示已有合格解题轨迹。

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


| `artifacts/r04-line41-sourcefix-v32/` | 启动预算校验拒绝低于配置的超时参数 | 没有调用模型，不计作 rollout |
| `artifacts/r04-line41-sourcefix-v33/` | 从原始 R04 第 41 行重新执行，Intent、Replay、Completion、Sufficiency 与 TaskFit 已产出；Verifier 六轮均被同一静态检查拒绝 | 公开源码引用 `crates/fred-core/tests/...` 被误判成隐藏测试访问，未进入 RED 或 rollout；候选还出现路径污染与隐藏结果引用入口，不能交付 |

v9 原始捕获重新核查：传输错误出现在第 47/60 次请求，第 48 次相同请求重试成功；最终响应完整，并非末次请求截断。格式围栏、schema、源码引用与评审内容仍有问题。捕获完整认证与任务通过是两种结论，历史 reward 保持不变。

v28 的一份可核对 job 为：
`tasks/rawtask_6b7d0cf92a8e2b17/verification/jobs/hermes-replay-v5/29843d809d1016d25e59d0428698fc16905dbec0b11676a6711a1d95c095b879/result.json`。

v30/v31 使用已有 records 的 `reconstruct run` 路径，不等于完整原始 session 的 `raw-run` 已获端到端通过。旧产物也不代表最新源码的运行结果。

line 41 的任务是只读代码审查：阅读 brief、先前报告和相关源码，写出评审报告并返回规定响应。**缺少 Cargo.toml 本身不能证明任务不可重建。** 应判断待审源码与上下文是否足够、文件和报告操作是否可执行、结论是否能被独立验收；只有任务确实需要编译时才要求相应构建能力。

## 仍未解决的模块问题

| 模块 | 已确认的问题 | 对产物的影响 |
| --- | --- | --- |
| Intent / 环境绑定 | v31/v33 出现描述词、schema 示例路径和跨义务路径污染；已按原始义务引用过滤，并用 Replay 的工作根对齐路径 | v33 保存的原始模型响应重放为 4 个真实输入、1 个输出，污染归零；仍须新运行确认，缺失的原始输入不补造 |
| Completion / Sufficiency | 当前先生成候选，再逐个评估；没有把后续充分性或执行问题反馈给 Completion 的跨模块修复闭环 | 能报告缺口，但还不能稳定地修复缺口并重新交付合格环境 |
| 环境执行验收 | 上下文充分性与探针结果已分开；真实 rollout 要求 load/reset/dependency 收据。三个探针成功只证明实际检查过的能力 | 不能用固定探针数量证明任意任务可解；探针应对应任务需要，审查任务不能被默认要求编译工程 |
| Verifier / 最终响应 | 整合代码允许显式诊断 rollout，解除响应必须在执行前已有收据的先后矛盾；收据仅验证格式与轨迹绑定 | 合并了结论、数量、报告路径的 NON_FILE 义务仍需内容验收，不能因 JSON 合法就自动清除 |
| RED / 内容质量 | NOP 失败、oracle 成功、mutation 失败能够检查候选验证器的区分能力，但当前样本的报告语义验证仍弱 | RED 数值 PASS 不等于报告结论正确、环境真实或任务合理 |
| rollout 生命周期 | 已加入子进程会话隔离，降低普通 SSH 断开影响 | 隔离不能防止显式 SIGTERM，也不能代替一次完成并保全轨迹的真实复验 |

表中区分了已写入代码的修复和仍未闭合的问题。离线回归不代替从新输入开始的真实复验。

## 状态字段如何理解

- Completion 的 `READY`：候选通过该阶段契约，不证明环境可用或任务可解。
- Sufficiency 的 `READY`：模型及相应结构检查认为上下文足够，执行探针另存。
- `environment_contract.status/context_status`：上下文状态；`execution_readiness=PROBED` 才表示要求的探针收据完整。`execution_status=EXECUTABLE` 是派生标签，不是独立证明。
- `task_fit.decision=READY_ORIGINAL`：已有环境与原任务的义务映射可支持继续验证，不表示目标已经实现。
- `calibration=PASS`：重建验证器通过 RED；不能代表真实 rollout 通过。显式复验失败或有未验证义务时，`verification.status` 保持 `REVIEW`。
- `sft_eligible=true`、`certification_closed=true`：代码要求真实复验、质量门禁和义务覆盖都完成。仍应核对实际文件与语义，不能只读一个布尔值。

环境上下文 READY 与执行 FAILED 可以同时出现，这是两种判断，不能直接视为合同自相矛盾。当前执行门禁检查的是 `execution_readiness`，而不是独立读取 `execution_status`。

## 数据与批处理状态

现有 `artifacts/r04-r05-all-sessions-v1/source_manifest.json` 为 READY、`coverage_complete=true`，记录 R04 6535、R05 1694，共 **8229 个 session**；清单中两组记录数、字节数、SHA256 与来源 distribution 一致，非法 JSON 行和重复物理行计数均为零。

冻结输入在 `return_data/four_batch/frozen_r04_r05/`，批次清单在 `artifacts/r04-r05-all-sessions-v1/sessions.jsonl`。这是现有 source_manifest 的记录，不代表本次重新扫描全部数据，也不代表 8229 条已完成重建。`return_data/four_batch/by-rubric/` 中旧 R04/R05 副本仍被截断，不能用来代替冻结全量输入；这一限制不适用于已经冻结并通过覆盖核对的副本。

## 查看一条结果的顺序

1. `reconstruction_manifest.json`、`stage_metrics.json`：任务列表、执行过的阶段、停止原因。
2. `intent/intent.json`、`tasks/<task_id>/task_contract.json`：任务是否保留用户目标；输入路径、输出路径和义务是否合理。
3. `tasks/<task_id>/replay.json`、`completion/completion.json` 和候选 workspace：观察事实与生成内容是否分开，关键源码是否可用，有无错误页或占位符。
4. `sufficiency/*/sufficiency.json`、`environment_contract.json`、`task_fit.json`：缺口、真实探针、义务与环境映射。探针覆盖的是否就是任务需要的能力。
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

本次维护没有发起新的模型 rollout。前述路径绑定污染、跨模块补全反馈、NON_FILE 验收顺序和 verifier 语义质量仍未完成真实复验，不能因代码合并和离线检查通过就宣布端到端完成。

## 当前源头修复的边界

- 整合主目录的原始字节回放、缺失证据参数重试、真实执行状态记录；同时保留原始用户契约、公开源码片段索引、严格响应格式收据和外部运行时哈希绑定。
- Verifier 路径隔离按隐藏根目录边界判断，公开仓库中的 `tests/` 不再被整段脚本文本误拒绝。
- capture 的单次无数据时限与整个 rollout 的预算分开。共享 Harbor 当前显式配置为 900 秒，源码默认 300 秒；外层 14400 秒预算不会覆盖显式 capture 配置。
- Intent 路径污染、Completion 隐藏结果入口修复已整合，相关回归通过，全量回归和新运行待完成。新运行必须检查真实候选无无关占位文件、无已完成答案，不能复用 v33 候选。
- 当前自动迭代仅覆盖 Verifier 生成和 RED 校准。rollout 失败不会自动回到 Completion 修复；不存在已验证的跨模块无限自修复循环。迭代次数可显式配置，真实运行仍受时间与资源预算约束。
