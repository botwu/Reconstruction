# Terminal 活跑配置与预检

这份文档只描述如何检查执行条件，不把预检结果当作 terminal 任务端到端成功。配置文件包含模型凭据，放在部署机上，不能提交到 Git，也不要把完整配置打印到日志。

当前原始 session 的入口使用 `reconstruct raw-run`，不先运行 screening 或 inventory。
任务分组属于重建过程；domain 由调用方通过 `--domain search|terminal` 指定，模型不分类。
R01 使用 search，R04 使用 terminal。会话未结束、工具返回缺失、内容损坏不作为初始淘汰条件。
问题记入对应阶段的证据缺口，重建环境和任务是否合格由实际产物及执行结果判断。

## 角色配置

通道连接及 `roles` 均支持标准 YAML 或 JSON，显式配置的空值和错误类型会报错，
不会静默退回默认模型。下面是当前真实调试使用的角色示例，网关连接与凭据配置见
[模型通道](model-gateway-config.md)。在 `gpt` 通道连接对象中声明
`agent_api_mode: codex_responses` 和 `agent_context_length: 1000000`。

```yaml
roles:
  session_parser:
    channel: gpt
    model: gpt-6-astra/azure/sfa
  reconstruction:
    channel: gpt
    model: gpt-6-astra/azure/sfa
  verifier:
    channel: gpt
    model: gpt-6-astra/azure/sfa
  rollout:
    channel: claude
    model: claude-opus-4-8/awsb_L/sfa
    timeout_seconds: 14400
    max_iterations: 500
```

session_parser 解释工具语义与返回引用；reconstruction 承担 Intent、Completion、
Sufficiency 及编排；verifier 生成和校准文件行为测试；rollout 在 Harbor 中独立解题。
重建与 rollout 使用不同模型。上述 1M 是显式窗口配置，14400 秒与 500 次是当前
单次 solver 预算，均不等于已验证对应容量，也不限制整个重建工作的修正次数。

未覆盖的角色仍使用源码默认配置（解析为 DeepSeek，重建和验证为 gpt-5），
不能把默认值与本轮实际 Astra 调用混为一谈。部署时应明确配置全部四个角色。

roles 中的 model 是网关的完整模型 ID，斜杠和路由后缀原样保留。
转接 Harbor 时另外加一层 provider；例如上面的 rollout 在 Harbor 计划中为
`anthropic/claude-opus-4-8/awsb_L/sfa`，实际发给网关的是
`claude-opus-4-8/awsb_L/sfa`。CLI 显式 `--rollout-model` 已接受 Harbor 的
`provider/model`，传该计划值即可，不再自动加前缀。不要把 Harbor 计划值填入 roles.model。
只有网关本身的完整 ID 确实以 provider 名开头时，外层计划才会出现重复前缀；
适配器不会猜测或删除合法网关别名。

实际报告中的 `roles` 只包含 role/channel/model，不包含 key。CLI 的 `--channel`、`--model-name`、`--verifier-channel`、`--verifier-model`、`--rollout-channel`、`--rollout-model` 只做显式覆盖，覆盖值不会写入配置。

## 本地静态预检

```bash
PYTHONPATH=src python scripts/preflight_terminal_environment.py \
  --config config.yaml \
  --harbor-root /mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags \
  --hermes-home /mnt/afs_toolcall/wujian1/Projects/tokenhub_data_model_eval/R01/hermes-agent \
  --output /tmp/traceforge-terminal-preflight.json
```

默认报告的 `scope=LOCAL_STATIC_ONLY`，只检查：

- 模型角色配置能否解析；
- channel、Hermes 根目录、Harbor 配置和可执行入口是否存在；
- 本机 pytest 是否可用；
- 仓库锁定的离线 pytest wheel 是否完整、哈希是否匹配。

`status=STATIC_PASS` 只说明这些本地条件满足。报告会明确列出 `unchecked_probes`，包括模型连通性、AGS 创建、Harbor RED 和 Hermes rollout；`end_to_end_verified` 始终为 false。

## 可选 AGS pytest 冒烟

需要明确验证真实 AGS 创建、离线 pytest 上传、PASS/FAIL 分类、输入不变和清理时，另给一个不存在的输出目录：

```bash
PYTHONPATH=src python scripts/preflight_terminal_environment.py \
  --config config.yaml \
  --harbor-root /mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags \
  --hermes-home /mnt/afs_toolcall/wujian1/Projects/tokenhub_data_model_eval/R01/hermes-agent \
  --sandbox-output /tmp/traceforge-terminal-preflight-smoke-<run-id> \
  --output /tmp/traceforge-terminal-preflight-smoke-<run-id>.json
```

这个选项会创建一个 verifier 角色沙盒，但不调用模型，也不执行任务 rollout。只有 `status=SANDBOX_SMOKE_PASS` 才说明 AGS pytest 冒烟通过；这仍不是 RED 校准，也不是端到端成功。缺凭据、网络或 Harbor 环境时会返回 `REVIEW`，不能把失败改写成 READY。

## 真实 terminal 主链

```bash
export HERMES_HOME=/mnt/afs_toolcall/wujian1/Projects/tokenhub_data_model_eval/R01/hermes-agent
PYTHONPATH=src python -m traceforge reconstruct raw-run \
  --input <完整 R04 或 R05 JSONL> \
  --domain terminal \
  --line-number <原始行号> \
  --output /tmp/traceforge-terminal-e2e-<run-id> \
  --config config.yaml \
  --hermes-home "$HERMES_HOME" \
  --sandbox --execute-red --execute-rollout \
  --rollout-trials 2
```

阶段顺序是原始 session → 任务分组 → 模型解析与引用校验 → Replay/domain 路由 → Intent → Completion → Sufficiency → Verifier → Harbor RED → rollout。只有 manifest 为 `READY`、Verifier 具有真实 RED 证据、两次 rollout 通过质量门禁，外部 `scripts/check_reconstruct_e2e.py` 才可能给出完整通过。预检、离线测试或单个角色 `READY` 都不替代这条证据链。

Completion 证据、Agent 工具参数与轨迹、候选清单和验证诊断不再按 `token`、`key` 等字段名或正文正则替换原值。诊断摘要原有的长度上限保留。原始 JSONL 不改写；源文件已有的脱敏标记不能靠取消当前管线的脱敏恢复，必须使用未改写的来源或标为证据缺口。

search 必须进入独立的检索重建策略。search 的网页检索后端独立执行；不能因终端文件分支拒绝 retrieval，就把 search 改成 terminal 来绕过。

实际输入、结果与执行边界见 [当前状态](current-status.md)。
