# Terminal-Universe 复现进度与实现边界

本仓库现在按论文的三阶段环境重建和四类 re-query 组织，不把论文中的大规模数据统计误称为已经复现。

## 论文机制

1. **Seed selection**：先按 repository、base commit、problem statement 去重，同一组选择 replay 暴露文件数、行数和字节数最多的 trajectory；不按原始 reward 选取。
2. **Stage 1 deterministic replay**：按时间处理 read/write/edit/exec，只保留首次 mutation 之前完整可见的文件；partial read、mutation 后 read、agent 新建文件和后续改动进入 withheld/private evidence。
3. **Stage 2 agentic completion**：completion agent 只能补 task 所需的上下文、依赖、配置、fixture 和 partial 文件，不能实现任务、写答案、写测试或泄漏 solution location。候选最多 5 个。
4. **Stage 3 sufficiency**：只读 judge 根据 task 和 workspace 判断是否有足够项目特定 source/config/data/structure；`SUFFICIENT + READY` 才能继续。
5. **Intent Recovery**：单轮使用 substantive user request；多轮保留首个任务主题，后续只吸收澄清、约束和同一任务的扩展。
6. **Single-WS**：每个 workspace 生成 5 个 grounded、可验证、互异的 query，合法候选中选择一个 rollout。
7. **Verifier**：verifier agent 生成自足 pytest；初始 workspace 上至少一个 missing-capability test 失败，protective tests 通过；之后再进行 solver rollout 和最终测试。
8. **Multi-Round**：保留失败后恢复的轮次，裁去末尾连续失败；至少包含两个通过轮次。

## 当前代码入口

- `reconstruction/terminal_universe_environment.py`：Stage 1、completion contract、候选安全校验、Stage 3 候选选择、同任务 max-exposure seed selection。
- `reconstruction/task_recovery.py`：Intent Recovery artifact runner。
- `reconstruction/environment_completion.py`：带 provenance 和 protected-file 校验的模型候选物化。
- `reconstruction/sufficiency_judge.py`：充分性判定 artifact runner。
- `requery/single_workspace.py`：C.2 五候选生成与可复现选择；CLI 为 `traceforge requery single-ws`。
- `requery/cross_workspace.py`：workspace profile、方向性依赖候选和 reference mount prompt。
- `requery/multi_round.py`：requirement ledger、用户 follow-up 和至少两轮通过筛选。
- `verifier/synthesis.py`、`verifier/iterative.py`：独立 verifier schema 与迭代生成接口。

## 与论文真实系统仍有差异

- 论文的 completion/sufficiency/verifier agent 在容器内通过 shell/file/pytest 工具主动检查；当前生产 workflow 的模型阶段仍通过窄 JSON gateway 和 Harbor rollout bridge 接入，真实容器内 completion loop、read-only sufficiency tool loop、verifier 初始 RED calibration 需要在 AGS 环境中完成验收。
- 论文的 359,593 条公开轨迹、68,263 个 replay environment、37,273 个 sufficient environment 和 SFT benchmark 分数不能由本仓库当前 demo 数据复现；仓库提供的是可审计的单样本/小批量机制复现。
- `build_query_task_input` 仍作为旧 API 保留，但主 prepare/workflow 已禁止 query projection，生产输入是完整 session。

## 复现验收顺序

```text
真实 trajectory
  -> normalize events
  -> select max-exposure seed
  -> deterministic replay
  -> completion candidates (<=5)
  -> sufficiency judge
  -> Intent Recovery / Single-WS
  -> verifier RED calibration
  -> Harbor/AGS solver rollout
  -> all-test PASS + trajectory capture
  -> SFT export
```

每一阶段都必须发布输入 hash、模型 receipt、candidate provenance、失败原因和可重算指标；任何 `REVIEW`、上下文溢出、缺失 trial 或 verifier infrastructure error 都不得进入 SFT。

## 容器验证路径（已实现）

`reconstruction/container_verification.py` 是生产 workflow 与 Harbor/AGS 之间的窄接口：

- `AGSRuntimeAdapter` 将 `AGSPrebuiltEnvironment` 映射为 `start/read_only、exec、upload_dir、download_dir、stop`。AGS 的 `start` 使用 `force_build=False`，停止后调用 `assert_cleanup_verified`。
- `run_completion_container` 先记录 replay workspace 的逐文件 SHA-256，再上传到 `/home/user/workspace`。`agent_runner(runtime, prompt)` 必须在 runtime 内调用 shell/file 工具并返回 `evidence_ref_ids_by_path`。下载后检查 replay 文件删除、内容改写、隐藏目录写入、未知证据引用；任一项失败为 `REVIEW`。
- `run_sufficiency_container` 以只读语义启动 runtime，上传后由 root 去除 workspace 写权限，再以 `user` 执行写入探针；judge 只能通过 runtime 主动 inspect，返回 `label/confidence/reason/missing_critical`。写探针成功、模型 JSON 非法或容器错误均不能通过。
- `run_verifier_red_calibration` 分离执行 missing-capability 与 protective 两组测试；前者必须全部 `FAIL`，后者必须全部 `PASS`，任何 `TIMEOUT/INFRA_ERROR` 进入 `REVIEW`。

`run_reconstruction_workflow` 新增三个注入点：

```python
container_runtime_factory=...       # 每个 candidate 创建新的 AGS runtime
container_completion_runner=...     # async (runtime, prompt) -> JSON manifest
container_sufficiency_runner=...    # async (runtime, prompt) -> judge JSON
verifier_executor=...                # 同步 executor，驱动 iterative verifier
```

同时提供 `run_environment_completion_container`，它把容器下载结果发布为标准 `environment_completion.json`，可直接被后续 Harbor bundle 消费。没有传入这些注入点时，workflow 仍为 plan-only/兼容的 JSON gateway 路径，不能把该路径标成论文级容器验收。

### Harbor/AGS 的 public 与 hidden 边界

`/home/user/workspace` 是 replay + completion 的 public workspace；`/tests`、`/solution`、`/hidden_control`、`.git` 和 verifier 目录属于 hidden control，不上传给 completion agent。RED calibration 必须在同一个初始 workspace 上先执行，再允许 solver rollout；solver 只能看到 task 与 public workspace，不能看到 `test_outputs.py`、gold solution 或 calibration 结果。

### 最小验收记录

每次容器阶段至少保存：runtime/sandbox id、输入 workspace hash、上传/下载路径、agent/judge 原始 JSON、逐文件变更、证据引用、写入探针结果、missing/protective 测试结果、cleanup audit。只有 `READY` 且上述字段完整的 candidate 才能进入后续 workflow。
