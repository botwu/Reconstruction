# 当前状态

更新：2026-10-01（北京时间）。

目标是从完整原始 session 恢复忠实的任务和可解的初始环境，交付原生 Harbor 任务包，再以真实 rollout 检查恢复是否充分。最终要稳定批量执行；阶段 READY、容器能够运行、回答正确是三个不同结论。目前不能宣布两个 domain 都已端到端验收通过。

## 框架与输入边界

完整 session → 任务分段和模型语义解析 → 任务要求与初态起点 → 按 domain 恢复环境 → Harbor 交付 → 真实 solver → 按失败归属反馈。

- search 恢复必要历史、原始返回、证据语料和检索能力。补全 agent 沿原轨迹线索获取真实页面、论文和代码，保留来源、版本、正文范围及缺口。
- terminal 按调用时序恢复文件初态、依赖和运行接口，在实际沙箱执行探针，再检查验证器与真实解题。
- harness 决定如何解释 system prompt、工具协议和调用返回，不决定任务 domain。
- 重建者保留完整轨迹；solver 不接收同任务中途答案。v5 以该任务第一条用户消息为起点；独立后续修改任务可以使用开始前的真实产物。
- 只有实际环境缺口返回补全。solver 的事实/推理错误和模型服务故障分别记录，不借返修向初态写入答案。

模块、契约与参考依据见[重建源码地图](rebuild-live-map.md)。AgenticFoundry 仅作只读参考，本项目不依赖其本地目录。

## 本轮实际结果

| 样本/阶段 | 已验证内容 | 尚不能宣称的部分 |
| --- | --- | --- |
| R01:38，Astra 解析 | 完整原文解析一次通过，5条工具事件与系统上下文有原始定位；实际输入64,649 tokens | 不代表所有 harness 已通过 |
| R04:270，Astra 解析 | 实际输入189,872 tokens；28条事件中27条返回、1条待返回；异步轮询、只读行为与截断源码已抽查 | 未验证1M输入，也不是该样本的完整terminal重建 |
| R01:38，v5恢复 | 原始5条返回全部保留并逐字段核对，实际分页覆盖全部正文；同任务旧答案不交付；14条本轮查询/页面记录；Harbor包与原始材料一致 | PDF仅为文本层读取；图像、复杂公式和空文本页未视觉核验 |
| R01:38，Claude rollout | 在v5环境完成真实解题：18次工具调用、9,926字符回答 | 复核未发现环境缺口，但四项均有solver引用、推断或表达错误；回答未通过 |
| R01:38，Astra独立rollout | 在同一冻结环境完成真实解题：66次工具调用、16,675字符回答，未提供原session答案；环境哈希不变 | 4项模型复核均SUPPORTED；已通读并人工抽查关键来源，未逐句人工核验全部21项文献 |
| R01:559，本地代码检索 | 47条返回完整交付；公开Normalize与transforms真实取回503行并校验Git blob；已有真实rollout | 公开源码接口与私有版本不一致，所需私有正文尚缺；不能生成同名文件冒充 |
| R04:1，当前terminal流程 | 真实解析与第一项候选已生成，依赖导入和本地业务探针有实证 | 验证器语义检查尚未收敛，当前新流程未进入RED/rollout；新外发运行待此前授权问题确认 |

R01:38 的原始 run03 在 Claude 完成后，researcher 请求遇到 TokenHub token 速率限制。失败环境和回答保留；仅恢复失败复核，不重新解析、补全或解题。复核恢复已成功；没有重新解析、补全或解题，原环境与回答哈希均不变。

Astra 独立回答已从真实论文中读出自动驾驶试验“1193吨、无长大上下坡”的条件，并保留摘要与正文、历史快照与本轮来源、文献结论与自身归纳的边界。这是已核对的改善，不代表整份回答已经逐句验收。

当前 search rollout 使用本项目工具代理执行真实模型。Harbor 原生任务包已经导出，但 dev-wj 没有 Docker，本轮没有运行原生 Harbor 容器 trial。历史 terminal 文件评分和旧版 rollout 的通过记录不能代替本轮完整流程验收。

## 本轮修复的根因

1. 中文PDF被普通网页服务抽取成英文片段或乱码。现在按实际HTTP响应识别PDF，支持无后缀地址和重定向，下载原文件并读取文本层。4份真实PDF已核对原字节哈希与中文文本；提取器、物理页码和空页记录进入产物，Harbor固定相同依赖。
2. 同任务旧分析借后续格式纠正进入初态。v5交接拒绝这种引用；原文仍完整供重建者使用。旧v4环境不能直接续跑冒充v5。
3. Astra工具请求使用了不支持的Chat组合，解析输出上限也超过服务允许值。改用现有Hermes Responses协议；解析上限固定128,000，实际模型标识为 gpt-6-astra/azure/sfa。没有另写一套agent循环。
4. API失败又被判为JSON或材料错误。真实失败保留上游原因，限流为MODEL_RATE_LIMIT；复核失败为REVIEW_INCOMPLETE，不追加不存在的格式诊断。Astra复核中再次遇到限流，已正确归类。通道可配置Hermes原有请求重试预算，本轮设为8，让原生退避有时间跨过短期限流；不另写工具或重建循环。
5. 删除无调用的旧筛选与分析脚本及配套测试，共748行；不再保留废弃rubric筛选分支。

旧回答的作者、刊名与数值归属错误属于已保存的真实失败，未手工回写成正确答案。模型复核的COMPLETE只能表示其判断环境足够，不能代替事实验收。

## 代码、数据与跨机完整性

公开仓库为 https://github.com/botwu/Reconstruction 。R01 search 共1,683条、R04 terminal 共6,535条，两份完整原始数据已通过Git LFS发布，并按 data/debug-datasets.json 校验下载恢复。全量8,218条完成的是字节、JSON和逐行哈希盘点，不是全量模型重建。

跨机仍需配置模型、检索凭据及固定的Hermes/Harbor AGS运行依赖，不能把能clone代码等同于完整运行环境就绪。见[跨机调试](cross-machine-debug.md)、[模型通道](model-gateway-config.md)和[Harbor交付](harbor-task-delivery.md)。

批处理已具备输入冻结、锁定、原子进度、失败保留与续跑。真实小批次验证过重复启动拒绝及完成项不重跑；当前为单工作进程，模型大上下文请求还受共享通道速率限制。样本语义与terminal闭环未收敛前，不扩成全量合格产物生产。

## 证据与复核状态

本轮证据在项目 artifacts/pipeline-debug-20261001/framework-astra-audit/，原始运行产物和凭据不进入Git：

- parser-R01-L38、parser-R04-L270、parser-semantic-review.json：真实解析与有限人工语义核查。
- pdf-fixed-live-result.json、science-engine-fixed-live.json：实际PDF下载和文本恢复。
- search38-astra-pdf-run02、task-start-boundary-audit.json：旧答案误交及修复依据。
- search38-astra-pdf-run03、search38-v5-handoff-audit.json、search38-v5-author-read-audit.json：v5全流程、完整原始交接与实际读取，以及限流失败。
- search38-v5-astra-solver、search38-v5-manual-content-review.json：冻结环境的独立真实解题、来源抽查和未验收范围。
- search38-v5-review-recovery：只恢复Claude失败复核的输入绑定与回执。
- search38-v5-astra-review、search38-v5-astra-review-resume02：Astra答卷复核的真实限流与保存会话恢复。

复核状态：Claude的复核恢复返回COMPLETE，含义是未发现新的环境缺口；四项义务均标SOLVER_ERROR。具体问题包括引用串线、将摘要中的被引研究归给当前论文、把承载超限扩大为“失稳阈值”、将间接观测扩大为控制理论不可观测，以及违反用户的陈述式表达要求。原run03失败回执不改写。

Astra独立回答的同口径复核已恢复完成，4项均为SUPPORTED。恢复从68条已保存消息继续，只新增1次模型调用、0次工具调用；没有重新解析、补全或解题，原始答卷与环境哈希不变。实际回执确认原生API尝试预算为8，但本次续接首个请求即成功，不能据此声称限流期间的吞吐已验证。该结论来自模型复核；人工已通读并抽查关键引用，尚未逐句核验全部文献，正式acceptance仍为NOT_ASSESSED。

可直接核查的最终答卷与回执（相对上述证据目录）：

- `search38-v5-astra-solver/rollouts/trial-01/answer.md`：真实Astra最终答卷。
- `search38-v5-astra-review-resume02/review-retry-receipt.json`：逐项复核及产物不变证明。
- `search38-v5-manual-content-review.json`：人工抽查范围与未完成范围。

前期证据保留在同级 search559-public-recovery-run04–08、terminal-batch-run01、terminal-real-import-check、search-batch-run01 等目录。测试保护已经确认的行为，不替代这些实际执行与产物核对。
