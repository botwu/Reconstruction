# Search 实跑

`reconstruct raw-run --domain search` 使用调用方给定的领域。任务切分、完整 session 解析和 Intent 与 terminal 共用；随后进入检索环境补全，不进入文件回放、源码补全、pytest 或 Harbor RED。

检索环境由任务、原始工具返回、来源索引以及公开查询/页面读取工具组成。补全 agent 可读取完整 session 和系统原文，选择相关历史与检索证据；程序按索引复制原始返回，不让模型重写捕获内容。solver 只能按需读取这些证据和公开来源，不能读取完整原 session 或原任务最终答案。

实时工具记录查询、URL、抓取时间、页面原始字节摘要与分页快照。旧搜索片段仍标为捕获证据，不能冒充网页全文；新抓取的网页也不声称与历史时点等价。

公开查询使用 Serper，来源读取默认使用 Jina Reader，仅依赖 Python 标准库。凭据从环境变量 `SERPER_API_KEY`、`JINA_API_KEY` 或私有 `~/.config/traceforge/search.json` 的同义小写字段加载。可用 `TRACEFORGE_SEARCH_CONFIG` 指定私有配置位置；网络无法访问 Jina 时，可明确设置 `fetch_provider: "serper"`（或环境变量 `TRACEFORGE_FETCH_PROVIDER`）使用同一 Serper 账户的网页读取接口。实际 provider 会记录在每条返回中，不静默切换。

凭据不进入任务、请求正文或产物。网络失败、登录页或缺失来源不能假称已取得完整论文；需按真实工具返回判断并保留限制。

产物位于 `tasks/<task_id>/environment.json` 和 `rollouts/trial-*/`。环境 `READY` 要求补全完成且实际查询、页面读取成功；`ROLLOUT_COMPLETED` 仅表示真实 solver 完成并留下回答与调用记录，不代表事实与引用已验收，也不会自动写成 SFT 合格。

重建角色的工具调用在 `private/tool_events.jsonl` 逐次落盘。`STARTED` 后没有 `FINISHED` 表示该工具尚未返回；相邻工具之间的空档可能是模型请求，不能只凭运行时长推断死循环。
