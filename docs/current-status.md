# 当前状态

更新：2026-10-12。此页记录当前执行边界；历史调试过程保留在 Git 和原始运行目录。

## 目标与流程

最大限度利用完整原始 session，重建忠实、可解的任务及初始环境，交付标准 Harbor 包。环境可用、执行完成、答案正确分别记录，不能通过缩小目标或提前交付历史答案换取通过。

- search：完整轨迹 → 原意与交付范围 → 轨迹资料恢复和必要真实检索 → 材料覆盖检查 → Harbor → 两次真实求解 → 环境反馈与内容核查。包含网页、文档、代码检索，以及有相关检索支持的科学分析。
- terminal：完整轨迹 → 原意及初态/产物绑定 → 代码、数据、依赖及必要运行条件恢复 → 按原目标检查 → Harbor → 两次真实求解 → 环境反馈与内容核查。
- domain 由调用方提供。Codex、Hermes、Claude Code 等 harness 影响协议解释；timeline 用于溯源，不机械重放历史命令。重建者使用全部轨迹，求解者只获得必要初态和背景。
- 原有意图模型返回 search 的 delivery_scope，保留原任务与依据；范围不符或不能确定的任务不进入作者，不改域，不按难度或原会话成败丢数据。
- 独立确认的环境缺口返回补全，任务合同错误返回意图恢复，solver 答卷错误不改写初态。基础设施错误单独记录。

## 当前 200 条试产

上海时间 2026-10-12 18:22 的快照：固定批次控制器已启动，进程存活，正在执行前置校验；尚未形成首个 session 终态，不能宣称 200 条已完成。执行代码为 `9e0f2cdb5819503176eeb158bcdbdb613e9847fc`。已设置本任务每 15 分钟跟进实际状态，仅对有效进展、失败、修复或需要用户决定的问题通知；实时进度以私有回执为准。

输入固定为 search 100 条、terminal 100 条，共 200 条原始 session。选取不依赖内容质量；保留失败，不自动补抽。一个 session 可有多个独立任务，任务数不冒充 session 数。

全局串行，20 个块，每块 10 条，search/terminal 交替。单条失败留回执并继续；块缺少完整终态或绑定异常时暂停后续块。已记账失败不会由普通 --resume 自动重试，部分中断可能从该 session 的新 attempt 重做。

私有入口（相对仓库根）：

| 路径 | 内容 |
| --- | --- |
| artifacts/pilot-20261012/selection/selection-receipt.json | 固定 200 条来源、物理行及 SHA |
| artifacts/pilot-20261012/freeze-run01/manifest.json | 冻结提交、代码和配置绑定 |
| artifacts/pilot-20261012/control-run01/state.json | 当前块和已完成块 |
| artifacts/pilot-20261012/outputs-run01/ | 每块 session 终态、任务包和执行记录 |

批次同时记录 session 状态，以及任务的 COMPLETED、OUT_OF_SCOPE、NEEDS_REVIEW、RECONSTRUCTION_OR_EXECUTION_ISSUE。COMPLETED 仅代表所配置处理完成，不代表答案正确或已经可训练。未核实答案保持 NOT_ASSESSED / SFT=false。质量审查后再决定扩量和并发。

解析、重建、复核使用 gpt-6-astra/azure/sfa，配置上下文 1M；solver 使用 claude-opus-4-8/awsb_L/sfa，每 task 两次原生 rollout。配置窗口不等于服务端对任意输入的容量保证。

## 前一轮证据与仍需验证的边界

旧私有索引 artifacts/pilot-20261010/recovery/delivery-index-current.json 包含 11 个来源 session 的 21 项任务、42 份答卷，索引 SHA 为 310213b7b27fa2221db26f90cc552d878e510232c5fcc2ce4c78533798446889。它保留旧范围，不代表 21 项都符合本批 search 范围；19 个 search 条目按新边界离线核查为 14 个纳入、4 个不纳入、1 个待确认。原件和旧索引未改。

R01:1216 第 8 项已实际完成“作者修正资料定位 → 新双 rollout → 独立复核”，最终环境复核 COMPLETE、执行 ROLLOUT_COMPLETED；答卷仍存在求解错误，保持 NOT_ASSESSED。此次新结果尚未合入旧交付索引。

旧 terminal 九条批次已处理完 9/9 session、19 个任务，其中 1 项完成 rollout、18 项 REVIEW。已定位材料缺失、必要依赖缺口和管线判断错误，不能据此宣称 terminal 全部成功。

本批冻结包含历史文件读取/初态恢复资格一致性、可选遥测关闭、检索覆盖进展判断、范围交接和批次续跑修复。相关定向验证已通过，但不能替代这 200 条的真实结果。旧样本的私有工程、原图、API 实现缺口不会凭代码修复自动消失；没有明确替代边界时不补造后端或原件。

## 工作与发布边界

仅修改远端 TraceRconstruction；本地 AgenticFoundry 只读参考。原始 R01/R04 已按授权经 Git LFS 发布，私有配置、密钥、完整捕获和运行产物不入 Git。OCR、GUI、完整工程构建只在原任务需要时处理。

实现入口见[源码地图](rebuild-live-map.md)、[反馈流程](researcher-pipeline.md)、[Harbor 交付规范](harbor-task-delivery.md)与[批量运行](batch-reconstruction.md)。
