# Terminal 活跑配置与预检

这份文档只描述如何检查执行条件，不把预检结果当作 terminal 任务端到端成功。配置文件包含模型凭据，放在部署机上，不能提交到 Git，也不要把完整配置打印到日志。

当前原始 session 的入口使用 `reconstruct raw-run`，不先运行 screening 或 inventory。
任务分组属于重建过程；domain 由调用方通过 `--domain search|terminal` 指定，模型不分类。
R01 使用 search，R04 使用 terminal。会话未结束、工具返回缺失、内容损坏不作为初始淘汰条件。
问题记入对应阶段的证据缺口，重建环境和任务是否合格由实际产物及执行结果判断。

## 角色 JSON

在本地 `config.yaml` 的 `roles` 项中放一个 JSON 对象。每个角色至少指定 `channel` 和 `model`；模型网关仍从对应 channel 读取凭据。

```yaml
roles:
  {"session_parser":{"channel":"deepseek","model":"bailian/deepseek-v4-flash-0731"},"reconstruction":{"channel":"gpt","model":"gpt-5"},"verifier":{"channel":"gpt","model":"gpt-5"},"rollout":{"channel":"claude","model":"anthropic/claude-opus-4-8/awsb_L/sfa"}}
```

这组值表示当前 terminal 运行矩阵：

| 角色 | channel | model | 作用 |
| --- | --- | --- | --- |
| session_parser | deepseek | `bailian/deepseek-v4-flash-0731` | Replay 前理解工具语义和返回引用；此角色有默认值，可在 roles 中覆盖 |
| reconstruction | gpt | `gpt-5` | Intent、Completion、Sufficiency 和编排代理 |
| verifier | gpt | `gpt-5` | 生成隐藏 pytest 和 RED 证据 |
| rollout | claude | `anthropic/claude-opus-4-8/awsb_L/sfa` | Harbor 解题复验；必须与 reconstruction 模型不同 |

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
