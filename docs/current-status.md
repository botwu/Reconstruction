# 当前状态

更新：2026-10-11；阶段回执核对至上海时间 01:53，交付索引快照为 00:48。本文只保留当前结论和限制，历次过程见 Git 与原私有产物。

## 目标与两类流程

最大限度利用完整原始 session，恢复忠实的任务及对应可解初态，交付标准 Harbor 包，再用真实求解检查环境是否足够。**执行完成、环境充分、答案正确分别记录；不能为可解而缩小原任务。**
原 system、工具定义、参数、返回、报错、补丁、历史回答和子 agent 输出都保留来源与时序。已采纳的历史方案可以是后续任务的必要输入；本次待求解的旧答案、目标实现和结论不能预置给 solver。

- **search：**完整轨迹 → 意图与来源边界 → 原资料/历史恢复及必要的真实检索 → 材料覆盖检查 → Harbor → 两次原生求解 → 逐项后审。
- **terminal：**完整轨迹 → 意图及初态/产物绑定 → 文件、依赖与业务能力恢复 → 原入口/已有行为实跑和独立充分性检查 → Harbor → 两次原生求解 → 原候选独立复核与反馈。
- domain 由调用方指定；Codex、Hermes、Claude Code 等 harness 只影响协议解释。timeline 用于溯源，不机械重放旧命令，也不把 NON_FILE 回答类型等同于无需初态资料。
- 仅真实必要环境缺口触发返修；solver 错误保持初态。默认无固定返修轮数，保留显式上限及 NO_PROGRESS；模型限流、捕获不全不能冒充环境缺口。

## 已有交付索引与质量边界

`recovery/delivery-index-current.json` 的 00:48 快照包含 **6 条 session、9 项任务、18 份原生答卷**：search 5 条/8 项，terminal 1 条/1 项。逐项绑定实际任务、环境、Harbor、双 trial、采用后审及旧失败续接关系。

| 已入索引样本 | 任务数 | 当前采用结果的边界 |
| --- | ---: | --- |
| R01:38 | 1 | 双原生与后审完成；仍有误引、未读完却声称全文已读及过度归纳 |
| R01:94 | 1 | 可用原工具计算观察支持条件判断；没有补造完整 CONTCAR 坐标 |
| R01:281 | 3 | 商品、物流、WhatsApp；物流采用用户允许的有来源历史报价，未核原件标签保留 |
| R01:468 | 2 | 完整有序原生轨迹后审已接续；各 trial 同 URL 的不同来源版本独立保留 |
| R01:842 | 1 | 真实 REPAIR 后交付必要源码；双原生和后审完成，没有强造完整组件 |
| R04:2179 | 1 | 原表头缺陷保留在初态；原测试/业务回读、双原生及限流后的独立后审完成 |

上述索引内没有已确认仍待修复的必要环境缺口，但答案仍为 **NOT_ASSESSED / SFT=false**。COMPLETE/SUPPORTED 不授予正确答案资格；历史 R04:1 等独立基准不混入本轮计数。
842 的自动纠偏是真实发生的；2179 首次后审限流返回 REVIEW，采用的是新目录中只复核原双 trial 的接续结果，未将旧失败改写为成功。
索引尚未追加后续 1029、1216 的阶段产物；下述状态不能与 9 项已入索引任务混算。

## 当前未完成与需保留的边界

R01:1029 的原 session 已结束为 BLOCKED：前三项各完成双原生和旧自动后审，第四项停在补全。完整 SOURCE 和试次均实际交接、无压缩丢失；自动 COMPLETE 仍出现目标判断遗漏。

| 任务 | 实际结果与独立判断 |
| --- | --- |
| 用户线 `ee8dd8cc622a932c` | 原要求一次性下发完整后续任务；旧合同把未知范围推到执行前核对，答卷成了条件计划/模板。原 SOURCE 中 7 段相关进度未交给 solver；自动 COMPLETE 漏检，不认定原目标充分 |
| 财务线 `15646e36a98acb3a` | 已知 Phase 3/4 的盘点与指令有依据；整条财务线剩余范围/总量仍未充分恢复。自动 COMPLETE 只支持当前有限合同，另有失败序号误推等 solver 错误 |
| 小说线 `a09a288316e6b69b` | 用户明确列出的三个后续阶段已有足够安排依据；答卷把上轮未改文件升级禁写、把附件补齐升级执行前提，属于 solver 加码，不能反称必需环境缺口 |
| 历史 UI 计划 `5c011a928caa4fe4` | 原任务是取回并重发旧计划。只有目录线索和未返回的历史搜索，没有计划正文；作者与独立补全复核均 BLOCKED。最小缺口是旧计划正文，不是整仓、截图或完整版本链 |

1029 前两项不能仅用“条件性/待核对”免责声明替代完整目标；第一项条件性交付的边界已询问用户，仍待答复，当前保留原目标。第四项不能用公开搜索或新写方案冒充私人历史原件。
已准备 task1 的 **MODULE_SEMANTIC_COMPARISON**：用 run06 正式后审模块、真实 checkpoint/cache、原环境与双 native，检查独立 reviewer 的新语义；仅离线预检通过，未发模型、不改旧 COMPLETE，也不冒称正式 retry-review CLI 已实跑。

R01:1216 正在处理 11 项任务：141 条消息、4 个工具定义、40 次调用/40 次返回已完整解析，11 份意图均核过完整 SOURCE。大量 security rejection/连接失败只能证明尝试，不能声称已读官网。
其报告 2.4/3.4 节及前次已选项目须按任务先后恢复；后续历史排序不能倒灌给要求选项目的任务。首项三类评估调研已双原生/后审 COMPLETE，独立核查未确认必要缺口，但发现分类、引用和“材料未覆盖”的错误；全 session 尚未结束，未加入上述索引。
第二项三个项目简短介绍已真实完成 BLOCKED → 独立复核 REPAIR → READY：官方替代正文足够，指定入口失败与来源时间差保留。两次原生均完成、两个沙箱关闭，独立核查无必要环境缺口，但答卷冗长且有功能条件泛化；正式后审 8 次 TPM 限流、无成功结论，记录为 BLOCKED / REVIEW_INCOMPLETE / MODEL_RATE_LIMIT，等待只重试后审，不重复 solver。第三项已自然启动，session 没有因该失败整体停住。
旧冻结代码把两任务的 4 处 `https://` 变为 `https:/`，已定位为路径别名规范化缺陷；真实旧产物保留。源码修复及 11 份真实模型输出的离线重放通过，不代表活动旧批已采用新代码。

R01:655 的 v26 真实意图已去除否定句中的旧答案泄漏，原六部分、九维度等要求保留。作者补全仍有局部材料缺口判断，独立补全复核 8 次均因 eastus2 TPM 无成功回复；终态 REVIEW_INCOMPLETE、进程和客户端已关闭，0 native/AGS，不能确认作者 BLOCKED 理由成立。
655 保留真实 15 条消息的作者 checkpoint；run06 续接已离线准备，仍沿用原 v26 task，不重跑意图。是否补齐资料、限流是否解除，要等真实接续。

terminal9 **尚未启动**：R04:364、1090、1816、2542、3268、3994、4720、5446、6172。run06 准备保持 offset=1、limit=9、workers=1、每 task 两次原生、无 resume，输出独立。
5446/6172 已有真实意图复验，不等于这九条已完成重建或求解。1403、1590 仍排在当前 search 串行批后段。

## 已提交的修复及未验证部分

- **v27 目标完整性 / fresh review：**已提交 `0edfb8f` 并包含于 run06。意图不因材料不足缩目标；补全重视进度/更正链；同一次后审改用独立会话，对照完整 SOURCE、原交付与双 trial。作者继续接收反馈，缓存隔离，不增加审核阶段。直接测试和无损请求构造已验证，**新语义尚待真实模型复验**。
- **URL 保全：**已提交并推送 `e83f4de`，run06 据此冻结。网址不参与文件路径提取/替换；大小写、查询、fragment、尾斜杠与相连中文原样保留。7 项 RED→GREEN、74 项绑定检查及真实 11 输出重放通过。
- **补全复核无损去重：**仅等值重复的工具返回改为原消息引用，完整 SOURCE/实际交付不变。655 未过滤原请求 SHA 命中，1,648,046→866,401 UTF-8 字节，20 条返回可还原；47.43% 是字节减少，不是 token 降幅或 TPM 已解决。
- **正式 `reconstruct retry-review`：**仅针对后审失败，在新目录认证并复核完整 native；不重建、不重跑 solver，REPAIR 不暗中返修。候选、原生输入及依赖绑定已在真实 2179/468 离线认证；正式 CLI 尚未真实调用模型。旧 COMPLETE 不属于此入口的失败重试范围。
- 既有完整 SOURCE 保护、显式初态/产物绑定、跨验收类型保留必要输入已落地；不把“原文可读”当作下游已利用，也不以结构 READY 替代语义审核。

## 旧试批与恢复真实性

旧版 `3959547` 首批 20 条在 10 月 10 日 04:20:46 结束为 COMPLETED_WITH_ERRORS，当时 **0 项端到端通过**：

| 旧批统计 | search | terminal |
| --- | ---: | ---: |
| session / task | 10 / 29 | 10 / 20 |
| Harbor 包 / native trial | 22 / 44 | 0 / 0 |
| 成功答卷 | 0 | 0 |
| session 终态 | 10 BLOCKED | 10 REVIEW |

44 份 search 原生轨迹均为 INFRA_ERROR/空答卷；Claude ID 配置多了一层 provider，396 个唯一请求均 HTTP 503。terminal 的 AGS 只是重建探针。44+77 个沙箱已终止，旧配置、失败和清理原件保留。
之后的权限、绑定、输入交接与服务修复在新目录冷跑或显式接续；两者分别记载，不能把复用候选/检查点称为全新冷处理，也不能因外层退出 0 计成功。
真实 SIGINT/--resume 已证明已完成条目 820 文件和中断尝试 52 文件不变、仅未完成 session 新建 attempt，21 个沙箱清理；恢复业务结果仍可 REVIEW。普通 batch --resume 是 session 级，不自动重试已记账失败。
全量数据 1,683+6,535=8,218 条未全部运行。仍先确认质量，再扩量和并发；不宣称 20 条全部通过或 100–200 条批量能力已验证。

## 模型、服务与运行范围

现冻结解析/重建/复核使用 `gpt-6-astra/azure/sfa`，配置上下文 1M；原生 solver 使用 `claude-opus-4-8/awsb_L/sfa`，每 task 两 trial。全局单一模型/AGS运行窗口、workers=1，新准备不抢占在跑 search。
Serper 搜索/抓取已真实成功，不能从单次 credits 推断账户余额；宿主按私有搜索配置走用户代理，AGS 已验证直连。TPM 是实际服务失败，不能据此断言资料缺失，也未承诺限流消失。
正式读取选 Serper。Jina 对费率 JPEG 虽 HTTP 成功却没有可用表格正文；原图已通过 Astra/Claude 原生视觉实际进入请求并核过表头和首行，不能外推全表正确。整图 OCR 出现金额误读，裁切结果也须保留来源与范围。
宿主 OCR 配置漏项已补；这不等于 solver 获得动态 OCR。Harbor 交付实际取得的材料，必要性逐任务判断，不加统一 OCR/整仓构建/GUI 门槛。既有 AGS 模板可用，不为本轮 terminal 强制重建模板。
只修改远端 TraceRconstruction；AgenticFoundry 保持只读参考。原始 R01/R04 按授权经 Git LFS 发布，其余私有配置、完整捕获和运行产物不入 Git。

## 关键私有证据入口

下表除 `artifacts/` 开头的条目外，均相对 `artifacts/pilot-20261010/`。索引链接最终采用路径，旧失败不覆盖；这里只列审阅入口，不复制历史记录。

| 入口 | 用途 |
| --- | --- |
| `recovery/delivery-index-current.json` | 6 session / 9 task / 18 trial 的实际交付、后审和续接关系 |
| `recovery/remaining16-run03/audits/` | 842 闭环、1029 四任务反证/边界、1216 解析与逐模块独立审核；含 URL 真实重放 |
| `recovery/remaining16-run03/search5/runs/` | 当前 search 冻结批的 source、意图、环境、Harbor、原生与正式终态 |
| `recovery/search1029-task1-fresh-review-module-run01-prepared/` | 新模块对照绑定/离线预检；395,278 字节请求保全原文和双 trial，尚未执行 |
| `recovery/search655-v26-complete-run01/`、`recovery/search655-v26-checkpoint-resume-run06-prepared/` | 655 原限流与真实 checkpoint、新代码接续准备 |
| `recovery/quality-recovery-freeze-run06/`、`recovery/remaining16-run06/terminal9-control/` | e83f4de 冻结及 terminal9 离线准备；不代表已获运行成功 |
| `recovery/remaining16-run02/terminal2179/`、`recovery/terminal2179-independent-review-retry-run01/` | 2179 原冷跑、双原生、限流及仅后审接续 |
| `recovery/search281-logistics-v25-complete-run01/`、`recovery/search281-whatsapp-v25-resume-run01/` | 用户授权边界下的最终交付和原生后审 |
| `recovery/search468-postreview-recovery-run02/`、`batch-run04/audits/` | 完整 trial 交接接续与独立内容核查 |
| `artifacts/pilot-20261009/run01/`、`artifacts/pipeline-debug-20261009/terminal-two-session-batch-run01/` | 旧 20 条失败原件，以及真实中断/恢复回执 |

实现入口见[源码地图](rebuild-live-map.md)、[反馈流程](researcher-pipeline.md)、[Harbor 格式](harbor-task-delivery.md)与[批量运行](batch-reconstruction.md)。
