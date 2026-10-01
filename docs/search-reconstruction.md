# Search 实跑

`reconstruct raw-run --domain search` 使用调用方给定的领域。任务切分、完整 session 解析和 Intent 与 terminal 共用；随后进入检索环境补全，不进入 terminal 的文件初态回放、可执行工作区补全、pytest 或 Harbor RED。

检索环境由任务、必要历史、原始工具返回、真实补充资料、来源索引及检索工具组成。补全 agent 可读取完整 session 和系统原文；已返回记录默认全部交付，仅隔离有逐字依据的本任务答案与解题后状态。程序按索引复制原始返回，不让模型重写捕获内容。solver 按需读取交付材料，不能读取完整原 session 或原任务最终答案。

search 包含本地代码、文档检索。补全 agent 根据原任务声明 `requires_live_web`，决定 solver 是否需要继续访问公网；纯语料任务用 `search_evidence/read_evidence`，无需 Serper/Jina 配置。重建者仍可沿原轨迹的公开包名、版本或 URL 获取真实补充材料，核对后随语料交付；公开上游与私有版本的差异必须保留。该判断不改变调用方给定的 domain，也不以 shell、Read 或网页工具的名称划分领域。

实时工具记录查询、URL、抓取时间、页面原始字节摘要与分页快照。旧搜索片段仍标为捕获证据，不能冒充网页全文；新抓取的网页也不声称与历史时点等价。

公开查询使用 Serper，普通网页读取默认使用 Jina Reader。显式 PDF 链接下载原文件，用 `pypdf[fonts]` 读取文本层；这项依赖已纳入 `pyproject.toml` 和锁文件。凭据从环境变量 `SERPER_API_KEY`、`JINA_API_KEY` 或私有 `~/.config/traceforge/search.json` 的同义小写字段加载。可用 `TRACEFORGE_SEARCH_CONFIG` 指定私有配置位置；网络无法访问 Jina 时，可明确设置 `fetch_provider: "serper"`（或环境变量 `TRACEFORGE_FETCH_PROVIDER`）使用同一 Serper 账户的网页读取接口。实际 provider 会记录在每条返回中，不静默切换。

PDF 返回保留原文件 SHA256、物理页码、提取器版本、空文本页和提取范围。文本层可读不表示图片、图表、公式已经核实；扫描件或无文本层的文件明确返回缺口，不生成替代正文。Harbor 包固定与作者相同的 PDF 库版本，在镜像构建阶段安装。

凭据不进入任务、请求正文或产物。网络失败、登录页或缺失来源不能假称已取得完整论文；需按真实工具返回判断并保留限制。

产物位于 `tasks/<task_id>/environment.json` 和 `rollouts/trial-*/`。环境 `READY` 要求原始来源交接合法、每项任务要求有对应材料且作者实际读过；声明需要公网的任务还须有本轮成功查询和页面读取。来源编号、读取记录和模型覆盖说明仍不能自动证明语义充分。`ROLLOUT_COMPLETED` 仅表示真实 solver 完成并留下回答与调用记录，不代表事实与引用已验收，也不会自动写成 SFT 合格。

实际循环为：恢复任务与资料 → 检查材料交接与访问 → 导出 Harbor → 真实 solver → 同一 researcher 对照逐项要求、答案及工具记录复核。缺失输入或能力进入恢复后重跑；资料已交付但 solver 漏读或推断错误时保留解题错误，不能把答案写进环境。重复状态无进展时停止，无法取得的必要资料明确记录；完整停止规则见[研究者管线](researcher-pipeline.md)。

READY 初态同时导出 `harbor/<digest>/task/`，`result.json.harbor_task` 返回可交给下游的原生任务路径。该目录包含任务说明、捕获证据和可执行检索工具；使用原生 Harbor 时仍须 `--disable-verification`，内容留待核查。详见 [Harbor 任务交付](harbor-task-delivery.md)。

重建角色的工具调用在 `private/tool_events.jsonl` 逐次落盘。`STARTED` 后没有 `FINISHED` 表示该工具尚未返回；相邻工具之间的空档可能是模型请求，不能只凭运行时长推断死循环。
