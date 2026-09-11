# Harbor/AGS 边界适配

版本：v1

本模块把已经通过重建与静态验证的 Task Bundle 映射成 Harbor/AGS 可执行计划。它只做协议转换、目录边界审计和执行参数冻结，不调用 Hermes、不创建 AGS 沙盒，也不判断任务语义正确性。

## 运行命令

```bash
traceforge harbor-ags plan \
  --task-dir /absolute/path/to/task \
  --harbor-root /mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags \
  --source-ref m4-report:<id> \
  --output /absolute/path/to/harbor-plans
```

`--harbor-root` 提供时，适配器会调用现有 `harbor_ags.task_bundle.validate_task_bundle(..., require_control=True)`。缺少该参数时仍执行本地结构检查，但计划状态为 `LOCAL_LAYOUT_ONLY`，不能作为 rollout 授权。

## 边界映射

本项目区分 Bundle 文件归属 与 Agent 运行时可见性。public_paths 只表示可作为 Agent 初始工作区发布的文件；runner 元数据即使随 Bundle 分发，也不等于 Agent 可读。

| Bundle 路径 | 归属 | Agent 运行时 | verifier 运行时 |
| --- | --- | --- | --- |
| instruction.md | public agent surface | 作为最终 user prompt 注入 | 不需要 |
| workspace/** | public agent surface | 挂载为 /home/user/workspace | 可按测试需要读取受控副本 |
| task.toml | runner metadata | 不挂载 | Harbor/AGS 调度读取 |
| environment/** | runner metadata | 不直接挂载；仅用于构造运行时 | verifier sandbox 的运行定义输入 |
| solution/** | hidden reference | 不可见 | 仅 verifier/离线审计可读，禁止泄漏 |
| tests/grader.py, tests/test.sh, tests/rubric.json | hidden verifier | 不可见 | 挂载到 /tests |
| tests/control/** | hidden control truth | 不可见 | 挂载到 /tests/control/ |

environment/ 是构造运行时的输入，不是 Agent public workspace。若某个环境文件确实需要让 Agent 在任务中读取，必须复制到 workspace/ 并在 EnvironmentRecovery 中留下 provenance；不能因为它位于 environment/ 就默认可见。

solution/ 与 tests/ 永远属于 Agent 隐藏面。兼容当前 HarborBundleManifestV1 时，hidden_verifier_paths 可以同时记录 solution/** 和非 control 的 tests/**；消费者必须将该字段解释为“受保护路径”，不能据字段名推断 solution 会暴露给 Agent。

Agent surface 的规范化声明：

{
  "visible_sources": ["instruction.md", "workspace/"],
  "runner_only_sources": ["task.toml", "environment/"],
  "hidden_sources": ["solution/", "tests/"],
  "visible_roots": ["/home/user/workspace"]
}

Verifier 使用 environment_mode=separate、network_mode=no-network；每个 Trial 删除沙盒并审计 _control/ags-sandbox-ledger.jsonl。Agent 只能看到 instruction 和 public workspace，最终 artifact 写入 /logs/artifacts/traceforge/。

## 输出

每次计划生成一个原子 artifact run：

- `harbor_boundary_plan.json`：边界、入口、来源引用、SFT admission gate；
- `artifact_manifest.json`：计划文件摘要；
- `run_receipt.json`：运行身份和 `model_status=NOT_RUN`。

计划的 `status=READY_FOR_ROLLOUT` 只在现有 Harbor validator 通过时出现。真实 rollout 仍由 Harbor runner 单独发起，不能把计划生成视为 teacher 已运行。
