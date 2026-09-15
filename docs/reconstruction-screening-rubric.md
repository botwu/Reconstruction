# 重建筛选 Rubric

更新时间：2026-09-15

权威实现：`src/traceforge/screening/rubric.py`。模型只打分和分类，终态由规则 + 本门禁决定。

重建要做的是拟合用户意图、还原环境。筛选先用下面三条；之后用真实 session 再细化。


## 筛选对象

一行 JSONL = 一次 capture。**在整条 capture 里召回失败/未完成、能重建的任务**，不是只评最后一个 USER。

`leaf_response_status=completed` **不是**任务成功。


## 三条入选特征

| 特征 | 怎么判定 | 过线 |
|------|----------|------|
| 意图能拟合 | `task_identifiability`（R1） | ≥2：目标基本明确 |
| 没做成 | `outcome` 为 FAILURE/INCOMPLETE，且 `failure_evidence`（R2） | ≥2：有错误、终止或纠正 |
| 环境有抓手 | `domain_route` | `code_file`：出现过文件/代码/工作区操作，不要求环境完整 |

检索问答 → DEFER。闲聊或纯概念 → 该 task 不过线。


## 分数（各 0–3）

| 键 | 编号 | 0 | 2 | 3 |
|----|------|---|---|---|
| `task_identifiability` | R1 | 无任务 | 目标基本明确 | 目标、交付物都有用户证据 |
| `failure_evidence` | R2 | 无失败/未完成 | 有错误、终止或纠正 | 失败边界可定位 |

不再打 R3/R4/R7。环境完整度、补环境、隐私、测试可验证都不在筛选层。


## 两层筛选

1. **规则粗筛**（必跑，不调模型）
   - REJECT：无法解析、无用户任务、无 Agent 尝试
   - DEFER：消息数 > 200 或 `source_request_count > 20`
   - REVIEW：其余，交给模型
   - 规则**不能**给出 ELIGIBLE

2. **模型细筛**（只对规则 REVIEW）
   - 输入是可观察轨迹：`shared_context` + **全部** user-span
   - 输出 `tasks[]`：`span_ids`、`outcome`、`needs_reconstruction`、`domain_route`、rubric
   - 模型不能推翻规则硬拒绝
   - 终态仍是一行一条决策；过线任务的 `selected_span_ids` 写入记录


## 入选门禁

偏召回：**任一任务**过线即可整条 ELIGIBLE。

- 规则 REJECT → REJECT
- 规则 DEFER → DEFER
- 模型失败、`tasks` 非法或空、未知 `span_id` → REVIEW
- 至少一个 task 满足：`FAILURE`/`INCOMPLETE`、`needs_reconstruction=true`、R1≥2、R2≥2、`code_file` → **ELIGIBLE**
- 全部 task 为 `SUCCESS` → REJECT
- 仅有 retrieval 可重建、无 code_file → DEFER
- 其余 → REVIEW

下游重建只吃 ELIGIBLE。
