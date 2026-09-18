# 模型网关配置

重建与 AgentRx 的模型调用支持两种后端：默认的 `TOKENHUB_KEY` 环境变量加 Claude Messages API，以及 `config.yaml` 中的 NewAPI channel。配置文件只在进程内读取，密钥不会写入调用回执、响应对象、日志或其他 artifact；`config.yaml` 已被 gitignore，不能提交到仓库。

Gemini 测试示例：

```bash
PYTHONPATH=src .venv/bin/python -m traceforge failure-analysis agentrx \
  --trajectory-json trajectory.json \
  --output agentrx.json \
  --config /mnt/afs_toolcall/wujian1/Projects/workspace/RraceRconstruction/config.yaml \
  --channel gemini \
  --model-name gemini-2.5-pro
```

`model-judge` 和 `reconstruct run` 同样支持 `--config` 与 `--channel`。当配置了 NewAPI channel 却仍使用默认的 Claude 模型名时，CLI 会自动选择该 channel 的安全默认模型（Gemini 为 `gemini-2.5-pro`，GPT 为 `gpt-5`）；需要其他模型时显式传入 `--model-name`。

配置格式是顶层 channel 名和一行 JSON 连接对象，例如：

```yaml
gemini:
  {"_type":"newapi_channel_conn","key":"<secret>","url":"https://tokenhub.sensetime.com"}
```

如果网关返回 HTTP 503，应先检查 channel 路由、模型名和服务状态；客户端会保留错误码但不会把响应正文（可能包含敏感信息）写入 artifact。
