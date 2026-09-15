# 重建闭环真实活跑运行手册（Tier A / Tier B）

> 目标：把一条真实回流失败轨迹，经真实模型跑通 `reconstruct workflow` 的任务/环境/充分性/验证器合成与 Harbor bundle 编排，直至（环境就绪时）执行 rollout + RED-check + SFT 认证。

本手册对应命令入口 `traceforge reconstruct prepare` 与 `traceforge reconstruct workflow`。两层拆分源于环境依赖不同：**Tier A 只需 `config.yaml` + 出网**，**Tier B 额外需要可运行的 Harbor venv、AGS/TokenHub 凭据与北京 AGS 可达**。

## 不变量（活跑不得破坏）

- 回流是生成约束，不是 Ground Truth；Truth / Reference / Verifier 相互独立，不用多数投票造 GT；不确定就弃权。
- `INFRA_ERROR ≠ TASK_FAIL`；`RESULT_NOT_OBSERVED ≠ 执行失败`。已配对且被观测的完整工具轨迹应能自动 COMPLETE。
- 任何密钥不得写入 artifact、rollout 计划、task bundle 或日志。`config.yaml` 只存在于部署机、已被 gitignore、不进 Git。
- 真实回流数据、运行结果、模型缓存、参考仓不进本仓。

## 前置（Tier A）

1. 已修复的可编辑安装：`.venv/bin/traceforge --help` 与 `.venv/bin/python -m pytest -q` 均无需 `PYTHONPATH` 即可运行（详见 P0-0）。不要用 `uv run`（离线证书失败）。
2. `config.yaml` 位于仓根，含目标 channel（当前实测可用：`claude` / `gemini` / `deepseek` / `gpt`，均走 `tokenhub.sensetime.com`）。
3. 出网可达 TokenHub：`curl -sS -o /dev/null -w '%{http_code}\n' https://tokenhub.sensetime.com` 返回 200。
4. 一条真实 tool-using 失败 R01 记录（`return_data/four_batch/by-rubric/R01.jsonl`）。整文件很大且 FS 很慢，活跑时先用 `head -n` 切前若干条到小文件再编译。

## Tier A：真实模型 + 无 Harbor（闭环逻辑可验证终点）

```bash
# 1) 切一小片真实记录（避免在慢 FS 上整文件编译）
head -n 3 return_data/four_batch/by-rubric/R01.jsonl > /tmp/r01_slice.jsonl

# 2) 编译成 M1B run（内容寻址目录，命令回打印 run 路径）
.venv/bin/traceforge trajectory compile \
  --input /tmp/r01_slice.jsonl \
  --dataset-id r01-liverun-vX \
  --source-schema traceforge.restored-long-capture.v1 \
  --output artifacts/m1b
# 记 M1B_RUN=<上一步打印的目录>

# 3) 确定性准备：产出 report.json + 带正文 evidence.json（不调用模型）
.venv/bin/traceforge reconstruct prepare \
  --m1b-run "$M1B_RUN" \
  --output artifacts/prepare
# 命令回打印 prepare_manifest.json；从中读 capture_id / attempt_ref / source_report_id / report_path / evidence_path

# 4) 真实模型跑闭环（PLAN_ONLY，不执行 rollout）
.venv/bin/traceforge reconstruct workflow \
  --attempt-ref "<manifest.attempt_ref>" \
  --source-report-id "<manifest.source_report_id>" \
  --report-json artifacts/prepare/report.json \
  --evidence-json artifacts/prepare/evidence.json \
  --normalized-run "$M1B_RUN" \
  --capture-id "<manifest.capture_id>" \
  --harbor-root ../harbor_ags \
  --output artifacts/run \
  --config config.yaml \
  --channel claude \
  --model-name claude-opus-4-8
```

说明：

- `--harbor-root` 即使在 Tier A（PLAN_ONLY）也是必填参数；不加 `--execute-rollout` 时 Harbor 不会被调用，只冻结 rollout 计划。
- `--channel claude` 时 `--model-name` 默认解析为 `claude-opus-4-8`。若选 `deepseek`，默认模型 `vol/deepseek-v4-flash-0731`；PLAN_ONLY 下不需要 `--rollout-model`（P0-5 已让 rollout model 仅在 `--execute-rollout` 时强解析）。
- `--normalized-run` 与 `--replay-workspace`/`--replay-files-json` 互斥；活跑一律用 `--normalized-run`（内部自动回放任务开始前的初始 workspace）。多 capture 时用 `--capture-id` 唯一指定。

### Tier A 断言（活跑通过判据）

- 任务恢复：Task `status == COMPLETE`（证明 P0-1 门禁收窄：已配对+已观测轨迹不再被误判需人审）。
- 环境补全：env `READY` 且逐候选 `SUFFICIENT`。
- 验证器合成非 `None`；编成 3 个 Harbor bundle。
- rollout 计划以 `PLAN_ONLY` 冻结。
- 全程无崩溃：单次模型网络抖动被 `ModelGatewayError` 捕获为 FAILED/BLOCKED artifact 并跳过该候选，而非打穿 workflow（证明 P0-2）。
- 秘密卫生：`grep -rn 'sk-\|e2b_' artifacts/run` 应为空。

## Tier B：`--execute-rollout` → Harbor → 北京 AGS 沙箱 → RED-check → SFT

Tier B 在 Tier A 命令基础上追加 `--execute-rollout --rollout-trials 2`（`reproducible` 要求 trials ≥ 2，否则 SFT 不可能 ELIGIBLE；trials < 2 且 `--execute-rollout` 时 workflow 会打印明确告警——P0-4），非 Claude agent 还需 `--rollout-model`。

### Tier B 前置清单（缺一不可，均属环境边界）

1. **Harbor 可执行**：`_harbor_command` 先找 `<harbor-root>/.venv/bin/harbor`，再 `shutil.which("harbor")`。当前两者皆无，须在 `../harbor_ags` 建**可在本 Linux 运行**的 harbor venv。注意 handoff 警告：远端 venv 可能是 macOS 构建，不可跨平台复用，必须本机重建。
2. **凭据**：设置 `AGS_API_KEY` 与 `TOKENHUB_KEY` 环境变量（不写入任何 artifact/日志）。`config.yaml` 里的 `e2bapikey` 是死钥——无人读取，真实沙箱是北京 AGS，忽略即可。
3. **北京 AGS 可达**：从本机能连到北京 AGS 服务端点。
4. **trials ≥ 2**：`--rollout-trials 2` 起步。
5. **rollout 模型**：`--rollout-model` 需 Hermes 兼容的 `provider/model`（非 Claude 恢复模型必须显式给出）。

### Tier B 追加命令片段

```bash
.venv/bin/traceforge reconstruct workflow \
  ... （同 Tier A 的必填项）... \
  --execute-rollout \
  --rollout-trials 2 \
  --rollout-model <hermes 兼容 provider/model>
```

### Tier B 断言

- RED-check 通过：oracle `PASS`（1/1）、nop `FAIL`（0）、mutation `FAIL`（0）。
- SFT 认证到 `ELIGIBLE`；检查 `artifacts/run/result/`。
- 秘密卫生：`grep -rn 'sk-\|e2b_' artifacts/run` 仍为空。

## 已知边界（活跑选型规避）

- `resolve_model_name` 无 `glm` 默认：`glm-*` channel 会把 channel 名当模型 id 直传。活跑固定选 `claude` / `gemini` / `deepseek` 已知可用 channel 规避。
- 论文级不变量（control plane 预算/重试、迭代式验证器、Terminal-Universe 引擎、Truth/Reference/Verifier 模型独立性）尚未接线到生产路径；本手册不据其宣称已生效。当前 runtime 用同一个 `model` 贯穿 task/env/sufficiency/verifier。
