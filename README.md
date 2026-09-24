# TraceForge

TraceForge 从真实 session 中恢复用户任务、补全 task-start 环境并生成行为验证器，交付 Harbor 任务包；真实 agent 在包中执行后，再交付执行轨迹和验收证据。

**当前尚无可信的完整端到端验收结果。** 已有真实重建、RED 校准和解题轨迹，但任务绑定、环境补全反馈与输出义务验收仍有未解决问题。最新审计结论、历史运行证据和待修事项统一见 [当前状态](docs/current-status.md)，不能仅凭某个阶段的 `READY` 宣布任务已完成。

## 阅读入口

1. [当前状态与交付标准](docs/current-status.md)
2. [原始会话处理流程](docs/raw-session-pipeline.md)
3. [源码阅读地图与契约](docs/rebuild-live-map.md)
4. [AGENTS.md](AGENTS.md)：唯一开发规范

## 当前入口

R04/R05 按原始 session 处理使用 `reconstruct raw-run`；`reconstruct run --records` 保留给已有筛选记录的路径。两者进入共同的重建主链。

```text
原始 session → 任务分段 → Intent → Replay/Route
→ Completion → Sufficiency → Environment Contract / TaskFit
→ Verifier / Harbor RED → Harbor bundle
→ 独立的 Hermes rollout 与完整验收
```

以下命令需要配置可用模型、Hermes、Harbor/AGS 环境；行号对应冻结输入，运行目录必须唯一：

```bash
PYTHONPATH=src python -m traceforge reconstruct raw-run \
    --input return_data/four_batch/frozen_r04_r05/R04.jsonl \
    --line-number <原始行号> \
    --output artifacts/terminal-live/<run-id> \
    --config config.yaml \
    --hermes-home "$HERMES_HOME" \
    --sandbox \
    --execute-red \
    --execute-rollout \
    --rollout-trials 2 \
    --rollout-timeout-seconds 14400 \
    --rollout-max-iterations 500
```

`--execute-red` 校准重建任务的验证器；`--execute-rollout` 追加真实解题复验。这两个阶段在产物和资格判断上分开。批处理入口为 `scripts/prepare_session_batch.py`、`scripts/run_session_batch.py`，输入清单说明见 [原始会话流程](docs/raw-session-pipeline.md)。

## 开发检查

```bash
uv sync --dev --python 3.12
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

这些是开发检查命令，不代表当前版本已经通过所有检查。离线测试、命令退出码或单个 RED 数值均不能替代真实产物验收。

原始轨迹提供重建依据；生成内容必须区分观察事实和补全推断。Completion 不提前实现用户目标，也不把参考答案或隐藏测试放进 agent 工作区。`config.yaml`、真实数据、凭据和运行产物不进入 Git。
