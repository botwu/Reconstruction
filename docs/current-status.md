# 当前状态

更新：2026-10-01（北京时间）。目标是从完整原始 session 恢复忠实、可解的任务与初始环境，再用真实 rollout 检验，最后稳定批量执行。阶段完成、文件评分与回答正确分别记录。

## 代码与数据

公开仓库为 https://github.com/botwu/Reconstruction 。R01 search 共 1,683 条、R04 terminal 共 6,535 条，完整原始数据已通过 Git LFS 发布，并按 data/debug-datasets.json 校验跨机下载与恢复。原数据已有的占位符保留，本项目不新增脱敏或初始筛选。

修改仅位于 dev-wj 的 TraceRconstruction。AgenticFoundry 是只读参考；本项目运行不依赖其目录。两种 domain 使用明确指定的不同策略，最终均导出 Harbor 任务。详见[跨机调试](cross-machine-debug.md)、[批量执行](batch-reconstruction.md)、[Harbor 交付](harbor-task-delivery.md)。

## 本轮真实运行

全量 8,218 条已完成字节、JSON 和逐行哈希盘点；不是全量模型重建。模型实跑仍使用已授权的固定样本。

| 样本 | 本轮实证 | 当前边界 |
| --- | --- | --- |
| R01:38，公开资料研究 | 正式批量入口完成解析、补全、Harbor 导出及一次 Claude rollout；另一次独立 rollout 复用同一环境 | 回答内容 NEEDS_CORRECTION。仍有占位来源及数值解释错误，不把执行完成算成内容合格 |
| R01:559，本地代码检索 | 47 条已返回记录完整交付。run07 agent 取回公开 Normalize（254 行）和 transforms（249 行）、校验 Git blob，导出 Harbor 并完成 Claude rollout | 人工内容核查 NEEDS_CORRECTION：把共享实现误称独立 oracle、零基显示编号误作文件行号；solver 未读 live:*。公开版本仍不能当作私有分支原文 |
| R04:1，terminal | 新分段恢复为两个任务；真实 DeepSeek v1.9 解析完成。第一项环境就绪，但六轮验证器语义检查均拒绝，未进入 RED/rollout | 验证器反复增加 AST 规则导致漏检和误拒绝。提示约束已修正但待真实验证；新运行等待明确外发授权，旧进程已停止并清理沙箱 |
| R04:1，独立候选检查 | AGS 真实加载 main.py 和已安装的 twitter-api-client 0.10.22；去重、状态保存与重读通过，候选不变、沙箱已清理 | 未替换本地模块；禁止网络连接。未验证外部账户操作或待实现的任务能力 |

R01:38 的内容核查区分了环境问题与 solver 错误：资料中已有原论文，但回答将不同编组方案各自最慢车辆的约 48 s / 122 s 再充风时间写成同一列车头尾差异；还混淆了开始制动与开始缓解的时间差。原始回答、轨迹和正式回执未回写。依据为[原论文第3.2、3.3节](https://pdf.hanspub.org/ojtt20220500000_70489163.pdf)。

R01:559 的新 rollout 已执行完成，researcher 返回 COMPLETE，正式 acceptance 仍为 NOT_ASSESSED。人工核查确认：去掉 v1_baseline 封装后直接调用共享底层仍不能检测底层的同向错误；源码引用还需将原 Read 工具的零基编号加一。已有输入足以发现这些错误，不应为此重复生成环境。

历史 R04:1 task1/run09 两次文件评分通过；task2/run14 两次真实 rollout 中一份通过、一份因漏掉网络超时重试而拒收。这些属于旧代码和旧阶段恢复路径，不代表本轮从原文开始已完整通过。

## 本轮定位并修复的问题

- 分段只看被截短的用户文本，容易将“基于已有产物的新修复”合并进上一任务。现在保留完整用户文本和前置助手状态，保留任务之间的 context 关系；R04:1 与 R01:38 已真实验证。
- JSON 工具外壳截断时，内部仍完整的源码行被整段丢弃。解析 v1.9 仅解码确实捕获且可严格解码的字符串前缀，保留原始起点与 partial 标记。R04:1 的四份观察共恢复 591 个重叠计数的观察行，逐字核对原文；不是 591 个不同初态行，不补尾部。
- search 把历史查询当本轮查询，或把已经内联的真实源码算作未读。现在记录实际查询、内联原文坐标与真实读取；解析意见和搜索预览不能冒充完整正文。说明可以是文本或文本数组，避免无意义的格式阻塞。
- 真实 fetch 暴露 Serper 把 GitHub 原始 Python 文件压成一行。文件 URL 现在经 GitHub 内容 API 解码，并校验大小、Git blob 与 SHA256；普通网页继续使用现有读取。R01:559 agent 已实际分页读取修复后的两份正文，未生成替代源码。terminal 补全也接通相同公开检索工具，写入仍须绑定原始证据。
- 环境修复只改缺口描述、不改候选也不重测，却继续循环。现在以候选、实际错误和成功探针判断进展；返修 READY 需要本轮执行依据。反馈删除重复快照，保留完整检查代码、stdout 和 stderr。
- 批处理固定要求 R04/R05、读取错误的 search 完成回执。现在由调用方明确数据源与 domain，保留失败、人工未验收和阶段产物，支持锁定、原子进度与续跑。续跑绑定代码和配置内容哈希，不能把同路径的新代码混入旧批次。坏回执单独记录，继续下一条。

真实批次已证明：并发重复启动被拒绝；完成后加 --resume 不再调用已完成样本，105 份既有文件哈希保持不变，原有 BLOCKED 和非零退出码保留。当前批量执行为单工作进程；小批次的语义问题未收敛前，不宣称可稳定生产全量合格产物。

## 本轮证据

原始运行证据不进入 Git。统一位于项目 artifacts/pipeline-debug-20261001/：

- batch-input-check：完整输入清单、冻结字节与发布数据哈希。
- boundary-run02、parser-v19-R04-L1：真实任务边界、解析回执、原始前缀逐字核对。
- search-batch-run01：两条正式 search 批次、重复启动和续跑验证。
- search559-completion-run03：47 条交接核对；search559-public-recovery-run04：真实 rollout 与人工环境复核；run05–07：从无 fetch、单行源码到保真获取的实际轨迹。search559-public-recovery-run07/output/public-source-audit.json 记录来源及接口差异。
- terminal-batch-run01：当前正式 terminal 批次，每阶段候选、检查和失败记录。
- terminal-real-import-check：真实依赖导入与本地业务读写探针。
- terminal-dependency-reference：固定公开依赖与原文源码比对，差异为七行原始脱敏占位；未替换候选文件。
- search38-solver-review-run01：同一环境的新 rollout 与人工内容核查。
- full-current-run02：旧修复循环的三次相同候选与重复失败；repair-loop-root-cause.json 保存对比。

上轮逐份分析与更正保留在 artifacts/pipeline-debug-20260930/root-cause-fix/。测试和静态检查用于保护已修复行为，不能替代上述真实产物核查。
