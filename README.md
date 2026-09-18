# TraceForge

TraceForge 从真实回流 session 重建可验证任务与环境，并在 Harbor 上做 RED 校准。

## 开始之前

1. [AGENTS.md](AGENTS.md)：唯一开发规范
2. [reconstruct run 阅读地图](docs/rebuild-live-map.md)：当前主链、要改的文件、对照产物

## 当前主链

```text
原始 JSONL + screening records
→ reconstruct run
→ Intent → Completion → Sufficiency → Verifier
→ Harbor RED（--execute-red）
```

入口是 `reconstruct run`，不是轨迹编译。筛选用 `screening run`。

```bash
PYTHONPATH=src python -m traceforge reconstruct run \
    --input return_data/four_batch/by-rubric/R01.jsonl \
    --records <records.jsonl> \
    --line-number 22 \
    --output artifacts/eligible-live/L22 \
    --config config.yaml \
    --channel claude \
    --model-name claude-opus-4-6 \
    --hermes-home "$HERMES_HOME" \
    --sandbox \
    --execute-red
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
