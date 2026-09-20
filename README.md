# TraceForge

TraceForge 从真实回流 session 重建可验证任务与环境，并在 Harbor 上做 RED 校准。

当前优先目标是跑通一条真实 terminal 任务：原始轨迹 → 筛选 → 恢复任务和初始环境 → 环境补全与充分性检查 → 隐藏验证器 → RED 校准 → 解题 rollout 与验收。先完成单条闭环，再扩展批量；当前尚无真实 terminal 样本完成这条全链路。

代码中的 `READY` 表示重建产物通过 RED 校准；完整端到端还需单独检查真实解题 rollout 和质量门禁。离线测试通过、命令退出码为 0、审计脚本的 `pipeline_ok=true` 都不能单独证明端到端成功。

## 开始之前

1. [AGENTS.md](AGENTS.md)：唯一开发规范
2. [reconstruct run 阅读地图](docs/rebuild-live-map.md)：当前主链、要改的文件、对照产物

## 当前主链

```text
terminal 原始 JSONL + screening records
→ reconstruct run
→ Intent → Completion → Sufficiency → Verifier
→ Harbor RED（--execute-red）
→ Hermes 解题复验（--execute-rollout）
```

入口是 `reconstruct run`，不是轨迹编译。筛选用 `screening run`。

```bash
PYTHONPATH=src python -m traceforge reconstruct run \
    --input return_data/four_batch/by-rubric/R04.jsonl \
    --records <records.jsonl> \
    --line-number <筛选记录对应的原始行号> \
    --output artifacts/terminal-live/<run-id> \
    --config config.yaml \
    --channel claude \
    --model-name claude-opus-4-6 \
    --hermes-home "$HERMES_HOME" \
    --sandbox \
    --execute-red \
    --execute-rollout
```

## 测试

```bash
uv sync --dev --python 3.12
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

`config.yaml`、真实回流和运行产物不进 Git。

## 核心边界

- 回流提供生成约束，不提供 Ground Truth
- READY 表示 Harbor 对初始 workspace 做出 RED
- 不要发明源码、不要写解题、不要写目标测试
- 真实数据、密钥、模型缓存不进入本仓库

开发规范只引用 [AGENTS.md](AGENTS.md)。
