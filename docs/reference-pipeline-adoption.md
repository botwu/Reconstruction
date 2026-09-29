# Session 解析采用的参考设计

原始 session 与调用方已知的 domain 直接进入模型解析，不进行初始筛选或领域分类。
解析链只处理工具语义和原文对应关系，不增加独立质检、质量评分或拒收环节。

| 来源 | 固定版本 | 参考文件 | 采用方式 |
| --- | --- | --- | --- |
| `git@gitlab.sh.sensetime.com:agent_data/datafilter_v2.git` | `d5bb0cb4e34867660184985ae013d1f4c24b249d` | `src/datafilter/rounds/common/llm_json.py` | JSON 或来源引用无效时，将具体错误反馈给同一个解析模型；保留每次输入和输出 |
| 同上 | 同上 | `src/datafilter/content_integrity.py`、`README.md` | 原始记录不变，派生结果保留来源与版本 |

正式实现集中在 `session_parser` 与 `model_json.complete_checked_json`。模型使用完整
可观察 session 和 1M 上下文；消息有明确索引，正文仍按引用从原始工具返回提取。
原文中的缺失、乱码和无法确定的归属记录为未知，不由解析器猜测修复。

也参考过 `git@gitlab.sh.sensetime.com:agent_data/LLMChecker.git` 的
`28334d87a5816c14b385c43f9645afe7d2d0bcc3` 版本。其独立质检不属于当前需求，
相关实验仅保留为历史产物，不接入解析链。

两个参考仓库的已跟踪文件中未找到 LICENSE、COPYING 或 NOTICE。本项目只参考思路，
没有复制源码、引入私有依赖或迁移调度框架；参考仓库及其已有未提交修改均保持原样。

生产文件提取契约为 `session-interpretation.v1.4`，只输出工作目录与逐个工具事件的解析，
`file_text` 只表示文件文本原文，不生成全局诊断。
通用调用、意图和时间的 v2 契约
仍是实验结构，不能将本次调整描述为 v2 已接入或 search、terminal 全流程已跑通。
