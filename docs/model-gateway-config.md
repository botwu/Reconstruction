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

`model-judge` 和 `reconstruct raw-run` 同样支持 `--config` 与 `--channel`。当配置了 NewAPI channel 却仍使用默认的 Claude 模型名时，CLI 会自动选择该 channel 的安全默认模型（Gemini 为 `gemini-2.5-pro`，GPT 为 `gpt-5`）；需要其他模型时显式传入 `--model-name`。

配置格式是顶层 channel 名和一行 JSON 连接对象，例如：

```yaml
gemini:
  {"_type":"newapi_channel_conn","key":"<secret>","url":"https://tokenhub.sensetime.com"}
```

如果网关返回 HTTP 503，应先检查 channel 路由、模型名和服务状态；客户端会保留错误码但不会把响应正文（可能包含敏感信息）写入 artifact。

`reconstruct raw-run` 在 Replay 前调用独立的 `session_parser`。
默认 channel 为 `deepseek`，模型为 `bailian/deepseek-v4-flash-0731`；使用配置文件的
`deepseek` 连接，或在 `roles` 中覆盖 `session_parser.channel/model`。
该角色解读完整原始 session 的系统指令和工具语义，不执行历史命令。任务分组仍由 Session
Agent 负责，环境补全与验收沿用各自角色。

`raw-run --domain search|terminal` 直接使用数据已有的领域；R01 指定 search，R04 指定
terminal。解析请求携带既定 `domain_route`，输出契约 v1.5 不要求模型分类，模型也不能覆盖路由。

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
`max_tokens=65536` 是输出预算，和输入窗口不同；超出服务实际限制会明确失败，不静默截断。
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
搜索观察不会转成伪文件，也不表示已有搜索环境执行后端。底层 API 不传 `parser_model`
只用于单独调试后续模块，会明确记录 `session_parser.status=NOT_RUN`。

Completion 的初次补全和修复轮均可通过 `read_session_message` / `read_session_context`
读取上游保留的完整原会话；初始提示提供原消息索引，长内容可分页续读，不以工具证据摘要替代原文。
用户前后文、助手说明、未返回调用和修改后观察都是理解项目的参考。
这些参考不自动成为任务初态：文件写入仍引用初态证据，未返回补丁不作为已执行改动。

参考来源及未采用的策略见 [参考管线接入说明](reference-pipeline-adoption.md)。
