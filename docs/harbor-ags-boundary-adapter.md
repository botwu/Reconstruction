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

| source bundle | Agent AGS sandbox | verifier AGS sandbox |
| --- | --- | --- |
| `instruction.md` + runtime appendix | 最终 user prompt | 不可见 |
| `workspace/` | `/home/user/workspace` | 由 Harbor/AGS 按 verifier 需要提供结果与 artifact |
| `environment/` | 仅作为运行定义输入 | 仅作为运行定义输入 |
| `solution/` | 不可见 | verifier 侧可用于受控参考（不得泄漏给 Agent） |
| `tests/grader.py`、`tests/test.sh`、`tests/rubric.json` | 不可见 | `/tests` |
| `tests/control/` | 不可见 | `/tests/control/` |
| Agent 产物 | `/logs/artifacts/traceforge/` | verifier 读取受控副本 |

适配器在计划中冻结以下安全约束：verifier 使用 `environment_mode=separate`、`network_mode=no-network`；Agent 使用 `harbor_ags.agent:LosslessHermesAgent`，环境使用 `harbor_ags.environment:AGSPrebuiltEnvironment`；每个 Trial 删除沙盒并审计 `_control/ags-sandbox-ledger.jsonl`。

## 输出

每次计划生成一个原子 artifact run：

- `harbor_boundary_plan.json`：边界、入口、来源引用、SFT admission gate；
- `artifact_manifest.json`：计划文件摘要；
- `run_receipt.json`：运行身份和 `model_status=NOT_RUN`。

计划的 `status=READY_FOR_ROLLOUT` 只在现有 Harbor validator 通过时出现。真实 rollout 仍由 Harbor runner 单独发起，不能把计划生成视为 teacher 已运行。
