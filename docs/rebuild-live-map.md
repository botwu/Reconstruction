# reconstruct run 阅读地图与清理清单

以源码为准。活跑入口只有 `reconstruct run`。轨迹编译 / lineage / query-turns / source-projection 已删除。

READY 仍表示：Harbor 对**初始 workspace** 做出 RED（nop FAIL / oracle PASS / mutation FAIL）。没有 RED 不能把交付标成 READY。

`reconstruct source` 只抽源、不调模型。`screening run` 产出 records，供 `reconstruct run --records` 使用。

## 主链

```text
R01.jsonl + screening records.jsonl
  → session_source（reconstruction_source）
  → env_replay（Stage1，无模型，按路径回放 bE0）
  → Intent（q + environment_bindings）
  → execution_support_route（Intent 后重算）
  → Completion（有回放则 from_replayed，否则 from_default_empty）
  → Sufficiency（workspace_sufficiency，不是 sufficiency.py）
  → Verifier（仅当 allow_file_verifier）
  → Harbor RED（--execute-red）
  → Hermes rollout（--execute-rollout，只写 SFT，不改 READY）
```

每次重建还会在 `tasks/<task_id>/task_environment_pair.json` 保存论文中的
`q/E` 对：`q` 只投影已通过 Intent 的用户目标、验收义务和环境绑定，`E` 记录回放证据、部分文件、 withheld 变更、候选和验证状态。Intent 未恢复时只写 `unrecovered` 审计记录，不生成执行指令。根目录的 `stage_metrics.json` 汇总 Intent、Completion、Sufficiency、Verification、路由和停止原因，便于区分“未运行”“审计态”和“真正通过”。

SFT 只从真实 rollout 的 `quality_gate`、trial、cleanup、轨迹和内容证据生成；缺少泄漏、可复现或奖励证据会进入 `REVIEW`，不会因为旧的 `sft_eligible` 标志而放行。

```mermaid
flowchart LR
  jsonl[R01.jsonl]
  records[screening records]
  source[session_source]
  replay[env_replay]
  intent[Intent]
  route[execution_support_route]
  completion[Completion]
  suff[Sufficiency]
  ver[Verifier]
  harbor[Harbor RED]
  jsonl --> source
  records --> source
  source --> replay
  source --> intent
  replay --> route
  intent --> route
  route --> completion
  completion --> suff
  suff --> ver
  ver --> harbor
```

编排在 [`src/traceforge/cli.py`](../src/traceforge/cli.py) → [`eligible_reconstruction.py`](../src/traceforge/reconstruction/eligible_reconstruction.py)。

`--sandbox` 才把文件 / pytest 打到 AGS。默认是宿主机 Hermes 代理，不是论文里的容器 Agent。缺 `AGS_API_KEY` / `E2B_API_KEY`（或 `config.yaml` 里的 key）直接失败，不要假装已经容器验收。

## 按这个顺序读源码

每文件只抓括号里的点。

**编排**

- [`eligible_reconstruction.py`](../src/traceforge/reconstruction/eligible_reconstruction.py)：筛选回放 → Intent → `_task_result` 重算路由 → Completion → Sufficiency → Verifier。有回放文件就走 `TERMINAL_FILE` 且 `allow_completion=true`。`allow_file_verifier` 只看 Intent 是否给出了明确 FILE 义务；为假时直接 `NO_FILE_ACCEPTANCE`，Harbor 不会启动。`TREE_TOO_THIN` 已从当前路由去掉。
- [`session_source.py`](../src/traceforge/reconstruction/session_source.py)：原始行 + 筛选记录 → `tool_timeline` / user texts。

**环境（Stage1，无模型）**

- [`env_replay.py`](../src/traceforge/reconstruction/env_replay.py)：按路径回放。具名 dump、只读探测、`cl /Zs` 语法检查、真写屏障都在这里。
- [`environment_bindings.py`](../src/traceforge/reconstruction/environment_bindings.py)：FILE / NON_FILE。`derive_binding`、`FILE_BINDING_PATHS`、listing 丢路径。NON_FILE 不得带 path。

**各阶段实现与契约**

- Intent：[`intent_recovery.py`](../src/traceforge/reconstruction/intent_recovery.py) + [`roles.py`](../src/traceforge/reconstruction/agents/roles.py) 的 `INTENT_ROLE`。看 `deepen_requires_file`、`_file_obligation_ready`、`FILE_OBLIGATION_REQUIRED`。义务证据只能是 `user:<message_index>`。
- Completion：[`workspace_completion.py`](../src/traceforge/reconstruction/workspace_completion.py) 的 `complete_from_replayed` / `complete_from_default_empty`。
- 写路径：[`session.py`](../src/traceforge/reconstruction/agents/session.py) 的 `workspace_relpath` / `_write_file`；[`runtime.py`](../src/traceforge/reconstruction/agents/runtime.py) 里 `os.chdir(hermes_scratch)` 和 `path_aliases`。
- 沙盒：[`sandbox.py`](../src/traceforge/reconstruction/agents/sandbox.py)、[`container_verification.py`](../src/traceforge/reconstruction/container_verification.py)。
- Sufficiency：[`workspace_sufficiency.py`](../src/traceforge/reconstruction/workspace_sufficiency.py)。活跑用这个。
- Verifier / Harbor：[`verification.py`](../src/traceforge/reconstruction/verification.py)、[`verifier/synthesis.py`](../src/traceforge/verifier/synthesis.py)（oracle 静态门按**写入目标**判断）、[`verifier/bundle.py`](../src/traceforge/verifier/bundle.py)（`solve.sh` 仍把 oracle 拼进 `#!/bin/sh`）。

**已知活跑卡点（改代码时对着看，本文不改逻辑）**

1. Intent：当前 prompt 要求保留用户原始目标和意图类型，并为每条验收义务显式给出一个 binding。完整环境不足时不得把实现/修复/评审改写成计划；缺 binding 或 FILE 路径不充分会进入 REVIEW。纯只读评审、解释和报告可以保持 NON_FILE，不再被强制伪造 FILE。
2. Replay：OpenCode filePath、<path>/<content> 包装、PowerShell 行号和只读 rg | sed 已纳入解析；未知写操作仍建立 barrier，后续内容只保留为私有证据，Completion 的证据索引、洞卡片和目录解析都会过滤这些事件。
3. Completion：目标 FILE 路径必须是真实上下文；全是 skip/pass 的测试骨架会以 `BINDING_PATH_TEST_SKELETON` 进入 REVIEW。仍需观察模型是否把写入落在 workspace，而不是 hermes_workspace；看 workspace_relpath、cwd 和 completion.json 的 evidence refs。
4. 全 NON_FILE 时 allow_file_verifier=false，管线可完成 Completion/Sufficiency，但在文件 Verifier 前以 NO_FILE_ACCEPTANCE 保持 REVIEW；这属于输出型验收尚未接入独立回执协议。
5. terminal selector 只产出 CANDIDATE_ONLY。当前本地 R04/R05 是截断采样（R04 297/6535、R05 336/1694 物理行，sha 不匹配），不能宣称覆盖上游全量；模型证据超过预算时 fail-closed，不删除中间事件。
6. 不要发明源码、不要写解题、不要写目标测试。Intent 只引用用户原文。web_search 只给 Completion，且不得把检索到的源码写进用户路径。

## 对照产物（只读）

看一份产物时只盯：`reconstruction_manifest.json` 的 `stopped_at` / `execution_support_route` → `intent/intent.json` 的 `environment_bindings` → `tasks/*/completion/completion.json` → 若有则 `verification/verification.json`。

- [`artifacts/eligible-live/L22-e2e-5/`](../artifacts/eligible-live/L22-e2e-5/)：历史活跑对照产物，当前状态为 REVIEW。Intent READY、三条全 NON_FILE；Completion `MODEL_DECISION_REVIEW`。
- [`artifacts/eligible-live/L22-e2e-3/`](../artifacts/eligible-live/L22-e2e-3/)：旧规则下走到 Verifier 的历史对照，当前状态为 REVIEW。

`artifacts/` 已被 gitignore，不会进仓库。

## 活跑命令

下面只作为需要凭据和 AGS/E2B 的参考命令，不属于普通回归步骤：

```text
export HERMES_HOME=/mnt/afs_toolcall/wujian1/Projects/tokenhub_data_model_eval/R01/hermes-agent
RECORDS=/tmp/traceforge-l22-v10-wrapped/records.jsonl

PYTHONPATH=src python -m traceforge reconstruct run \
    --input return_data/four_batch/by-rubric/R01.jsonl \
    --records "$RECORDS" \
    --line-number 22 \
    --output artifacts/eligible-live/L22 \
    --config config.yaml \
    --channel claude \
    --model-name claude-opus-4-6 \
    --hermes-home "$HERMES_HOME" \
    --sandbox \
    --execute-red
```

加 `--execute-rollout` 只做解题复验。`config.yaml` 只在部署机，已 gitignore。

## 仍有效的专题文档

- 筛选：[reconstruction-screening-plan.md](reconstruction-screening-plan.md)、[reconstruction-screening-rubric.md](reconstruction-screening-rubric.md)
- 模型通道：[model-gateway-config.md](model-gateway-config.md)
- Harbor 计划适配：[harbor-ags-boundary-adapter.md](harbor-ags-boundary-adapter.md)
- 失败分析复用：[failure-analysis-reuse.md](failure-analysis-reuse.md)

## 清理清单

本次已删除过时文档，见上一节。下面是磁盘与代码，**不在这次提交里删产物**。

**可删（误文件 / 缓存，不影响主链）**

- `.pytest_cache/`、`.ruff_cache/`
- `artifacts/research-goal-audit-20260918-regression.log`（若还在）

**体积大、可再生成；删前先确认不需要对照**

- 各次活跑里的 `hermes_scratch/`、`sandbox_workspace/`、`verification/agent/round-*/_pytest_runtime/`。保留同目录 `*.json` 即可复盘。
- `artifacts/eligible-live/path-validation-20260917/`：L33/L37/L49 路径验收，不是 L22 主对照。
- `artifacts/eligible-live/run-live/`：分段 Completion 实验。
- `/tmp/run_l22_completion_grounded.py`、`/tmp/traceforge-l22-*`：宿主机临时脚本和旧 records。活跑命令仍可能引用 wrapped records，删前先看启动命令。

**保留**

- `L22-e2e-3`、`L22-e2e-5` 的 manifest / intent / completion / replay JSON。
- `return_data/`、`config.yaml`、`tests/`、`src/traceforge/reconstruction/` 主链。
- 筛选 records（`/tmp/traceforge-l22-v10-wrapped/records.jsonl` 或 v9 records）。

**勿当死代码删**

- `trajectory/` 里剩下的 `json_codec` / `privacy` / `artifacts`：筛选、Harbor、重建源仍在用。
- `failure_analysis/`、`requery/`、`screening/`：有 CLI。
- [`reconstruction/sufficiency.py`](../src/traceforge/reconstruction/sufficiency.py) + [`tests/test_reconstruction_sufficiency.py`](../tests/test_reconstruction_sufficiency.py)：旧契约，活跑走 `workspace_sufficiency.py`，测试还在用。
- [`control_plane.py`](../src/traceforge/reconstruction/control_plane.py)：未接到 `reconstruct run`，有单测；不要当无用直接拆。
- 源码里的 `#region agent log`：调试探针，不是业务。
