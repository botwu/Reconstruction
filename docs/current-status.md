# 当前状态

更新：2026-09-30（北京时间）。阶段 READY、rollout 完成、文件通过、回答正确分别记录，不能互相替代。

## 当前交付

输入是完整原始 session 和调用方已知的 search / terminal，不做初始筛选、领域推断或脱敏。harness 的消息外壳由结构层适配，系统指令、工具协议、调用意图和返回含义由模型解读；原文和来源引用始终保留。

两种 domain 均可导出标准 Harbor 任务。terminal 交付文件初态、依赖、测试与参考解；search 交付任务、原始证据和所需检索能力。本地代码检索仍属于 search，不强制调用公网工具。见 [Harbor 交付](harbor-task-delivery.md) 和 [原始会话流程](raw-session-pipeline.md)。

本轮已修复 search 的来源交接与上下文边界，并真实重跑 R01:559。交接完整不等于回答正确；目前仍不能宣称所有原任务均已完整可解、回答全部通过。

- R01:559 原先通过模型白名单将 47 条已返回记录缩为 25 条。现在默认保留返回、显式说明排除；新环境、solver 输入和 Harbor 包均包含相同的 47 条，原正文、参数与消息来源逐项一致。
- 自由生成的 context_note 与 session_parse.reason 不再进入 solver。历史上下文按原消息及其所支持的用户请求恢复；完整方案可以成为后续输入，摘录须逐字匹配。
- R01:39 的真实模型边界实验恢复了前置用户要求和选题 1 的完整方案、数据条件与评价指标，未交付后面的投稿建议答案。其 2024/2025 CSTRO oral 来源问题仍未解决，不能以该上下文实验替代全流程通过。
- 新的持续 researcher 接收真实 rollout 的答案和工具轨迹，并区分作者来源索引与 solver 的实际读取。其首次复核仍漏报了回答错误；人工反馈后对源码事实作了更正，但仍返回 COMPLETE。该决定只作环境复核意见，不作为回答验收；真实输出保留 NEEDS_CORRECTION / NOT_ASSESSED。
- 原始 R01:559 还存在 6 次无返回读取、2 份只有预览的大输出。ImageTransforms / Normalize 部分正文不可取得，原仓库路径在 dev-wj 不存在。已询问对应源码快照；不编造未知真实实现。

| 样本 / 产物 | 真实结果 | 内容与边界 |
| --- | --- | --- |
| R01:559，本地代码检索 | handoff-run05 延续真实作者会话后交付 47 份捕获；Claude 31 次 API、48 次工具调用，直接读取 17 个事件，其中 7 个来自此前漏交材料；零公网调用 | 47 条原文无交接丢失；回答仍混淆独立 oracle、baseline 与生产 RB 顺序，并漏读已有目录列表。完整来源不代表全部被利用，内容保持 NEEDS_CORRECTION |
| R01:1，复杂并行包装 | 最终解析 24 个事件；两段正文为返回行 5–405、408–713，文件起点 220、300 | 排除打印空行和分隔线，保留原先漏掉的完整末行；真实边界复核通过 |
| R04:270，截断及异步轮询 | 最终解析 28 个事件；保留返回行 9–367、369–686，另一读取完整保留文件行 25–115 | 首段起点 380，后段起点 763 与另一原始编号读取有 21 行逐行一致；不再将空输入轮询解释成发送中断 |
| R01:39，会议研究 | search05 两次 rollout 完成；本轮复用初态重跑 intent 和 solver | 年份与 oral 来源已恢复到义务；新回答仍缺 2024/2025 CSTRO oral 依据，保持 NEEDS_CORRECTION |
| R04:1，task1/run09 模块提取与分析 | 两次真实 rollout 的 FILE 评分均为 1，原输入保留认证通过 | 两份分析已核查并提供更正说明；外部账户调用未经实测，文件评分不代表分析全部正确 |
| 同一 R04，task2/run14 超时修复 | 两次真实 rollout 完成，独立复核与正式评分一致 | 第一份通过；第二份遗漏网络超时重试而拒收，保留真实失败 |

R01:559 SHA256 为 `5f5e4c177393c129af89db66840a4d7148c08b7ff1dedba270e58342803439f9`；R01:39 为 `8c33a4c06f4973daec9dd9ac9185eae28e83c08fe0b7b152a5e15458a70db2f7`；R04:1 为 `2c9d609055058904e0162e0d5e3ab74116f2a6d4a765b08b3c6a8de4e3c67486`。

## 本轮根因修复

1. 旧解析只检查引用合法，允许遗漏或错误排除。v1.8 要求正文与排除段完整覆盖；截断排除须对应原文标记，已引用的连续编号正文不能部分丢弃。反馈明确区分模型抄错请求范围与原始返回损坏。
2. 旧 reason 混合动作和结果。现在分别解释 action / observation，保留内部执行关系，不从退出码倒推未经观察的动作。
3. search 曾无条件要求网页查询与读取，阻塞纯代码检索。现在根据原任务声明 requires_live_web；本地分支须按原要求列出支持材料并实际读取引用来源，保留历史版本、正文与坐标。
4. Intent 曾丢失年份/指定来源，也曾把自身“不得联网”的限制写入原任务。v16 约束义务限定与约束来源；真实 R01:559 重跑已去掉无原文依据的网络及测试禁令。
5. search 的白名单已删除；v3 交接按来源取回上下文并移除解析意见。旧 v2 包不能跳过新边界直接 resume。实跑还移除了两项过严限制：重复引用当前用户原文、排除已知 pending 调用都属于合法操作。
6. search 原本只在 rollout 前纠正访问错误，现在将真实执行反馈送回同一 researcher；真正的环境缺口才进入重建。输出错误保持人工核查，不新增自动回答评分；复核协议与补全协议分开。
7. R01:1 一次调用输入约 35 万 token，却耗尽原 65,536 输出预算。当前为 1M 上下文、128K 输出预算，截断作为调用错误保留，不能继续回放不完整 JSON。

最终两条解析各一次模型调用完成，输入分别为 349,850 / 206,422 tokens；未手改模型 JSON。针对性检查共 189 项通过，改动源码 F/E9 检查通过。这些检查不代替真实语义核查，也不证明所有 harness 已普遍无误。

## Harbor 与版本边界

已有 terminal 包通过原生 Harbor 加载、Docker 参考解执行及 Harbor/AGS 独立无网 verifier。search 网页包通过 Docker 真实查询与读取；本轮本地检索包由 Harbor 0.20 在 Docker 中启动成功。nop 只验证初态启动，没有答案或奖励；真实 Claude 检索 rollout 使用现有直接检索 runner。

R01:559 的 full-run01 使用本轮较早的 v1.7 解析快照。reviewed-run02 只更新 intent 与 solver；handoff-run03/04/05 则复用同一解析结果重跑交接。run05 从 run04 的持久化作者会话继续，solver 使用独立新会话；不得改标为 v1.8 全链路或无恢复提示的独立作者试验。v1.8 两条解析保存在 source-provenance-parser。跨机任务归档需保留文件权限；运行凭据不进入任务包或 Git。

## 剩余内容项

历年 CSTRO oral 依据尚未补齐。生产工具对已找到的日程入口发生空检索及网页 HTTP 500；公开索引仍有部分日程片段，不能推断资料不存在。solver 用相邻会议趋势作建议，仍不满足指定来源要求。这包含回答提前结束和来源读取失败，不靠重建环境反复循环来伪造通过。

旧 terminal 分析已更正“部分工作区等于完整安装环境”、原编码、外部网络行为及超出测试覆盖的断言。旧回答、正式评分和轨迹不回写；自由文本继续人工核查，不新增筛选或自动评分模型。

本地代码检索返修仍有将零起始显示行号照抄为文件物理行号的问题。人工报告按捕获正文重新核对坐标，并直接执行已读过的映射纯函数，确认两个边缘输入下基线报错而生产可返回；这只是函数级补充证据，不是完整仓库测试或新 rollout。

## 证据位置

本轮统一在项目 `artifacts/pipeline-debug-20260930/root-cause-fix/`：

- `R01-L1/source-provenance-parser/`、`R04-L270/source-provenance-parser/`：完整请求、原响应、解析和回执。
- `boundary-semantic-review.json`：真实边界前后对比及 R04 坐标交叉复核。
- `R01-L559/full-run01/`：正式入口的任务、补全、Harbor 包及首次 rollout。
- `R01-L559/reviewed-run02/`：意图修正、人工反馈及 solver 返修。
- `R01-L559/handoff-run05/`：47 条交接包、真实 rollout、handoff-audit.json、content-review.json 和 debug-review.zh-CN.md；review-final 保留人工反馈后的原会话复核，不回写首次误判。
- `search-history-boundary/`：真实 R01:39 历史方案选择、逐字恢复及后续答案隔离回执。
- `search-obligations/`：指定来源要求的 intent 重跑、回答及实际网页失败。
- `search-answer-corrections.zh-CN.md`、`terminal-analysis-corrections.zh-CN.md`：旧回答的更正与边界。
- `local-search559-reviewed-report.zh-CN.md`、`local-search559-function-check.json`：本地检索的人工更正版与源码函数反例。
- `reconstruction-boundary-audit.json`：原始事件到实际交付范围的核对、历史/答案边界及目前未完成的充分性检查。

此前五条 harness 审核及四份内容核查在上一级 `harness-review-20260930.md/json`、`content-review-20260930.md/json`。原始数据已从上游完整复制并逐文件核对 SHA256；原数据自带的占位符不会因复制恢复。历史代码固定、清理及 terminal 回执保留在 `artifacts/pipeline-debug-20260929/default-integration/`。
