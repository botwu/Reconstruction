# 模型网关配置

重建与 AgentRx 的模型调用支持两种后端：默认的 `TOKENHUB_KEY` 环境变量加 Claude Messages API，以及 `config.yaml` 中的 NewAPI channel。配置文件只在进程内读取，部署密钥不会写入调用回执、响应对象、错误诊断或其他 artifact；`config.yaml` 已被 gitignore，不能提交到仓库。

Gemini 测试示例：

```bash
PYTHONPATH=src .venv/bin/python -m traceforge failure-analysis agentrx \
  --trajectory-json trajectory.json \
  --output agentrx.json \
  --config /mnt/afs_toolcall/wujian1/Projects/workspace/TraceRconstruction/config.yaml \
  --channel gemini \
  --model-name gemini-2.5-pro
```

`model-judge` 和 `reconstruct raw-run` 同样支持 `--config` 与 `--channel`。当配置了 NewAPI channel 却仍使用默认的 Claude 模型名时，CLI 会自动选择该 channel 的安全默认模型（Gemini 为 `gemini-2.5-pro`，GPT 为 `gpt-5`）；需要其他模型时显式传入 `--model-name`。

配置格式是顶层 channel 名和一行 JSON 连接对象，例如：

```yaml
gemini:
  {"_type":"newapi_channel_conn","key":"<secret>","url":"https://tokenhub.sensetime.com"}
```

如果网关返回 HTTP 503，应先检查 channel 路由、模型名和服务状态；客户端保留 HTTP 状态及服务返回的 error.code/message/param，限制诊断长度并移除本次部署密钥；不复制整段响应正文。真实调试中的模型名错误、输出预算超限因此能直接定位。

## 模型 ID 与 Harbor provider 的边界

`roles.<role>.model` 是请求实际发给网关的完整模型 ID，不是 Harbor 的
`provider/model`。原样保留网关路由后缀；转接 Harbor 时由运行器另外加 provider。
显式 `--rollout-model` 则已是 Harbor ID，不再加一层。

本项目 Claude 配置的对应关系为：

| 配置位置 | 值 |
| --- | --- |
| roles.rollout.channel | `claude` |
| roles.rollout.model / 实际请求 model | `claude-opus-4-8/awsb_L/sfa` |
| Harbor plan / 显式 --rollout-model | `anthropic/claude-opus-4-8/awsb_L/sfa` |

2026-10-02 原生 run05 精确保留错误配置的 `anthropic/claude-opus-4-8/awsb_L/sfa`
作为网关 ID，实际返回 HTTP 503 `No available channel`；旧 run04 曾在 Hermes
额外移除前缀后成功。修复应更正角色配置，不能依赖客户端静默改写模型别名。
原生适配器保持最终请求的 model 字面值，且使用非流式响应；配置正确仍需真实请求验证。
若其他网关的合法 ID 自身含 `anthropic/`，仍须完整保留，不能套用全局去前缀规则。

## Astra 的实际接入

2026-10-01 的 TokenHub 实测可用 ID 为 `gpt-6-astra/azure/sfa`，响应报告
`gpt-6-astra-2026-09-03`。简称 `gpt6-astra` 没有可用路由。仅更换模型名不足以完成接入：
带工具和推理参数的 Agent 调用需要 Responses 协议；现有 Hermes 已有该协议，无需另写工具循环。

```yaml
gpt:
  {"_type":"newapi_channel_conn","key":"<secret>","url":"https://tokenhub.sensetime.com","agent_api_mode":"codex_responses","agent_api_max_retries":8}
roles:
  {"session_parser":{"channel":"gpt","model":"gpt-6-astra/azure/sfa"},"reconstruction":{"channel":"gpt","model":"gpt-6-astra/azure/sfa"},"verifier":{"channel":"gpt","model":"gpt-6-astra/azure/sfa"}}
```

`agent_api_mode` 只指定 Hermes Agent 的协议；不带工具的 session_parser 继续使用
NewAPI JSON 请求。未配置时沿用既有通道协议。服务实测接受的最大输出预算是
128000，131072 会被拒绝；输入上下文上限是另一项能力，不能从输出预算推导。
已真实处理约 6.46 万、18.99 万及 25.69 万输入 token 的完整 session，尚未验证 Astra 的 1M 输入。

Hermes Agent 可在通道连接对象中增加 `"agent_context_length":1000000`，显式声明
该通道的输入窗口（正整数）；不配时继续采用 Hermes 检测结果。它复用原生压缩器更新窗口、
压缩阈值与摘要预算，不修改压缩比例。默认比例为 50% 时，1M 配置对应 50 万 token
的预压缩阈值，避免网关别名被识别为较小窗口而提前摘要完整轨迹。
每轮 `agent_trace.json` 记录 `agent_context_length`、`resolved_context_length`、
`compression_threshold_tokens`、`compression_threshold_percent` 和 `compression_count`。
运行时逐值核对 terminal 行或 search JSON 中的完整 `SOURCE_SESSION`，并用 Hermes
原生 `protect_first_n` 保护包含它的历史前缀；其后的旧工具记录仍可压缩。
`source_history_protection` 记录输入位置、保护范围和返回会话中的原文位置；缺少原文或
原始上下文时明确标记，不以磁盘仍有原文代替当前模型持有原文。
`native_usage` 只保存 Hermes 实际返回的累计和末次主请求字段，不包含辅助摘要用量，
也不以粗估 token 数充当上游 usage。受保护前缀过大时，一次压缩未必降至阈值。
此配置只控制本地窗口判断，不扩充上游能力；1M 输入仍须真实请求验证。
独立的 session_parser JSON 请求不使用 Hermes 压缩器，也不受该字段控制。

HTTP 400 等运行失败记录为 `SESSION_TASK_AGENT_FAILED`，保留实际错误；
只有 Agent 正常返回但任务覆盖/引用不合约时才记录 `SESSION_TASK_REVIEW`。
协议探针成功不等于重建或 rollout 验收成功，具体实跑见[当前状态](current-status.md)。

## Session 解析

`reconstruct raw-run` 在 Replay 前调用独立的 `session_parser`。
默认 channel 为 `deepseek`，模型为 `bailian/deepseek-v4-flash-0731`；使用配置文件的
`deepseek` 连接，或在 `roles` 中覆盖 `session_parser.channel/model`。
该角色解读完整原始 session 的系统指令和工具语义，不执行历史命令。任务分组仍由 Session
Agent 负责，环境补全与验收沿用各自角色。

`raw-run --domain search|terminal` 直接使用数据已有的领域；R01 指定 search，R04 指定
terminal。解析请求携带既定 `domain_route`，当前输出契约不要求模型分类，模型也不能覆盖路由。

解析产物位于 `session_parser/`。默认 DeepSeek 使用模型提供的 1M 上下文能力，
每次送入完整原始 session，不做摘要或分组替代，也不按字段名删去原数据中的
`reasoning` / `reasoning_content`。原始数据中的这些字段是历史参考，随 source 原样保留。
请求中的每条消息增加明确的原始索引，消息本身保持原样；事件说明只解释该次调用和返回，
调用提交、结果显示和执行完成的顺序分开表述，不生成额外的全局诊断。
`ordering` 描述单个事件内部的执行关系：并发批为 `parallel`，单操作或明确逐项等待为
`sequential`，不能确定为 `unknown`；外层请求顺序和数组输出顺序不证明子调用串行执行。
同一次解析还输出 `system_context`：覆盖原始 system/developer 消息，每条包含
`message_indices` 和 `interpretation`，解读工具协议、环境与权限、并发和子 agent 约定、
任务及输出约束。重复模板可合并引用，变化按原消息时间解释。声明的能力或权限不能证明
实际行为；解读保留“必须/仅限/禁止”等约束强度，并区分原文事实与上下文推测。
后续只读声明也不能反推未返回补丁失败。原文完整保留，解读不替代原文。
本地只检查解读非空、引用属于原 system/developer 消息且无遗漏；这不保证语义推断正确。
Intent 和 Completion 的提示直接携带这份解读，并可按索引读取原文；历史指令不改变
当前角色权限，通用模板不增加用户目标或待补全依赖。
两者的提示还直接携带 `SOURCE_SYSTEM_MESSAGES` 全量原文，保留原索引、角色、重复项及所有字段，
以原文为准。解读可能遗漏或弱化约束，不能成为唯一信息入口；历史摘要和用户偏好也保留为参考。
调用与返回的消息编号直接沿用时间线中的结构化配对，不让模型在说明文字中重复抄写。
同一待返回调用按 ID 配对一次；后续无法匹配的返回单独保留，不覆盖早先结果，也不按 ID 相似度猜配对。
`max_tokens=128000` 是输出预算，和输入窗口不同；超出服务实际限制会明确失败，不静默截断。
待返回调用的参数保留用于理解意图，但不能生成初始文件；没有返回只表示执行结果未知。

解析阶段只接入本地输出契约和原始引用校验：模型返回结构不合法、引用不存在或越界时，
将具体错误连同完整原始输入反馈给该模型修正。模型输出即使是合法 JSON，只要服务标记为
长度截断，也不采用。结构修正不设固定轮数；相同无效输出重复，或同一错误累计三次时
报告无进展。传输错误独立记录，不进入语义修正循环。

每次调用保存到新的 `parser/attempt-NNNN/`：请求、原始响应、校验错误、请求哈希和回执。
根目录 `receipt.json` 指明状态和采用的轮次。`READY` 仅表示解析结构和来源引用可用，
不表示环境、任务或 rollout 通过。工具行为不确定时保留 `unknown`，无返回保留 `pending`，
缺失证据在对应事件中说明，不改写原文。
解析链不增加质检模型、质量评分或 session 拒收环节；结构错误由原解析模型直接修正。

文件正文仍由程序按模型引用从原始工具返回取回，不能由模型重新抄写；失败不退回旧命令规则。
模型输出使用 `file_text` 明确表示文件原文，读取 XLSX 表头等派生观察保留在原始返回中，
不进入文件操作列表；适配给 Replay 时才转换为其已有的 `read` 操作。
请求全文、执行成功且无截断迹象的文件返回可标为完整；范围读取保持部分，不以输出上限代替实际行数。
带行号返回显式声明 `line_number_base=0|1`，文件位置仍统一按 1 起始；零起始 Read 不丢第0行。
目录外或坐标未知的读取保留 `path=null`、原始 `source_path` 和精确正文引用，物化为
`reference_file_ops` 供参考；仅工作区内的 `file_ops` 进入 Replay，不能扩大工作目录或丢弃外部证据。
搜索观察不会转成伪文件，也不表示已有搜索环境执行后端。底层 API 不传 `parser_model`
只用于单独调试后续模块，会明确记录 `session_parser.status=NOT_RUN`。

Completion 的初次补全和修复轮均可通过 `read_session_message` / `read_session_context`
读取上游保留的完整原会话；初始提示提供原消息索引，长内容可分页续读，不以工具证据摘要替代原文。
用户前后文、助手说明、未返回调用和修改后观察都是理解项目的参考。
这些参考不自动成为任务初态：直接恢复与依赖自动覆盖只使用合格初态观察；
MODEL_COMPLETED 可如实引用完整交付、唯一可定位的原始工具证据，按路径说明推断及时序依据，
再由独立 Sufficiency 检查。未返回补丁不作为已执行改动，已知解题后状态不能预装到初态。

参考来源及未采用的策略见 [参考管线接入说明](reference-pipeline-adoption.md)。

通道可用 `agent_api_max_retries` 设置 Hermes 每次模型请求的总尝试数（含首次，正整数）；不配时保留其原生配置。Astra 大上下文实跑曾在默认3次、约2秒起始退避中耗尽预算，本轮设为8，复用原生 Retry-After 与指数退避。它不增加重建或solver轮数，也不重放已完成的工具；仍失败时保留服务错误。该设置不能保证上游配额或批量吞吐。

上游限流和服务错误须与模型语义输出分开：Hermes 的真实失败字段会保留具体原因，token 限流记为 `MODEL_RATE_LIMIT`。search 复核因此停止时保留已有环境和回答，不追加虚假的格式诊断；不要同时启动大量共享同一通道的大上下文任务。
