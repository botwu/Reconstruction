# 原始 session 批量重建

先按已知 domain 建立输入清单，再通过与单条调试相同的 raw-run 入口逐条执行。R01/R04 是源文件标识；清单沿用的 rubric 字段只用于定位来源，不参与筛选或路由。

## 准备输入

先按[跨机调试](cross-machine-debug.md)恢复完整数据、运行时和私有配置。在项目根目录执行；准备步骤只校验 JSON、行号和哈希，不调用模型。

```bash
"$TRACEFORGE_PYTHON" scripts/prepare_session_batch.py \
  --source-dir return_data/four_batch/by-rubric --source-codes R01 --domain search \
  --distribution data/debug-datasets.json \
  --frozen-dir artifacts/batch-inputs --output artifacts/search-inventory

"$TRACEFORGE_PYTHON" scripts/prepare_session_batch.py \
  --source-dir return_data/four_batch/by-rubric --source-codes R04 --domain terminal \
  --distribution data/debug-datasets.json \
  --frozen-dir artifacts/batch-inputs --output artifacts/terminal-inventory
```

准备会复制原始字节并生成每条的物理行号、字节位置和 SHA256；已有冻结文件不会被覆盖。两类清单分别传入对应 domain，混用会在启动模型前报错。

## 先执行小批次

以下命令真实调用模型。search 示例选第 38 条，terminal 示例选第 1 条；offset 从 0 起算，原始 line_number 从 1 起算。扩大范围时调整 offset/limit，删去二者才会处理整份清单。

```bash
"$TRACEFORGE_PYTHON" scripts/run_session_batch.py \
  --manifest artifacts/search-inventory/source_manifest.json --domain search \
  --offset 37 --limit 1 --output artifacts/search-batch \
  --config config.yaml --hermes-home "$PWD/.runtime/hermes-agent" \
  --harbor-root "$PWD/.runtime/harbor-ags" \
  --execute-rollout --rollout-trials 1 --manual-response-review

"$TRACEFORGE_PYTHON" scripts/run_session_batch.py \
  --manifest artifacts/terminal-inventory/source_manifest.json --domain terminal \
  --offset 0 --limit 1 --output artifacts/terminal-batch \
  --config config.yaml --hermes-home "$PWD/.runtime/hermes-agent" \
  --harbor-root "$PWD/.runtime/harbor-ags" \
  --execute-red --execute-rollout --rollout-trials 2 --manual-response-review
```

加上 --plan-only 可以只生成计划，不请求模型。实际执行固定 workers=1；两种领域使用各自的补全和验证策略，不按原始 harness 名称拆出另一条重建管线。

## 结果与续跑

- batch_manifest.json 原子保存已完成条目、当前运行目录、状态计数和执行参数；batch_results.jsonl 是逐条结果。
- 每条原始 session 的阶段产物位于 runs/<session_id>/。search 读取 manifest.json，terminal 读取 reconstruction_manifest.json；仅解析 READY 不算整条完成。
- 单条失败或超时会保留回执和日志，继续下一条。COMPLETED_WITH_ERRORS 返回退出码 2；具体 REVIEW、缺少来源、模型错误或超时仍分别保留，不抹成同一个原因。
- COMPLETED 只表示所选条目完成管线，不能替代 tasks 中的 acceptance 或人工内容核查。NOT_ASSESSED 不会自动变成验收通过。
- 控制端中断后，以完全相同的命令加 --resume。已完成条目不再调用模型；未完成条目在 retries/<序号>/ 重新执行，旧尝试不覆盖。已结束的失败条目也保留，修复代码后应建新批次重跑这些样本。
- 同一输出目录只允许一个批处理进程。若先前子进程仍在运行，续跑会拒绝重复启动；不能仅因为 SSH 断线就再开一份。
- 每条默认总时限 7200 秒，可用 --session-timeout-seconds 调整。超时先向本地进程组发送 SIGINT，最多等待 30 秒清理后才强制结束；远端沙箱的实际清理结果须核对相应运行回执。

恢复时绑定原选定行、domain、代码目录、源码/入口与配置内容的 SHA256，以及执行参数；同一路径的代码或配置内容改变也会拒绝续跑，须使用新批次。实验仍应冻结运行时目录并保留版本，避免运行过程中更换依赖；配置凭据不提交到公开仓库。
