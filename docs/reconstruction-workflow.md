# Agent World 失败轨迹重建工作流

这份文档描述当前代码真正执行的语义模块。历史设计文档中的 M1/M4 等编号只作为兼容字段保留；新代码和新产物使用下面的名称。

## 输入与重建单位

重建单位是一个 attempt：同一 session 先由 failure-analysis/query-turns 切分为多个尝试，再对需要重建的失败或未完成尝试执行本工作流。原始 session 不直接作为一个训练样本。

输入包括：

- report：失败分析的结构化事实、失败原因和完成状态；
- evidence：带 evidence_ref_id 的用户文本、工具观察、文件观察；
- replay_workspace 与 replay_files：确定性回放得到的任务开始前 workspace；
- attempt_ref 和 source_report_id：跨阶段稳定引用。

## 语义模块与职责

0. **Failure Analysis Agent**（failure_analysis/model_analyzer.py）  
   在确定性结构分析之后判断 SUCCESS/FAILURE/INCOMPLETE/UNCERTAIN，定位首个未解决失败步骤，给出用户意图边界、重建价值 rubric 和 needs_reconstruction。未知证据、未知事件、状态矛盾一律 REVIEW；不会把缺失标签当失败。

1. **Task Recovery**（task_recovery.py / semantic_recovery.py）  
   依据用户显式请求重述任务、验收义务、约束和歧义。模型只能引用 evidence 索引；未知引用、缺失验收义务或低置信度进入人工复核。

2. **Environment Completion**（environment_completion.py）  
   复制确定性回放 workspace，只允许补充 PARTIAL 或缺失文件。COMPLETE/UNKNOWN 文件、solution、tests、environment、路径穿越和无证据内容都会被拒绝。产物中的每个文件带 provenance 和 evidence 引用。

3. **Workspace Sufficiency**（sufficiency_judge.py）  
   独立只读判断环境是否“可解但未解”。UNKNOWN、缺证据或 REVIEW 均不能进入 Harbor。

4. **Verifier Synthesis**（verifier/synthesis.py）  
   根据验收义务生成隐藏 pytest、至少两个独立 oracle 解和一个 mutation 解。只做 AST/引用校验，不在宿主机执行模型代码，状态为 UNVALIDATED，必须经 RED-check。

5. **Harbor Bundle Compiler**（verifier/bundle.py）  
   输出 Harbor 1.4 任务包：Agent 可见内容位于 workspace/ 和 instruction.md；参考解、测试、验证规格位于隐藏的 solution/、tests/ 和 tests/control/。运行时通过 verifier.collect 在沙盒销毁前快照最终 workspace。

6. **RED-check**（verifier/red_check.py + workflow）  
   对 oracle、no-op、mutation 三类对照分别运行 Harbor，要求 oracle=PASS/1，no-op=FAIL/0，mutation=FAIL/0。任一缺失、基础设施错误或结果不一致都不能放行。

7. **Hermes Rollout 与结果门禁**（harbor_ags/rollout.py、results.py）  
   默认只生成冻结计划；显式执行时读取当前进程凭据。结果必须有完成状态、轨迹、artifact manifest 和沙盒清理记录。

8. **SFT Curation**（curation/sft.py）  
   仅允许验证 PASS、reward 达标、任务/环境置信度达标、轨迹完整、无答案泄漏且可复现的样本进入 SFT。泄漏或不可复现明确 REJECT；缺少这两项审计证据为 REVIEW。

## 统一入口

Python API：

    from traceforge.reconstruction.workflow import run_reconstruction_workflow

CLI：

    PYTHONPATH=src python -m traceforge reconstruct workflow \
      --attempt-ref ATTEMPT \
      --source-report-id REPORT \
      --report-json report.json \
      --evidence-json evidence.json \
      --replay-workspace replay/workspace \
      --replay-files-json replay/files.json \
      --harbor-root /path/to/harbor_ags \
      --output artifacts/reconstruction

上面的命令只生成 Task、Environment、Sufficiency、Verifier、Bundle、RED-check 和 Hermes 的冻结计划。增加 --execute-rollout 才会执行 Harbor/AGS；真实运行需要在 dev-wj 进程环境中配置 AGS_API_KEY 与 TOKENHUB_KEY（密钥不写入 artifact）。

## 每阶段可检查的输出

工作流结果目录包含 workflow_manifest.json、metrics.json、execution.json、sft_curation.json。各阶段目录分别保留公开 artifact、私有模型 exchange（不含密钥）和 hash manifest，可独立重跑和审计。

## 关键研究指标

- 重建可执行率：通过充分性、bundle 校验并能启动 Harbor 的比例；
- RED-check 通过率：三类对照同时满足预期的比例；
- Hermes 完成率、任务通过率、轨迹捕获率、artifact manifest 完整率；
- SFT eligibility rate，以及人工复核后保留率；
- 与原始失败轨迹相比的任务通过率、轨迹长度、token 成本和错误类型分布。

rollout_trials=1 只能证明单次结果，不能证明可复现；SFT 门禁会将其标为 REVIEW。

