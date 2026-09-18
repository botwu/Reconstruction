# 重建筛选 Rubric（已冻结）

状态：**FROZEN** `reconstruction-screening-v10` / `reconstruction-screening-triage-v10`
冻结日期：2026-09-15
权威实现：`src/traceforge/screening/rubric.py`、`model_triage.py`、`rules.py`
校准基线：R01 前 50 条，DeepSeek，concurrency=8；现行持久化模型为 `bailian/deepseek-v4-flash-0731`。v10 增加任务标签和关系校验。

模型只打分和分类，终态由规则 + 本门禁决定。未做新一轮校准前，不要改 R1/R2 含义、硬门槛或「任一 task 过线」规则。


## ELIGIBLE 是什么

ELIGIBLE = 这条 capture 里**至少有一个有效用户任务没有做好**，值得送进重建。

它不是「已经重建完」，也不是「Harbor 包已就绪」。下游只消费 ELIGIBLE。


## 第一性原理

重建是拟合用户意图、还原当时环境。筛选的主判断只有两句：

1. **用户 query 是不是一个有效任务**
2. **这个任务有没有完成，或完成得好不好**

工具调用是辅助：有的话用来看环境和过程；没有本该有的工具，本身也是没做好，**同样要重建**。不要因为轨迹里没有 read/write/edit/`code_file` 就把有效任务丢掉。


## 筛选对象

一行 JSONL = 一次 capture。在整条 capture 里找上述任务，不是只评最后一个 USER。

`leaf_response_status=completed` **不是**任务成功。有回复但对不准任务 = 完成不好，不要标 SUCCESS。


## 主判断（硬门槛）

| 特征 | 怎么判定 | 过线 |
|------|----------|------|
| 有效任务 | `task_identifiability`（R1） | ≥2：目标基本明确 |
| 没完成或完成不好 | `outcome` 为 FAILURE/INCOMPLETE，且 `failure_evidence`（R2） | ≥2 |

任一 task 过线，整条 ELIGIBLE，并记录 `selected_span_ids`。闲聊、不是任务的寒暄：R1 不够，不过线。已经做好：SUCCESS，整条都成功才 REJECT。


## 辅助：工具与环境

对着真实工具名和参数认，不写死名字。R01 里大量是 `exec`/`wait`，不是 Claude 的 read/write/edit。

| 看到什么 | 辅助标签 | 缺了会怎样 |
|----------|----------|------------|
| read/write/edit/glob/grep，或 exec/bash 在读改文件、跑本地命令 | `code_file` | 用户要改工作区却没这些调用 → 仍入选 |
| web_search / url_fetch / chat_history_get | `retrieval` | 该检索却没搜 → 仍入选 |
| wait | 配对，不定领域 | — |

`domain_route` 只记录，**不挡入选**。


## R2 没完成或完成不好

R2=0 只给「任务已经做好、过程正常结束」。

下面都打 ≥2：

- 环境问题（缺文件、缺依赖、缺组件、凭证/权限）
- 模型没做好（答错、卡住、没交交付物、用户纠正）
- **该用的工具没用**
- 截断、pending、轨迹不合理

不要求必须看到编译错误。


## 分数（各 0–3）

| 键 | 编号 | 0 | 2 | 3 |
|----|------|---|---|---|
| `task_identifiability` | R1 | 无任务 | 目标基本明确 | 目标、交付物都有用户证据 |
| `failure_evidence` | R2 | 已做好 | 没完成或完成不好 | 边界可定位 |


## 两层筛选

1. **规则粗筛**：坏 JSON / 无用户 / 无尝试 → REJECT；消息数 >200 或 `source_request_count` >20 → DEFER；其余 REVIEW。规则不能 ELIGIBLE。
2. **模型细筛**：整条可观察轨迹（`shared_context` + 全部 user span，thinking 已剥）→ `tasks[]`。模型不能推翻规则硬拒绝。


## 入选门禁

- 规则 REJECT / DEFER 仍优先
- 模型失败、`tasks` 空或非法 → REVIEW
- 至少一个 task：`FAILURE`/`INCOMPLETE`、`needs_reconstruction=true`、R1≥2、R2≥2 → **ELIGIBLE**
- 全部 SUCCESS → REJECT
- 其余 → REVIEW

有工作区工具时 route 为 `ELIGIBLE_CODE_FILE`，否则 `ELIGIBLE_TASK`。检索不再只因为领域而 DEFER。


## 入口与产物

```text
PYTHONPATH=src python -m traceforge screening run \
  --input return_data/four_batch/by-rubric/R01.jsonl \
  --output artifacts/screening \
  --channel deepseek \
  --concurrency 8
```

`--channel deepseek` 的默认模型已固化为 `bailian/deepseek-v4-flash-0731`。channel JSON 里若有 `model` / `model_name` 则覆盖默认值；不要再发 `vol/deepseek-v4-flash-0731`。

产物：`selection_manifest.json`（含 `eligible_source_refs`）+ `private/records.jsonl`。
每条 ELIGIBLE 带 `source_ref`、`line_number`、`line_sha256`、`selected_span_ids`、`triage.tasks[]`。

## v10 任务标签契约

筛选仍以完整 capture 为证据，`tasks[]` 是可追溯的派生标签。每个 task 必须包含：

- 稳定 `task_id`（由 `source_ref` 和有序 `span_ids` 确定性生成）；
- `is_actionable`、`outcome`、`needs_reconstruction`；
- `evidence_refs`（`message_indices` 和/或 `span_ids`）；
- `reconstruction_eligible` 与 `eligibility`（逐任务门禁结果）；
- 规则派生的 `tags[]`（模型不能写）。过线必须同时有 `selected_for_reconstruction` + `rubric_pass`。
  fail-closed / REVIEW 也要打 `rubric_review` 或 `rubric_reject`。
## session_tags 的作用（冻结）

`triage.session_tags` 描述**整条 capture**，不是再打一遍 R1/R2。

| 它做什么 | 它不做什么 |
|----------|------------|
| 索引：按 `screening_eligible` / `multiple_tasks` / `has_*` 找批次 | **不**决定 ELIGIBLE / REVIEW / DEFER / REJECT（那是 `decision`） |
| 给 Intent 看会话形态：多任务、有延续/纠错/依赖、混有闲聊 | **不**挑选重建哪条 task（那是 task 上的 `selected_for_reconstruction`） |
| 落进 `reconstruction_source.session_tags`，Completion 分流只作审计 | **不**分流 `TERMINAL_FILE` / retrieval（那是 `domain_route` + 回放树） |

| tag | 含义 | 下游怎么用 |
|-----|------|------------|
| `screening_eligible` / `_review` / `_defer` / `_reject` | 本条筛选终态的镜像 | 检索；与 `decision` 对照，不能单独开门 |
| `contains_reconstruction_candidate` + `needs_reconstruction` | 至少有一个过线 task | 检索「有候选」的 session |
| `contains_unfinished_task` / `contains_success_task` / `contains_non_actionable` | 会话里有未完成 / 已成功 / 闲聊 | Intent 知道不要把闲聊或已成功问当成当前 q |
| `multiple_tasks` | 多于一个 task | Intent **禁止合并**其它 task |
| `has_continuation` / `has_correction` / `has_dependency` | 已校验的关系类型 | Intent 只在本 task 的 span 里收澄清；dependency 不抑制前置任务 |

task `tags[]` 才是入选词汇（`selected_for_reconstruction`、`rubric_pass` / `rubric_review` / `rubric_reject`）。

下游接线：

- `reconstruct source` 重算并校验 `session_tags`，只选 task 上同时有 `reconstruction_eligible` 和 `selected_for_reconstruction` 的项；
- Intent 读 `SESSION_TAGS` 作上下文；
- `execution_support_route` 只记录，不用 session tag 改路线。

每个 span 只能归属一个 task，所有 span 都必须归属某个 task。`relations[]` 只允许
`continuation`、`correction`、`dependency`，关系必须有两端证据、按消息顺序向前，不能自环或重复。
同一目标的澄清和纠错应尽量在模型标签阶段合并；若被分成多个 task，后续有证据的
`continuation/correction` 成功会抑制此前失败 attempt 的 `reconstruction_eligible`。
`dependency` 不会抑制前置任务。关系缺证据、span 重叠、旧 v9 记录缺详细标签时，
记录标为 `LEGACY_INCOMPLETE` 或 `INVALID` 并进入 REVIEW，禁止默默认合并或 ELIGIBLE。

模型输入的 `shared_context + spans[]` 保留完整可观察 session。超出输入预算只标记
`serialization.oversized=true`，不机械截断，避免丢失跨 span 的失败和关系证据；成本路由
由规则层单独处理。


## 校准结论（冻结依据）

R01-50 / DeepSeek：ELIGIBLE 35，REVIEW 7，REJECT 5，DEFER 3。

人工核过 REVIEW/DEFER 后：

- **尺本身对齐第一性原理。** 入选的是「有效任务且没做好」，不是「必须已有工具/code_file」。
- REVIEW ≠ 不可重建。7 条里多数是通道空响应或 span_id 被截短；其中 injector / 文献综述 / 目录介绍等本应 ELIGIBLE。
- DEFER ≠ 不可重建。过长规则会跳过仍可能未完成的任务（例如同一 publish/recapture 家族里，短的已 ELIGIBLE、长的被 COST_DEFER）。
- 已做好的闲聊/完成任务被 REJECT 是对的。

因此：**不为调 rubric 重跑全量 50。** 漏网是通道和规则层操作问题，不回改 R1/R2。


## 明确不改（冻结期内）

- 不再加回 R3–R8，也不把工具/code_file/retrieval 做成硬门槛
- 不回到 last-span-only 或 last-2-turn 投影
- 不把 REVIEW/DEFER 当「已证明不可重建」
- 全量 R01（368）或 R08 筛选可另开批次，但仍用本尺，不先改尺


## 操作层已知缺口（不改尺，重建期也不先修）

| 现象 | 处理 |
|------|------|
| `EMPTY_RESPONSE` | 记 REVIEW，可事后单条重试 |
| 模型截短 `span_id` | 记 REVIEW（`TASK_SPAN_ID_UNKNOWN`） |
| 消息 >200 或请求 >20 | 规则 DEFER，留给抽样，不进自动重建 |

重建只吃 ELIGIBLE。这三类不阻塞下一阶段。
