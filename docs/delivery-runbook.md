# Agent World 重建交付说明

本文是当前代码的可执行交接记录。原始回流数据只读，所有运行产物写入被 git 忽略的 `runs/`。

## 目标与重建单位

输入是一条 session 的编译轨迹，但重建单位是 **QueryTurn**：从连续 USER 边界切出一个用户任务及其之后可归属的工具调用。整条 session 不能直接作为一个 task，因为真实样本中 50/50 个未完成 capture 都包含多个 query（439 个 USER、2150 个 TOOL_CALL、99 个缺失结果）。

`trajectory/evidence_join.py` 负责从 `event_occurrences.jsonl` 选择事实并生成 `task-reconstruction-input.v1`。它只引用存在的 event occurrence；缺失引用写入 `missing_evidence_ids` 并将 `quality.requires_review=true`，不从 metadata 猜测任务。真实可用样本：

`runs/r01/task_inputs/3d79-last-query.json`

该样本包含一个用户 query、一个无结果的 TOOL_CALL、0 个缺失引用，`usable=true`，并保留 `pending_tool_call_count=1`。

## 端到端机制

```text
sessions.jsonl (只读)
  -> trajectory compile / event_occurrences
  -> evidence_join (QueryTurn + evidence hash)
  -> failure_analysis (规则；不确定则 REVIEW)
  -> task_recovery (模型重述；只允许 evidence refs)
  -> trajectory_replay (只重放初始可观察状态)
  -> environment_completion (模型补全候选；只写 evidence-backed 文件)
  -> sufficiency_judge (任务-环境可执行性)
  -> verifier/synthesis + compile_bundle (Harbor public/hidden)
  -> Harbor plan -> Hermes rollout
  -> oracle/no-op/mutation RED-check
  -> SFT curation (pass-only)
```

每个阶段发布带 schema、hash 和 receipt 的 artifact；模型的 prompt/response 仅在阶段的 `private/model_exchange.json` 留存，调用回执不含 key。`ControlPlane` 对顺序、证据数、候选上限、非法计数、未知上游状态、重试预算和 artifact hash 进行 fail-closed 门禁。

## Harbor 映射

bundle 中 `instruction.md`、`task.toml` 和公开 workspace 是 public；`solution/`、`tests/grader.py`、`tests/control/` 是 hidden verifier/control。环境补全阶段只复制 replay-files 中经过 hash 校验的 UTF-8 文件，原 workspace 的未索引文件不会进入 public，从而防止答案泄漏。模型提出的文件必须有 evidence refs，且不能覆盖 COMPLETE/UNKNOWN 文件。

## SFT 进入条件

候选必须同时满足：verifier `PASS`、reward 达到配置阈值、task/environment confidence 和 trajectory quality 达标、`solution_leakage=false`、`reproducible=true`。`reproducible=true` 只有在要求的 rollout 次数全部存在且全部 PASS（至少两次）时成立；缺字段进入 REVIEW，失败直接 REJECT。当前代码只做筛选和 JSON artifact，不隐式启动训练。

## 验收命令

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m traceforge trajectory compile ...
PYTHONPATH=src .venv/bin/python -m traceforge reconstruct workflow \
  --report-json ... --evidence-json ... --replay-workspace ... \
  --replay-files-json ... --harbor-root harbor_ags \
  --config config.yaml --channel deepseek \
  --model-name vol/deepseek-v4-flash-0731 \
  --rollout-model <provider/model> --rollout-trials 2 --execute-rollout
```

首次交付建议先 `--execute-rollout` 不启用，检查所有 artifact 和 bundle；再在已配置的 Harbor/AGS 节点启用真实执行。Hermes rollout 模型必须显式使用 `provider/model`，避免把重建模型名误当 Anthropic。DeepSeek channel 的安全默认模型为 `vol/deepseek-v4-flash-0731`。

## 已验证与边界

本地 Python 3.12 全量 pytest 已通过；远程 Linux venv 正在执行同一套测试和 Harbor 依赖恢复。真实数据已完成证据投影和 50 条统计，但模型重建与 Hermes/AGS 真实 rollout 必须以远程凭据和可用 sandbox 的最终日志为准。若证据不足、Verifier RED-check 未通过、Harbor quality gate 失败或 rollout 结果不完整，结果只能是 REVIEW/REJECT，不得进入 SFT。
