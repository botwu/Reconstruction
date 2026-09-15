# TraceForge 模块机制与实现审查图

更新时间：2026-09-12

本文档供后续**逐模块审查和修改**使用。交接边界见 `docs/agent-world-reconstruction-handoff.md`。本文只描述：整体目标、每个模块做什么、怎么做、接到哪、审查时先看什么。

审查原则：以 `src/` 与 `tests/` 为准；有文件不等于已接入闭环；模型只能提候选，规则系统做门禁。


## 一、整体目标

把真实 Agent 失败/未完成轨迹，重建成：

```text
可审计事实 → 重述 Task → 不泄漏答案的初始 Environment
  → 独立 Verifier → Harbor Bundle → 对照 rollout → SFT 筛选
```

对齐 Terminal-Universe 三阶段，输出不能混用：

1. **Stage1 规则回放**：只恢复首次完整 read；不执行 shell；不落盘 mutation。
2. **Stage2 模型补全**：只补证据支持的缺失/PARTIAL。
3. **Stage3 只读充分性**：UNKNOWN 必须 REVIEW。

两条入口互不自动串联：

- 分析链：`trajectory compile` → lineage / query-turns / source-projection / failure-analysis
- 闭环链：人工准备 report + 带正文 evidence + replay → `reconstruct workflow`


## 二、建议审查顺序

按数据流向，一次只改一个模块：

```text
1. trajectory（事实底座）
2. evidence_join（重建输入投影）
3. trajectory_replay（Stage1 交付回放）
4. failure_analysis（选样本，不进闭环）
5. model_gateway（统一模型口）
6. semantic_recovery + task_recovery（Task）
7. environment_completion（Stage2 实际路径）
8. sufficiency_judge（Stage3 实际路径）
9. verifier.synthesis + bundle + grading + red_check
10. harbor_ags
11. workflow（编排，不承载算法）
12. curation.sft
13. 未接线层：terminal_universe_environment / iterative / control_plane / requery
```


## 三、事实层

### 3.1 `trajectory` — 编译只读 JSONL 为不可变事件

**目标**：一条 JSONL ≈ 一个 capture，产出可重算的结构化事实。

**机制**：

1. `source.scan_jsonl_source` 两遍扫描：整文件 hash + 逐行 `SourceRecordRef`。
2. `source_adapter.adapt_source_record` 把 R01 / restored-long 收成统一 envelope。
3. `compiler.compile_capture`：request boundary → tool catalog → events → tool pairings → quality。
4. `pipeline.compile_trajectory` 原子发布 artifact；失败行 quarantine，不伪造 capture。

**关键实现**：

| 文件 | 职责 |
|------|------|
| `trajectory/source.py` | JSONL 扫描与复核 |
| `trajectory/source_adapter.py` | schema 适配 |
| `trajectory/compiler.py` | 单 capture 编译内核 |
| `trajectory/contracts.py` | M1B 契约；`COMPILER_CONTRACT_VERSION` |
| `trajectory/pipeline.py` | 编排与发布 |
| `trajectory/validation.py` | 已发布 run 校验 |
| `trajectory/artifacts.py` | 原子 publish、canonical JSONL |
| `trajectory/json_codec.py` | `stable_id`、严格 JSON |
| `trajectory/privacy.py` | 派生策略 / 敏感内容门禁 |

**不变量**：`RESULT_NOT_OBSERVED` ≠ 工具失败；业务 ID 由 `stable_id` 决定，与输出路径无关。

**审查重点**：pairing 是否 1:1；quarantine 是否不写假 capture；隐私是否在编译时生效。

### 3.2 `lineage` — M1C 请求关系图

**目标**：在已发布 M1B 上只读派生 request/capture 关系。

**机制**：`lineage/builder.py` 从可见事实建边；`pipeline.build_lineage` 发布。Grade-A 边不读组 ID 猜关系。

**接入**：独立 CLI `lineage build`。workflow **不读** lineage。

**审查重点**：是否只依赖 M1B 已发布事实；是否把不透明 hash 当证据。

### 3.3 `query_turns` — M1D QueryTurn

**目标**：以连续 USER 为根切结构回合。

**机制**：`query_turns/builder.py` 建 UserBlock / AgentStep / QueryTurn；未配对 result 进 orphan，未观测 call 进 unresolved。

**接入**：独立 CLI `query-turns build`。可绑到 M4 mapping；**不驱动** Task prompt。

**审查重点**：TOOL_RESULT 是否按 pairing 归属，而不是按到达顺序猜。

### 3.4 `source_projection` — USER 文本结构注解

**目标**：给 USER 事件打结构注解，不复制正文到公共 annotation。

**机制**：`source_projection/builder.py` + `pipeline.build_user_text_projection`。

**接入**：独立 CLI。workflow **不读**。

### 3.5 `trajectory.evidence_join` — 重建用证据投影

**目标**：从 M1B 事件抽出带 `text`/`phase` 的 Task 输入。

**机制**：

- `build_query_task_input`：按 USER 序号切 turn（默认最后一问）。
- `build_capture_input`：整 capture 截断。
- `build_task_input`：按已有 `source_id` 列表 join。
- `_phase`：目标前 → `PRE_TASK_CONTEXT`；目标 USER → `TARGET_REQUEST`；`TOOL_CALL` → `ATTEMPT_ACTION`；`TOOL_RESULT` → `ATTEMPT_OBSERVATION`；助手 → `ATTEMPT_RESPONSE`。

**产物 schema**：`traceforge.task-reconstruction-input.v2`。

**审查重点**：pending tool 不得标失败；无 `text` 的 M4 index 不能当本模块输出。


## 四、Stage1 回放（两套实现）

### 4.1 `trajectory_replay` — 交付用，已接 workflow

**目标**：物化任务开始前的公开 workspace。

**机制**（`_replay_capture`）：

1. 完整 read 的 TOOL_RESULT 首次写入 `observed`。
2. 带 offset/limit 的 read → `partial_read_range`。
3. write/edit/create → `withheld_changes`，不落盘。
4. shell/exec/terminal → `unknown_mutation_barriers`，不执行。
5. 已被 mutation 的已观测文件 → `modified_after_observation` / PARTIAL。

**接入**：Python API `normalized_run_dir` → `build_trajectory_replay`。CLI `trajectory-replay` 独立；`reconstruct workflow` CLI **没有** `--normalized-run`。

**已知差距**：`mutation_started` 从未置 True，`read_after_first_mutation` 是死分支。

**审查重点**：是否应与 `terminal_universe_environment.replay_events` 对齐首次 mutation 后不再倒灌。

### 4.2 `terminal_universe_environment` — 更严原语，未接线

**目标**：论文对齐的 Stage1/2/3 原语库。

**机制**：`replay_initial_workspace` 会在 write/edit/delete/shell 时设 `mutation_started=True`；`materialize_environment` 写 `workspace/` + `hidden_control/withheld_changes.json`；`select_sufficient_candidate` 与 workflow 选型等价。

**接入**：仅测试。workflow 使用 `environment_completion`，**未 import** 本模块。

**审查重点**：是替换交付 replay，还是只保留对照测试。不要两套规则长期分叉。


## 五、失败分析（选样本，不自动进闭环）

### 5.1 `failure_analysis` 确定性层（M4）

**目标**：对 M1B 做结构不变量，产出 report + **无正文** evidence index。

**机制**：`reader.load_failure_input` → `extractor.analyze_capture` → `pipeline.build_failure_analysis`。

检查包括：事件单调唯一、pairing 1:1、terminal 是否观测到。无 FAIL → `primary_failure=INCONCLUSIVE`。

**产物**：`failure_analysis.jsonl`、`evidence_refs.jsonl`（`content_sha256` 常为空）。

### 5.2 `model_analyzer` / `model_runner`

**目标**：在已有 report+evidence 上判断 outcome、重建价值、decision。

**机制**：`analyze_failure` 调 `ChatModel`；未知证据或状态矛盾 → REVIEW。

### 5.3 AgentRx / TRACE / review_batch

- AgentRx：normalize → static → dynamic → check（Python 标 `NEEDS_SANDBOX`）→ judge。
- TRACE：六列表；无 SUCCESS/FAILURE 对照则 metrics 必须 unavailable。
- review_batch：JSONL 去重排序 Top-N，`PENDING_HUMAN_REVIEW`。

**审查重点**：不要把这些入口误接成 workflow 前置；M4 index 不能当 Task evidence。


## 六、模型网关

### 6.1 `reconstruction/model_gateway.py`

**目标**：唯一模型协议 `ChatModel.complete(ModelRequest) -> ModelResponse`。

**机制**：

- 有 `config.yaml` → `NewAPIClient`（OpenAI-compatible）。
- 无 config → `OpusClient`（Anthropic Messages）。
- `resolve_model_name`：避免把 `claude-*` 发到 Gemini/DeepSeek channel。

**审查重点**：密钥不落 artifact；Hermes 必须 `provider/model`，不能直传 `vol/deepseek-*`。


## 七、重建语义层（workflow 实际路径）

### 7.1 `semantic_recovery` + `task_recovery`

**目标**：重述用户任务、义务、约束、歧义。

**机制**：

1. `_evidence_projection` 优先保留 TARGET / ATTEMPT_*，上下文可裁剪。
2. 模型返回最多 3 候选；`_evidence` 用输入权威 metadata 覆盖模型杜撰。
3. 来源质量差则即使候选全 READY，run status 也变 `REVIEW`（`SOURCE_QUALITY_REVIEW_REQUIRED`）。

**触发 REVIEW 的来源质量**（代码原样）：

- report `INCONCLUSIVE` / `REVIEW`
- `quality.requires_review`
- `pending_tool_call_count > 0`
- `provenance.snapshot`
- **任意 evidence `phase == ATTEMPT_ACTION`**

**接入**：`run_task_recovery` → workflow 要求 `status==COMPLETE` 且存在 READY 候选。

**审查重点（优先改）**：`ATTEMPT_ACTION` 会使所有带工具调用的真实轨迹无法过 COMPLETE 门禁。`evidence_join` 对 TOOL_CALL 正好打这个 phase。产品语义需要先定：是 bug，还是“有工具尝试就必须人审”。

### 7.2 `environment_completion`

**目标**：Stage2 补全环境，物化最多 5 个候选 workspace。

**机制**：

- Prompt：只补 PARTIAL/缺失；禁止改 COMPLETE/UNKNOWN。
- `_safe_path`：拒 `..`、绝对路径、`solution`/`tests`/`.git`/`hidden_control`。
- `_materialize`：只写索引过的 replay 路径 + 模型新文件；不 copytree 未索引文件。
- 模型失败 → FAILED artifact，不抛成“成功”。

**接入**：workflow 唯一环境路径。

**审查重点**：COMPLETE 保护、泄漏路径、evidence_ref 闭包、与 `terminal_universe_environment.validate_completion_candidate` 是否重复且不一致。

### 7.3 `sufficiency_judge`（实际）与 `sufficiency.py`（未用）

**目标**：只读判断“可解但未解”。

**机制**（judge）：模型返回 `label` + `decision`；`UNKNOWN` / 缺 evidence → 强制 REVIEW。

**选型**（在 workflow 内联，不调用 `select_sufficient_candidate`）：

```text
READY 环境候选 + SUFFICIENT + READY
排序：confidence 降序，uncertainty 升序，index 升序
```

**已知差距**：`parse_json_object` 的网关错误会降 REVIEW；`model.complete()` 本身抛错会打穿 workflow。

**审查重点**：两套 sufficiency 契约不要并存误用；本地目录读取 ≠ 容器内只读审查。


## 八、Verifier / Bundle / RED

### 8.1 `verifier/synthesis.py`

**目标**：生成隐藏 pytest、≥2 oracle、≥1 mutation。

**机制**：AST 解析 `test_*`；`obligation_coverage` 必须覆盖全部义务 id；script 不得含 `/tests` `/solution`；产物 `status=UNVALIDATED`。不在宿主机执行模型 Python。

**接入**：workflow 只调这一次合成。

### 8.2 `verifier/iterative.py`（未接线）

**目标**：生成 → `VerifierExecutor.run` → PASS/FAIL/INFRA_ERROR → 最多 3 轮再生成。

**接入**：仅测试替身。无 Harbor executor。

**审查重点**：接入时应把候选测试拷进 verifier sandbox，回传有界 stdout/stderr；禁止宿主机 exec。

### 8.3 `verifier/bundle.py` + `grading.py`

**目标**：编 Harbor 1.4 bundle；grader 在 verifier 沙盒跑 pytest。

**机制**：Agent 可见 `instruction.md` + `workspace/`；隐藏 `solution/` `tests/` `tests/control/`。`tests/test.sh` 内容固定。`grade()` 在 INFRA 时删除 reward，避免 Harbor 把基础设施错误当成 0 分失败。

### 8.4 `verifier/red_check.py`

**目标**：审计三类对照，不负责开 Harbor。

**机制**：必须同时有 `oracle_pass`（PASS/1）、`nop_fail`（FAIL/0）、`mutation_fail`（FAIL/0）。workflow 在 `quality_gate` 失败时把 trial 标 `INFRA_ERROR`。


## 九、Harbor / SFT / 编排

### 9.1 `harbor_ags`

| 文件 | 职责 |
|------|------|
| `adapter.py` | bundle 布局、可见/隐藏边界、可选调用兄弟仓 `validate_task_bundle` |
| `rollout.py` | 复制 dataset、冻结构 config、`PLAN_ONLY`；`execute_rollout_plan` 才 subprocess Harbor |
| `results.py` | 读 trial / verdict / trajectory / cleanup；算 `quality_gate` |

**凭据**：只检查 env 名是否存在，不写入 plan。

### 9.2 `curation/sft.py` 与 `requery/sft_export.py`

**筛选阈值**：task/env/trajectory ≥ 0.8；reward ≥ 1.0；verifier PASS；泄漏/可复现必须是显式布尔。

**导出**：只接受 `ELIGIBLE`。workflow **只 curate、不 export**。仓库内无论文标准 JSONL。

### 9.3 `reconstruction/workflow.py`

**目标**：单条 attempt 的意见化编排，不承载领域算法。

**顺序**：

```text
replay(可选) → canonicalize_evidence → task → env → 逐候选 sufficiency
  → synthesize_verifier → compile_bundle×3 → rollout plan×4
  → [execute_rollout] RED + curate → 发布 result/
```

**停止**：Task 非 COMPLETE、无 READY+SUFFICIENT、verifier 为 None、rollout_model 非法。

### 9.4 `reconstruction/pipeline.py`

批量 M4 + 执行计划，节点多为 `PENDING_MODEL`。不调模型、不跑 Harbor。

### 9.5 `reconstruction/control_plane.py`（未接线）

固定阶段顺序与 PASS/REVIEW/RETRY/FAIL/BLOCKED。workflow 零引用。接入时应在阶段边界写不可变 gate，禁止 try/except 绕过。

### 9.6 `requery`

- `cross_workspace`：profile / 方向 gap / 只读挂载提示；无 TF-IDF、无 LLM judge、无双 mount。
- `multi_round`：requirement tracker、follow-up prompt、≥2 PASS 才保留；无 user agent、无持久 Harbor workspace。


## 十、CLI 对照

| 命令 | 模块 | 是否进 workflow |
|------|------|-----------------|
| `trajectory compile` | trajectory | 否，只产 M1B |
| `trajectory-replay` | trajectory_replay | 否，产物可喂 CLI workflow |
| `lineage / query-turns / source-projection build` | 派生层 | 否 |
| `failure-analysis *` | M4 / judge / AgentRx / TRACE / batch | 否 |
| `reconstruct pipeline` | reconstruction.pipeline | 否 |
| `reconstruct workflow` | workflow | 是，强制 replay-workspace + files |
| Harbor 子命令 | harbor_ags | 计划/读结果，不替代 workflow |


## 十一、当前最值得先改的实现问题

按会阻断真实闭环的程度：

1. **Task `ATTEMPT_ACTION` → SOURCE_QUALITY_REVIEW**：带工具调用的轨迹无法 `COMPLETE`。
2. **交付 replay 的 `mutation_started` 死分支**：与论文 Stage1 / `terminal_universe_environment` 不一致。
3. **CLI 无 `--normalized-run`**：生产入口与 Python API 分裂。
4. **sufficiency `model.complete()` 未捕获**：网关失败打穿编排。
5. **workflow 跳过 iterative**：verifier 只有静态 UNVALIDATED。
6. **两套环境回放/校验并存**：长期会漂移。

审查时一次只处理上表一项，先补失败测试再改行为。


## 十二、不在本次清理范围内、也不当“无用代码”删除的模块

以下是已实现未接线或研究适配器，后续审查决定接或不接，**不要当死代码删**：

- `terminal_universe_environment.py`
- `verifier/iterative.py`
- `control_plane.py` / `control_plane_contracts.py`
- `sufficiency.py`
- `requery/*`
- `failure_analysis/agentrx_*`、`trace_capabilities.py`
- `third_party/` 许可证与参考 prompt
