# TraceForge

从真实 session 恢复用户任务和任务初始环境，再执行真实 agent rollout。输入由调用方明确标为 `search` 或 `terminal`，两者使用不同的环境与工具。

已取得两种 domain 的真实执行产物。阶段通过、文件评分和回答内容正确是不同结论；最新证据与未完成事项见 [当前状态](docs/current-status.md)。

## 两种重建策略

| | search（R01） | terminal（R04） |
| --- | --- | --- |
| 恢复目标 | 原任务、必要历史、可检索且可追溯的资料及读取工具 | 原任务、任务开始前的目录、源码、配置、数据、依赖及执行能力 |
| 补全方式 | 保留原始捕获；agent 沿轨迹线索检索、fetch 真实正文并核对时间和版本 | 按读写时序恢复初态；结合原始片段、补丁修改前内容及真实源码/依赖补全，记录必要推断 |
| 实际检查 | 逐项检查任务所需材料是否交付、可读且足够，再核对真实回答的事实与引用 | 运行任务相关入口和已有行为；文件验证器经独立审查及 RED 校准，再检查真实 rollout |
| 返修依据 | 恢复漏交资料、必要历史或检索能力；solver 已有资料却答错时保留解题错误 | 修复实证的初态、依赖或执行缺口；区分验证器错误、solver 错误和基础设施故障 |
| Harbor 交付 | 任务、证据语料和检索工具；当前回答内容保留人工核查 | 任务、初始工作区和运行环境；隐藏测试与参考解仅供验收端使用 |

两者共用原始 session 保留、任务分段、工具语义解析和目标恢复；环境构建与验证分别执行。
domain 由调用方指定，Codex / Hermes / Claude Code / OpenClaw 等 harness 用于解读原工具协议。
search 包含本地代码和文档检索，terminal 补全也可以联网取真实材料；是否出现 shell 或网页工具不改变 domain。
完整要求与实现边界见[原始会话重建流程](docs/raw-session-pipeline.md)。

## 跨机器调试

代码获取、完整 R01/R04 数据恢复及哈希校验见[跨机器调试](docs/cross-machine-debug.md)。

## 入口

search：

```bash
PYTHONPATH=src python -m traceforge reconstruct raw-run \
  --input return_data/four_batch/by-rubric/R01.jsonl \
  --line-number 38 --domain search \
  --output /path/to/new-search-run --config /path/to/config.yaml \
  --hermes-home /path/to/hermes-agent \
  --execute-rollout --rollout-trials 2 --manual-response-review
```

terminal：

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

terminal 默认使用 AGS；`--no-sandbox` 仅供离线诊断。search 使用已交付语料，按任务需要启用公开检索和网页读取；重建主链不依赖 AGS，不走文件初态补全与 pytest 路径。模型、凭据和执行预算从配置读取，配置说明见 [模型连接](docs/model-gateway-config.md)。

两种 domain 均导出原生 Harbor 任务目录，供后续独立 rollout 使用。terminal 保留已有验证器；search 当前使用 `--disable-verification` 并单独核查回答。目录、运行方式和已知边界见 [Harbor 任务交付](docs/harbor-task-delivery.md)。

[批量执行与断点续跑](docs/batch-reconstruction.md)使用 `scripts/prepare_session_batch.py` 和 `scripts/run_session_batch.py`；执行时同样必须显式传入 `--domain search` 或 `--domain terminal`。每条 session 单独记录最终清单和错误，不能以进程结束代替产物验收。

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

普通测试不发起模型请求；真实验证须另外检查输入、环境、校准、rollout 和实际产物。两份原始数据按用户明确授权通过 Git LFS 发布；部署配置、凭据及运行结果不提交到 Git。
