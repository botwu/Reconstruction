# Harbor 与 AGS 连接层

本目录是项目自建的执行连接层，现纳入 TraceRconstruction 统一版本管理。源码取自实际运行的兼容快照，原始文件哈希见 `provenance.json`；不再依赖项目外的 `workspace/harbor_ags`。

它负责 AGS 环境、Hermes 启动、模型请求捕获、输入与轨迹对账、独立验证器和沙箱清理。官方 Harbor 通过 `harbor==0.22.0` 安装；宿主 Hermes 与 AGS 模板仍需单独配置。无关的交易示例、smoke 任务生成及示例编排已移除；TraceForge 正式入口不依赖它们。

## 使用

在仓库根目录按[跨机器调试](../../docs/cross-machine-debug.md)准备 Python 3.12、宿主 Hermes 和私有配置，将 `--harbor-root` 指向本目录。运行环境的解释器和 Harbor 命令位于本目录 `.venv/bin/`，不复制其他机器的虚拟环境。

```bash
"$TRACEFORGE_PYTHON" -m traceforge reconstruct raw-run \
  --input return_data/four_batch/by-rubric/R04.jsonl \
  --line-number 1 --domain terminal --output artifacts/new-terminal-run \
  --config config.yaml --hermes-home .runtime/hermes-agent \
  --harbor-root integrations/harbor_ags \
  --execute-red --execute-rollout --rollout-trials 2 --manual-response-review
```

任务包、捕获与验收规则见[Harbor 交付](../../docs/harbor-task-delivery.md)。`harbor-ags` 保留 `preflight`、`validate-trial`、`audit-cleanup` 三个诊断命令；`preflight --probe-ags` 会实际调用外部服务。

## 版本与边界

`version-lock.json` 固定 Harbor、SDK、模板内 Hermes 提交与离线 wheel 哈希。离线 wheel 保留各自许可证。`configs/` 仅含环境变量占位；模型与 AGS 凭据由运行方提供。

AGS 模板 `node-python-hermes` 的构建定义和镜像摘要尚未交付，模板访问权限仍必需。当前捕获适配使用 Anthropic Messages，不能仅更换模型名就转为 Responses。现有 `.venv` 不随 Git 发布；干净安装及实际验证进度以[当前状态](../../docs/current-status.md)为准。
