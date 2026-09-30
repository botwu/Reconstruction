# 当前状态

更新：2026-10-01（北京时间）。目标是从完整原始 session 恢复忠实、可解的任务与初始环境，再用真实 rollout 检验，最后稳定批量执行。阶段完成、文件评分与回答正确分别记录。

## 代码与数据

公开仓库为 https://github.com/botwu/Reconstruction 。R01 search 共 1,683 条、R04 terminal 共 6,535 条，完整原始数据已通过 Git LFS 发布，并按 data/debug-datasets.json 校验跨机下载与恢复。原数据已有的占位符保留，本项目不新增脱敏或初始筛选。

修改仅位于 dev-wj 的 TraceRconstruction。AgenticFoundry 是只读参考；本项目运行不依赖其目录。两种 domain 使用明确指定的不同策略，最终均导出 Harbor 任务。详见[跨机调试](cross-machine-debug.md)、[批量执行](batch-reconstruction.md)、[Harbor 交付](harbor-task-delivery.md)。

## 本轮真实运行

全量 8,218 条已完成字节、JSON 和逐行哈希盘点；不是全量模型重建。模型实跑仍使用已授权的固定样本。

| 样本 | 本轮实证 | 当前边界 |
| --- | --- | --- |
| R01:38，公开资料研究 | v4 环境已完成真实补全、Harbor 导出、Claude rollout 和 researcher 复核。5条原始正文实际读到且逐字段交付；4次真实查询、8份真实页面进入环境，原轨迹推动的3份新增来源被solver实际读取 | 人工内容仍为 NEEDS_CORRECTION：过程性前言和不完整引用。部分中文PDF抽取只保留英文摘要/题录及数字，不能说全文恢复；未执行原生Harbor容器 |
| R01:559，本地代码检索 | 47 条已返回记录完整交付。agent 取回公开 Normalize（254 行）和 transforms（249 行）、校验 Git blob；run07 已完成 Claude rollout，run08 记录具体版本缺口 | 503 行公开源码均来自真实 fetch，但 Normalize 参数和 EvalImageTransforms 与原调用不一致，尚未恢复私有版本；旧回答另有共享 oracle 与行号错误，NEEDS_CORRECTION |
| R04:1，terminal | 新分段恢复为两个任务；真实 DeepSeek v1.9 解析完成。第一项环境就绪，但六轮验证器语义检查均拒绝，未进入 RED/rollout | 验证器反复增加 AST 规则导致漏检和误拒绝。提示约束已修正但待真实验证；新运行等待明确外发授权，旧进程已停止并清理沙箱 |
| R04:1，独立候选检查 | AGS 真实加载 main.py 和已安装的 twitter-api-client 0.10.22；去重、状态保存与重读通过，候选不变、沙箱已清理 | 未替换本地模块；禁止网络连接。未验证外部账户操作或待实现的任务能力 |

R01:38 的内容核查区分了环境问题与 solver 错误：资料中已有原论文，但回答将不同编组方案各自最慢车辆的约 48 s / 122 s 再充风时间写成同一列车头尾差异；还混淆了开始制动与开始缓解的时间差。原始回答、轨迹和正式回执未回写。依据为[原论文第3.2、3.3节](https://pdf.hanspub.org/ojtt20220500000_70489163.pdf)。

R01:559 的新 rollout 已执行完成，researcher 返回 COMPLETE，正式 acceptance 仍为 NOT_ASSESSED。人工核查确认：去掉 v1_baseline 封装后直接调用共享底层仍不能检测底层的同向错误；源码引用还需将原 Read 工具的零基编号加一。已有输入足以发现这些回答错误，不应为此重复生成环境。但对原任务要求逐项定位具体实现的部分，run08 确认公开上游不能替代私有版本：原调用传入 clip_keys/clip_min/clip_max，公开 Normalize 不接受这些参数，公开 transforms 也无 EvalImageTransforms。该缺口尚未解决；不以同名文件或纯生成代码冒充原文。

R01:38 run05 的回答为 8,412 字符，实际执行 17 次工具调用。此轮通过代理工具执行 Claude，未运行原生 Harbor 容器；dev-wj 未安装 Docker。Harbor 包的原始材料与新增来源已逐项核对，但交付与容器执行、回答合格仍是三件不同的事。run05 冻结于内部 v4 版本标签升级前，交接校验逻辑相同；当前 v4 的 run06 补全、Harbor 包与后段真实 rollout 已完成；读取区间与实际工具回执哈希共同证明5条原始正文完整进入作者输入，原轨迹第3、4条还推动了3次新增真实 fetch。该核对不声称模型理解了全部语义。run06 的三条新增来源均被 solver 实际读取，链路记录在 source-use-audit.json；最终回答仍有过程性前言和不完整引用，人工状态为 NEEDS_CORRECTION。新增中文 PDF 的抽取仍不等于完整中文正文，相关可读范围不得扩大。

历史 R04:1 task1/run09 两次文件评分通过；task2/run14 两次真实 rollout 中一份通过、一份因漏掉网络超时重试而拒收。这些属于旧代码和旧阶段恢复路径，不代表本轮从原文开始已完整通过。

## 本轮定位并修复的问题

- 部分中文 PDF 的 Serper 抽取只有英文片段或乱码。访问成功及非空正文不足以证明全文恢复；现在要求 agent 对照可读正文、沿期刊页或公开版本继续获取，并明确摘要边界。真实补充资料保留 URL、版本、抓取时间和类型，solver 的 list_evidence 也展示这些索引。成功查询但零匹配只表示检索可用，不表示材料足够。
- R01:38 的旧 researcher 以“历史片段/已有新来源”为由删掉四条返回。v4 交接默认保留全部返回，排除只能引用任务答案或解题后状态，并提交逐字原文依据。旧 v3 环境不允许直接续跑；分类名和引文存在仍不能代替语义核查。
- 真实补全把来源 ID 改成分页区间或 user:索引，失败后又反复提交。反馈现在给出已交付及已读来源的准确编号表，让 agent 修正引用；不猜测或自动替换错误编号。
- 真实 reviewer 还混淆了作者和 solver 的读取：复核现从每次 trial/input.json 映射 read_evidence 的具体来源，不让 live:编号依赖模型猜测。实际重试已不再把那两篇未读UMT页面算给solver，原始环境和回答哈希保持不变；这仍不能保证模型所有语义判断正确。
- 真实 reviewer 曾在 rollout 后继续返回补全阶段 READY。现在每轮明确当前复核阶段，格式错误反馈给同一会话，保留已完成环境和 rollout；同类错误无进展时停止。独立真实复核重试已返回正确协议，原 rollout 全部文件哈希不变。但 reviewer 漏判了旧环境的四条漏交，COMPLETE 仍不能取代交接校验或人工验收。
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
- search559-completion-run03：47 条交接核对；search559-public-recovery-run04–08：从无 fetch、单行源码到保真获取及私有版本缺口的实际轨迹。run07/output/public-source-audit.json 记录来源及接口差异；run08/manual-review/public-recovery-review.json 记录尚缺的具体实现。
- terminal-batch-run01：当前正式 terminal 批次，每阶段候选、检查和失败记录。
- terminal-real-import-check：真实依赖导入与本地业务读写探针。
- terminal-dependency-reference：固定公开依赖与原文源码比对，差异为七行原始脱敏占位；未替换候选文件。
- search38-solver-review-run01：旧回答数值和占位引用核查。search38-source-handoff-run02：新来源索引实际被读取，仍发现期刊、TACS 架构和题名错误；未改写原答案。
- search38-readable-source-preflight、search38-readable-recovery-run03–05：可读公开来源恢复、旧漏交和错误来源编号的真实轨迹。
- search38-full-record-completion-run06：v4 完整原始正文读取、基于原轨迹线索的新增 fetch、原文与 Harbor 包逐字段校验；后续 solver-review/ 保留该环境的独立真实 rollout 和复核。
- search38-review-source-mapping-run02：针对真实读取归属错误，只重试复核，校验原 rollout 文件哈希保持不变。
- search38-review-retry-run01：只恢复复核阶段的真实调用、原 rollout 哈希验证及人工漏判记录。
- full-current-run02：旧修复循环的三次相同候选与重复失败；repair-loop-root-cause.json 保存对比。

上轮逐份分析与更正保留在 artifacts/pipeline-debug-20260930/root-cause-fix/。测试和静态检查用于保护已修复行为，不能替代上述真实产物核查。
