# Terminal 活跑配置与预检

这份文档只描述如何检查执行条件，不把预检结果当作 terminal 任务端到端成功。配置文件包含模型凭据，放在部署机上，不能提交到 Git，也不要把完整配置打印到日志。

当前原始 session 的入口使用 `reconstruct raw-run`，不先运行 screening 或 inventory。
任务分组属于重建过程；domain 由调用方通过 `--domain search|terminal` 指定，模型不分类。
R01 使用 search，R04 使用 terminal。会话未结束、工具返回缺失、内容损坏不作为初始淘汰条件。
问题记入对应阶段的证据缺口，重建环境和任务是否合格由实际产物及执行结果判断。
旧的 `reconstruct run --records` 仅用于已有筛选记录，不是原始数据入口的必经步骤。

## 角色 JSON

在本地 `config.yaml` 的 `roles` 项中放一个 JSON 对象。每个角色至少指定 `channel` 和 `model`；模型网关仍从对应 channel 读取凭据。

```yaml
roles:
  {"screening":{"channel":"deepseek","model":"bailian/deepseek-v4-flash-0731","max_input_chars":500000,"max_messages_for_triage":260,"max_source_requests_for_triage":20},"reconstruction":{"channel":"gpt","model":"gpt-5"},"verifier":{"channel":"gpt","model":"gpt-5"},"rollout":{"channel":"claude","model":"anthropic/claude-opus-4-8/awsb_L/sfa"}}
```

这组值表示当前 terminal 运行矩阵：

| 角色 | channel | model | 作用 |
| --- | --- | --- | --- |
| screening | deepseek | `bailian/deepseek-v4-flash-0731` | 旧筛选入口使用；`raw-run` 不调用 |
| session_parser | deepseek | `bailian/deepseek-v4-flash-0731` | Replay 前理解工具语义和返回引用；此角色有默认值，可在 roles 中覆盖 |
| reconstruction | gpt | `gpt-5` | Intent、Completion、Sufficiency 和编排代理 |
| verifier | gpt | `gpt-5` | 生成隐藏 pytest 和 RED 证据 |
| rollout | claude | `anthropic/claude-opus-4-8/awsb_L/sfa` | Harbor 解题复验；必须与 reconstruction 模型不同 |

实际报告中的 `roles` 只包含 role/channel/model，不包含 key。CLI 的 `--channel`、`--model-name`、`--verifier-channel`、`--verifier-model`、`--rollout-channel`、`--rollout-model` 只做显式覆盖，覆盖值不会写入配置。

## 候选 inventory

对完整上游 R04/R05 做只读盘点：

```bash
PYTHONPATH=src python scripts/inventory_terminal_candidates.py \
  --input <screened-records.jsonl> \
  --manifest <screened-records.jsonl.manifest.json> \
  --output /tmp/traceforge-terminal-inventory.json
```

该脚本只识别 terminal 工具、用户动作、读取证据和本地风险，输出候选数量、路径 token、来源行号和 manifest 哈希匹配；它不回放命令、不调用模型、不改变 JSONL。输出放到 `/tmp` 或被 Git 忽略的目录。inventory 的 `eligible` 只代表适合进入筛选/重建，不代表环境足够，也不代表 Verifier 或 rollout 已通过。

## 本地静态预检

```bash
PYTHONPATH=src python scripts/preflight_terminal_environment.py \
  --config config.yaml \
  --harbor-root /mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags \
  --hermes-home /mnt/afs_toolcall/wujian1/Projects/tokenhub_data_model_eval/R01/hermes-agent \
  --output /tmp/traceforge-terminal-preflight.json
```

默认报告的 `scope=LOCAL_STATIC_ONLY`，只检查：

- 模型角色配置和 screening 预算能否解析；
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

search 必须进入独立的检索重建策略。当前 `RETRIEVAL_UNSUPPORTED` 表示后端未实现，不表示原始样本质量不合格，也不能把它改成 terminal 文件任务来绕过。

2026-09-21 的真实 R04 line 262 已完成一次可复现的 Verifier RED 校准：v13 的 NOP 中缺失能力测试全部失败、protective 测试通过，两个 oracle 进程退出码为 0，mutation 语义失败，报告为 `status=READY`、`calibration=PASS`。但当前候选测试主要是源码文本/正则断言，不足以证明 ROS 订阅、持续发布或参数写入行为；`obl-001` 和 `obl-003` 仍是 `NON_FILE`，因此 `unverified_obligations` 不为空，不能进入 SFT 或关闭认证。

同一冻结 Bundle 的 Hermes 复验已经用于验证执行边界。v1 暴露 AGS 默认 120 秒传输超时；现在 Harbor 配置会把 sandbox、request 和 transfer timeout 一起绑定到 900 秒。v2–v5 的失败来自实验脚本把 Claude 模型错误地配到了 `gpt` channel，不能归因于 TokenHub 不可用。修正为 `channel=claude` 后，v6 已成功启动真实沙盒、CaptureProxy 和 Hermes，并产生约 10 MB 的 exchanges/trajectory 证据；但 `max_iterations=30` 用尽，agent 在最后一个 `tasks.yaml` 数值修改前结束，故仍未通过任务质量门禁。v7 正在用 `max_iterations=60` 重跑，结果需要同时满足两次 trial、真实 reward 和 evidence reconciliation；在此之前保持 `sft_eligible=false`、`certification_closed=false`。


## CaptureProxy 与直连 TokenHub

CaptureProxy 不负责提供模型网络能力；沙盒可以在正确的 channel 和凭据下直连 TokenHub。它负责把每一次 Anthropic 请求、响应和流式 SSE 事件记录成受控证据，并让 Hermes 的 session、工具调用、workspace 快照与模型边界按 exchange id 对账。

当前 AGS validator 要求 `anthropic-exchanges.jsonl`、`anthropic-sse.jsonl` 和 `trajectory.full.json` 等产物。移除 CaptureProxy 后，Hermes 仍可能留下 session、终端输出和 workspace 文件，但当前 evidence builder 会因缺少 exchanges 进入 `INFRA_CAPTURE`，不能生成可验收的完整轨迹。若将来支持直连模式，必须在 Hermes SDK/HTTP 层实现等价 recorder，至少写出相同 schema 的 request/response 原文哈希、HTTP 状态、stream 完整标记、错误和时间戳，然后复用现有 evidence/validator；只保存 session 不足以替代 CaptureProxy。

## 本次预检证据

2026-09-21 在部署机使用真实 AGS 执行了 verifier pytest 冒烟（产物在 `/tmp/traceforge-terminal-preflight-smoke-20260921-1`，未入库）：

- `status=SANDBOX_SMOKE_PASS`，工作区输入摘要前后一致；
- 一个固定测试得到 `PASS`，一个固定失败测试得到 `FAIL/PYTEST_EXIT_1`；
- 离线 pytest vendor 上传成功，沙盒清理已确认；
- 该冒烟没有模型调用，且 `end_to_end_verified=false`；模型连通性、Harbor RED 和 rollout 仍未检查。

因此这条证据只关闭“当前 AGS verifier 能执行 pytest”的基础设施疑问，不能替代真实 terminal 候选的 Verifier、RED 和 rollout 产物。
