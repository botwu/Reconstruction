# M1 实现来源与迁移记录

版本：v1.0

日期：2026-08-31

本文记录 M1A、M1B 对历史 `seed2traj` 轨迹审核代码的使用边界。它是代码来源审计记录，不替代 [`r01-processing-spec.md`](r01-processing-spec.md) 的业务契约。

结论先行：旧 `search_audit` 不是 TraceForge M1 的上游管线、运行时依赖或标签来源。M1 只参考其中 canonical artifact、显式消息位置/tool ID、typed observation 和回归测试组织等通用底层思路；R01 结构契约、状态机、稳定 ID、事件图和 validator 均按 TraceForge 契约独立实现。

## 1. 来源基线

历史实现位于：

```text
/Users/wujian1/Downloads/seed2traj
```

该目录不是可解析提交版本的 Git 工作树，根目录也没有许可证文件。因此 TraceForge 不逐字复制其代码，不把它作为运行时依赖；采用方式统一为 `PRINCIPLE_ONLY`：依据内容摘要做只读审计，再按新契约独立重写。来源版本以同步清单及逐文件 SHA-256 为准：

| 来源文件 | SHA-256 | 只读审计点 |
|---|---|---|
| `results/search_rubric/local_sync_manifest_20260826.json` | `72cc7e23ecf3d22fdf42d496070122924bb9efb128ae69350f1ad8bdc4899c83` | 来源清单与历史验收事实 |
| `src/seed2traj/continual/raw.py` | `7e9b480fe223bbdae5d0eba21ca9c56447dfcf0682d7d66601cdde49c22b7859` | 二进制物理行、offset、digest、stat 与原子写模式 |
| `src/seed2traj/search_audit/contracts.py` | `0e8c94b3f1994b16dc74573063ce53ee522252d364e6fd6d3d6a170e1833bf09` | canonical JSON 与 artifact 组织的一般模式 |
| `src/seed2traj/search_audit/trace.py` | `f29585ff8cea961e75ef7223ae928db6b91389ece739afa50c2bf1ff3a1484fe` | 显式 tool ID、消息位置与 typed observation 的一般价值 |
| `src/seed2traj/search_audit/pipeline.py` | `b6e8f4f76985e6be987318e48724a0a8d4caeb292504381dae4accbcbb2fe14c` | 分阶段 artifact 和失败终态的一般模式 |
| `scripts/run_search_audit.py` | `1029d8ff0333ca58b9bf2e80bc80abe5675240e698f31a685381349dffc68ea4` | CLI 与审核逻辑分责的一般模式 |
| `scripts/test_raw_lineage.py` | `b6502be655110be08f1154d62ffc1c7f9f83cbf2a6bf1058fa9d520df346cb1d` | 来源账本回归测试思路 |
| `scripts/test_search_audit.py` | `de1a2f85f748ef6fc64deb194e03f5d6c921189ff548eb2d48abe32a36487958` | 消息与工具结构回归测试思路 |

同步清单本身说明：权威实现当时未进入 Git，`commit_anchor=null`，逐文件 SHA-256 是版本依据。若后续找到带许可证和提交锚点的正式来源，应新增记录，不覆盖本次事实。

## 2. 已采用的实现思想

### 2.1 来源账本

TraceForge 在 `trajectory/source.py` 独立重写了：

- 二进制逐行扫描；
- 1-based 物理行号、byte offset、byte length 和 line SHA-256；
- 读取前后 stat 对比；
- 原始数据集 SHA-256；
- 单行解析错误不丢来源。

本地改动不是兼容层，而是新契约实现：

- 绝对路径不进入业务 ID；
- source ID 同时绑定 dataset ID、dataset digest、行号和行 digest；
- 第一遍与第二遍之间再次校验 stat；
- 第二遍逐行生成 offset、length 和 digest 账本，同时复核整文件 digest、字节数和行数；
- 严格拒绝重复 JSON key、非有限数、过长整数、未配对 Unicode surrogate 和非 UTF-8 输入；
- 物理 `jsonl` 与语义 `source_schema` 分离，由 `trajectory/source_adapter.py` 的单一 restored-long v1 adapter 进行显式门控；
- `source_schema` 进入 source/artifact manifest 和内容寻址 run ID，不对任意 JSON 做格式猜测。

### 2.2 Artifact 发布

TraceForge 在 `trajectory/artifacts.py` 独立重写了临时文件、`fsync` 和原子替换模式，并增加：

- canonical UTF-8 JSON/JSONL；
- 每个文件的 digest、字节数和记录数；
- 同文件系统 staging run；
- 整个 run 的一次性原子发布；
- 已存在内容寻址 run 拒绝覆盖；
- 非确定性 `run_receipt.json` 与业务清单分离。

### 2.3 可见事件与工具关系

旧 `search_audit/trace.py` 说明显式 `tool_call_id`、消息 index 和 typed result 对审核有用。TraceForge 只采用这些与搜索 rubric 无关的事实层思想，重新实现了 occurrence-preserving 图契约：

- assistant decision 与 tool call 拆为不同事件；
- 同一 decision 的 calls 进入 `ActionBatch`；
- call/result 按显式 ID 分组，不按相邻位置猜测；
- 重复 call 和重复 result 全部保留，不用字典覆盖；
- list content block 不转成字符串；
- reasoning 原文不进入派生产物；
- Base64 Data URL 只保留长度、摘要、mime 和来源 pointer。

## 3. 明确没有迁移的内容

以下历史内容不进入 M1 运行时：

- `rubric/search_rubric.json`；
- `rubric/search_stopping_rubric_v1.json`；
- `results/search_rubric/human_review_assisted_v1/human_reviews_v1.jsonl`；
- 所有 rubric score、search stopping 结论和人工 review 标签；
- LLM judge、badcase verdict、人工复核覆盖、resume 和并发模型调用；
- 搜索专属 URL、query retry、claim、citation 和 stopping 判断；
- 旧 `record_id` 假设、单条记录 valid/badcase 状态和历史 pipeline 状态机；
- 旧代码根据 reasoning 原文生成审核视图的逻辑。

原因是这些对象回答“轨迹是否是搜索 badcase”，而当前 M1 只回答“来源中明确可见了哪些结构事实”。rubric、人工 review、badcase 和 LLM judge 既不进入 compiler 分支，也不进入 validator oracle；它们最多用于理解旧字段历史，不能成为 TraceForge 的输入标签、质量终态或 Ground Truth。

## 4. 许可证与依赖结论

历史 `seed2traj` 根目录没有发现许可证声明，本次也没有可用 Git commit。为避免来源不明代码进入新仓库：

- 没有逐字复制历史函数或测试；
- TraceForge 不导入 `seed2traj` 包；
- 新代码只依赖 Python 标准库；
- 历史路径和摘要只出现在本文，不进入业务 artifact；
- 若未来需要直接移植任何实现，必须先补齐许可证、提交版本、原文件和差异记录。
