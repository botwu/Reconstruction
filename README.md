# TraceForge

从真实 session 恢复用户任务和任务初始环境，再执行真实 agent rollout。输入由调用方明确标为 `search` 或 `terminal`，两者使用不同的环境与工具。

已取得两种 domain 的真实执行产物。阶段通过、文件评分和回答内容正确是不同结论；最新证据与未完成事项见 [当前状态](docs/current-status.md)。

## 跨机器调试

代码获取、完整 R01/R04 数据恢复及哈希校验见[跨机器调试](docs/cross-machine-debug.md)。

## 入口

```bash
PYTHONPATH=src python -m traceforge reconstruct raw-run \
  --input return_data/four_batch/by-rubric/R04.jsonl \
  --line-number 1 --domain terminal \
  --output /path/to/new-run --config /path/to/config.yaml \
  --hermes-home /path/to/hermes-agent --harbor-root /path/to/harbor_ags \
  --execute-red --execute-rollout --rollout-trials 2 \
  --manual-response-review
```

`--manual-response-review` 适用于用户同意逐份人工核查自由文本分析的任务；文件验证照常执行，未核查的回答不计为通过。纯文件任务不需要此选项。

terminal 默认使用 AGS；`--no-sandbox` 仅供离线诊断。search 使用检索和网页读取服务，不依赖 AGS，不走文件补全与 pytest 路径。模型、凭据和执行预算从配置读取，配置说明见 [模型连接](docs/model-gateway-config.md)。

两种 domain 均导出原生 Harbor 任务目录，供后续独立 rollout 使用。terminal 保留已有验证器；search 当前使用 `--disable-verification` 并单独核查回答。目录、运行方式和已知边界见 [Harbor 任务交付](docs/harbor-task-delivery.md)。

批处理使用 `scripts/prepare_session_batch.py` 和 `scripts/run_session_batch.py`；执行时同样必须显式传入 `--domain search` 或 `--domain terminal`。每条 session 单独记录最终清单和错误，不能以进程结束代替产物验收。

## 阅读与开发

- [处理流程与产物](docs/raw-session-pipeline.md)
- [作者、独立检查和反馈循环](docs/researcher-pipeline.md)
- [模块阅读地图](docs/rebuild-live-map.md)
- [开发规范](AGENTS.md)

```bash
uv sync --dev --python 3.12
uv run pytest -m 'not live'
uv run ruff check src scripts --select F,E9
```

普通测试不发起模型请求；真实验证须另外检查输入、环境、校准、rollout 和实际产物。原始 session、配置、凭据及运行结果不提交到 Git。
