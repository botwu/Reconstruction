# 当前状态

更新：2026-09-29 UTC。本文只记录当前可复核结论；历史失败保留在归档中。

## 实际输入与结果

输入为调用方已知 domain 的完整 session，不作初始筛选、领域推断或脱敏。上游 `by-rubric` 已完整复制：103 个文件、15,448,184,178 字节，源与目标逐文件 SHA256 一致。R04 当前为 6,535 条；旧截断副本保留备份。第一条原本已有五处占位，复制不会补回已经缺失的内容。

| 样本 | 实际执行 | 当前结论 |
| --- | --- | --- |
| R01 物理第 39 条，search | search05 两次 Claude rollout，分别 7、5 次真实检索/读取 | 执行完成；历年 CSTRO oral 条目未核实，回答内容保持 `NOT_ASSESSED` |
| R04 物理第 1 条，terminal，模块提取与分析 | task1/run09 两次真实 rollout，FILE 评分均为 1，原输入保留模式下认证通过 | 文件行为检查通过；分析回答按用户选择逐份人工核查，不能认定 SFT 合格 |
| 同一 R04，后续超时修复 | task2/run14 审查与校准通过，两次真实 rollout 完成 | 第一份评分 1，独立六项复核全部通过；第二份遗漏网络超时重试，评分 0，独立复核一致拒收 |

search 原行 SHA256 为 `8c33a4c06f4973daec9dd9ac9185eae28e83c08fe0b7b152a5e15458a70db2f7`；terminal 为 `2c9d609055058904e0162e0d5e3ab74116f2a6d4a765b08b3c6a8de4e3c67486`。

两条样本最初从默认 `raw-run` 进入，随后复用了同源解析、任务或作者检查点，并根据实际失败继续执行。未手改模型 JSON，也没有把中途续跑描述为一次无人介入的完整运行。DeepSeek 接收完整消息和系统上下文；当前证据不代表压测了满 1M tokens 或所有 harness。

## 本轮修复与复核

- 把语法错误的编译器消息和原文位置交回作者，删除两处 2,000 字符的错误截断。
- 独立语义审查保留尚未解决的实际校准失败，直到新执行替换；反馈绑定候选和测试哈希。
- 既有正常行为必须按原初态保留。验证器须复现原失败的实际输入层级，GraphQL 错误响应不能用同名网络异常替代。
- 批处理显式传入已知 domain；search 入口不再初始化 AGS，terminal 默认使用 AGS。

run13 的两次正式评分均为 1，但独立 AGS 复核发现 HTTP 200 的 GraphQL `DeadlineExceeded` 仍失败。因此另记拒收，保留原正式回执，将完整失败交回同一作者。run14 同时覆盖应用层错误与网络超时；初态失败、两份参考解通过、错误实现失败。第一份实际产物通过六项独立 AGS 检查；第二份只修复 GraphQL 错误，网络超时仍直接退出，正式测试与独立检查一致拒收。

第二份认证还曾把 pytest 缩略显示的原输入常量误判为新增凭据。认证器现仅在初态绑定有效、缩略前后片段可由原文证明时认可这一显示形式；新凭据和显式运行密钥仍拒收。34 项认证检查通过。复核结果为 `TASK_PASS`、`TASK_FAIL`，保留原始轨迹、评分和历史回执。

task1 的两份产物都完成了模块提取。第一份保留原 `saved_data` NameError，第二份修复后可保存状态；文件评分不应掩盖这个实际差异。外部 X/Telegram 行为通过明确的受控网络边界检查，没有宣称真实账户操作成功。

## 版本、审核与清理

本轮执行源码固定为 `default-integration-code-13.tar.gz`，SHA256 `b09eaf0635bf95d0fa31737528f29287ee19a67df412a2deeae88311347fa906`。固定该受测版本后，单独提交精简，保持结果可追溯。冻结表示明确保留已验证的行为与边界，不把一成功一失败改写为全部通过；run14 整体仍为 `REVIEW`，不授予 SFT 资格。

精简删除无逻辑转发、重复条件、重复递归处理、无用导入及重复提示。完整非实测回归 1,380 项通过、5 项跳过；精简后相关 122 项通过，生产源码与脚本 F/E9 检查通过。两条真实输入的消息视图、工具时间线和分段索引在精简前后相同。这些检查不代替上面的真实运行。

仅清理已逐文件匹配归档的八个旧实验目录，保留原始数据、当前交付、运行依赖和关键失败证据。实际提交与清理数量以回执为准。

证据统一存放在项目的 `artifacts/pipeline-debug-20260929/default-integration/`：

- `by-rubric-upstream-copy.json`：完整上游复制回执。
- `terminal09-source-preservation-recertification.json`：task1 正式复核。
- `terminal13-output-quality-review.json`、`terminal13-rejected-output-archive.json`：评分漏测的实际失败与归档。
- `terminal14-abbreviation-recertification.json`、`terminal14-output-quality-review.json`：正式认证与两份实际产物的独立复核。
- `simplification-real-input-equivalence.json`：精简前后的真实输入对比。
- `release-freeze.json`、`cleanup-receipt.json`：源码固定与实际清理记录。

流程、循环和模块边界分别见 [原始会话流程](raw-session-pipeline.md)、[研究者管线](researcher-pipeline.md)、[源码地图](rebuild-live-map.md)。
