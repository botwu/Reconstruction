# Harbor/AGS 边界适配

版本：v1

本模块把已经编好的 Task Bundle 映射成 Harbor/AGS 可执行计划。它只做协议转换、目录边界审计和执行参数冻结，不调用 Hermes、不创建 AGS 沙盒，也不判断任务语义正确性。

它**不是** `reconstruct raw-run` 的主入口。重建主链在 Sufficiency 之后、且 `allow_file_verifier=true` 时，由 [`verification.py`](../src/traceforge/reconstruction/verification.py) 直接调用 [`harbor_ags/rollout.py`](../src/traceforge/harbor_ags/rollout.py) 做 RED / rollout。`harbor-ags plan` 只用于已经落盘的 Bundle 做边界审计。

没有 FILE 义务时重建不会走到这里（`NO_FILE_ACCEPTANCE`）。

## 和 reconstruct 的关系

| 命令 | 做什么 |
| --- | --- |
| `reconstruct raw-run` | 重建 q/E；默认不做 RED |
| `reconstruct raw-run --execute-red` | Verifier 编 Bundle 后，对**初始 workspace** 做 nop/oracle/mutation。通过才标重建 READY |
| `reconstruct raw-run --execute-rollout` | RED 后执行 Hermes 复验；完整验收才可 READY/SFT，未验证义务保留 REVIEW |
| `harbor-ags plan` | 对现成 Bundle 做 dry-run 边界计划，`model_status=NOT_RUN` |
| `harbor-ags prepare-rollout` | 物化 Harbor Dataset 并生成显式 dry-run 计划 |
| `harbor-ags execute-rollout` | 执行已审核的 rollout plan（独立于 reconstruct） |
| `harbor-ags read-results --plan-dir ...` | 核对冻结输入与执行，认证 Hermes 轨迹并验收最终响应 |

`plan` 的 `status=READY_FOR_ROLLOUT` 只表示 Harbor validator 通过，不表示 teacher 已跑、也不等于重建 READY。

## 运行命令

```bash
PYTHONPATH=src python -m traceforge harbor-ags plan \
  --task-dir /absolute/path/to/task \
  --harbor-root /mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags \
  --source-ref m4-report:<id> \
  --output /absolute/path/to/harbor-plans
```

`--harbor-root` 提供时，适配器调用现有 `harbor_ags.task_bundle.validate_task_bundle(..., require_control=True)`。缺少该参数时仍做本地结构检查，但计划状态为 `LOCAL_LAYOUT_ONLY`，不能作为 rollout 授权。

独立执行已审核计划：

```bash
PYTHONPATH=src python -m traceforge harbor-ags execute-rollout \
  --plan-dir /absolute/path/to/plan \
  --config config.yaml \
  --channel claude
```

独立 Hermes 执行完成后验收：

```bash
PYTHONPATH=src python -m traceforge harbor-ags read-results \
  --plan-dir /absolute/path/to/plan
```

Job 从冻结计划推导；可同时传 `--job-dir`，但必须与该计划一致。Hermes 只提供
`--job-dir` 会拒绝完整验收，避免漏用响应合同。`oracle/nop` 仍可用
`--job-dir ... --agent-mode oracle`（或 `nop`）读取校准指标。

读取会校验 plan、dataset、隐藏 `tests/control/input-manifest.json` 中的
`task_acceptance`、执行收据及 trial 的 `config.json/task.path`。仅完成且成功的
执行进入 Hermes 认证；失败或未认证的 trial 不生成有效响应验收收据。
最终 assistant 消息索引、响应/轨迹/合同哈希与检查结果保存在
`rollout_results.json`。文件缺失、回复缺失、未支持的检查和未覆盖 NON_FILE
义务不能得到 `acceptance.status=PASS`。CLI 只有完整验收 PASS 才返回 0。

此命令有本地派生文件写入：既有认证器重建 trial 的 `artifacts/manifest.json`
和 `reconstruction-certification.json`，结果读取原子替换 plan 目录下的
`rollout_results.json`。重复读取重新校验当前字节并重算收据，不沿用过期 PASS；
它不会启动模型、创建 AGS 沙盒、重新执行 rollout、修改 task workspace 或轨迹。
输入绑定校验报错时不覆盖旧报告；旧报告只对应其中记录的输入哈希，不能作为本次读取通过的依据。
`quality_gate` 继续描述原始运行证据质量；合同与任务通过情况看 `acceptance`。
独立验收不会修改重建状态，也不会独自宣称 RED/SFT 认证完成。

## 边界映射

本项目区分 Bundle 文件归属与 Agent 运行时可见性。`public_paths` 只表示可作为 Agent 初始工作区发布的文件；runner 元数据即使随 Bundle 分发，也不等于 Agent 可读。

| Bundle 路径 | 归属 | Agent 运行时 | verifier 运行时 |
| --- | --- | --- | --- |
| instruction.md | public agent surface | 作为最终 user prompt 注入 | 不需要 |
| workspace/** | public agent surface | 挂载为 /home/user/workspace | 可按测试需要读取受控副本 |
| task.toml | runner metadata | 不挂载 | Harbor/AGS 调度读取 |
| environment/** | runner metadata | 不直接挂载；仅用于构造运行时 | verifier sandbox 的运行定义输入 |
| solution/** | hidden reference | 不可见 | 仅 verifier/离线审计可读，禁止泄漏 |
| tests/grader.py, tests/test.sh, tests/rubric.json | hidden verifier | 不可见 | 挂载到 /tests |
| tests/control/** | hidden control truth | 不可见 | 挂载到 /tests/control/ |

environment/ 是构造运行时的输入，不是 Agent public workspace。若某个环境文件确实需要让 Agent 在任务中读取，必须复制到 workspace/ 并留下 provenance；不能因为它位于 environment/ 就默认可见。

solution/ 与 tests/ 永远属于 Agent 隐藏面。兼容当前 HarborBundleManifestV1 时，hidden_verifier_paths 可以同时记录 solution/** 和非 control 的 tests/**；消费者必须将该字段解释为“受保护路径”，不能据字段名推断 solution 会暴露给 Agent。

Agent surface 的规范化声明：

```json
{
  "visible_sources": ["instruction.md", "workspace/"],
  "runner_only_sources": ["task.toml", "environment/"],
  "hidden_sources": ["solution/", "tests/"],
  "visible_roots": ["/home/user/workspace"]
}
```

Verifier 使用 `environment_mode=separate`、`network_mode=no-network`；每个 Trial 删除沙盒并审计 `_control/ags-sandbox-ledger.jsonl`。Agent 只能看到 instruction 和 public workspace，最终 artifact 写入 `/logs/artifacts/traceforge/`。

本地结构检查要求：`task.toml`、`instruction.md`、`workspace/`、`environment/`、`solution/`、`tests/`、`tests/control/`，以及 `tests/test.sh` / `grader.py` / `rubric.json`。`test.sh` 必须是 `#!/bin/sh` + `python3 /tests/grader.py`。不允许符号链接。

## 输出

每次 `plan` 生成一个原子 artifact run：

- `harbor_boundary_plan.json`：边界、入口、来源引用、SFT admission gate
- `artifact_manifest.json`：计划文件摘要
- `run_receipt.json`：运行身份和 `model_status=NOT_RUN`

计划生成阶段的 `model_status=NOT_RUN` 是事实状态。若上游重建节点尚未调用模型，应在 pipeline 中保持 `PENDING_MODEL`，不能提前写成 `COMPLETED`。
