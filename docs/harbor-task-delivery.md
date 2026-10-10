# Harbor 任务交付

重建交付的是原任务和任务开始前的环境。`domain` 由调用方提供，`harness` 用于理解原 session 的系统说明、工具协议和返回，不决定任务属于 search 还是 terminal。两种 domain 最终都可交付给原生 Harbor；无需为了复现输入而安装原会话使用的 Codex、Hermes、Claude Code 或 OpenClaw。

参考 AgenticFoundry `c91ab4f33f787b5901c3e2df1566f81bf531cfbd`（Apache-2.0）的 `agents/synth/instruction.md` 和 `harness/run_synth.py`：作者输出任务目录，后续 runner 从 `task.toml` 发现任务，再独立运行环境、求解和验证。本项目独立实现导出，未复制参考代码，也不引入其出题难度、创新性或数据筛选逻辑。

```text
task/
  task.toml
  instruction.md
  environment/
    Dockerfile
    docker-compose.yaml
    setup.sh
  workspace/
  tests/
  solution/                 # 已有参考解时提供
```

`task.toml` 当前使用原生 Harbor 可读取的 `schema_version = "1.4"`。参考仓库部分任务使用 `2.0`，版本字串不同不意味着格式不兼容；以实际 Harbor 加载及容器执行为准。

Compose 的构建上下文为任务根目录，agent 镜像只复制 `environment/` 和公开的 `workspace/`；独立 verifier 镜像只复制 `tests/`。不把完整原 session、隐藏验证器或参考解塞进 solver 工作区。当前镜像面向 Linux amd64；Python 版本匹配冻结二进制 wheel 的 CPython ABI（本次 terminal 为 3.11），无此约束时默认 3.12。其他解释器、语言或系统依赖仍须根据实际初态准备，不能据这一模板声称所有环境已支持。

## terminal

公开任务与环境使用共用导出层，评分包再附加隐藏测试和参考解。环境充分性与真实执行探针通过后，即可单独交付公开 Harbor 包；不需要先生成评分器。

`reconstruct raw-run --disable-verification --execute-rollout` 可直接收集未评分的真实求解，`scripts/run_session_batch.py` 传递同一选项。该模式不与 `--execute-red` 同用，包中不含 `tests/` 或 `solution/`；验收保持 `NOT_ASSESSED`，不产生 reward、通过率或 SFT 资格。原生入口为：

```bash
harbor run --path /path/to/public/task --agent <solver> --env <backend> \
  --disable-verification --yes
```

下文的 oracle 和独立 verifier 仅适用于包含已审查评分器的任务包。

原 bundle 编译器保留公开初态、冻结依赖、隐藏测试及参考解，同时生成原生容器定义和 workspace 采集 hook；原 AGS 执行路径继续使用原有 setup 和目录。独立 verifier 读取求解后的副本，保持无网络验证。已有旧 bundle 在发布阶段补上同一采集 hook。

```bash
harbor run --path /path/to/published/task --agent oracle --env docker
```

oracle 只用于校准参考解与验证器；实际 rollout 由下游配置求解 agent、模型和调用凭据。文件验证通过不代表自由文本分析通过，既有人工核查状态继续保留。

无网 verifier 需要执行后端支持相应隔离。本次本机 Docker 内核未通过 Harbor 的 nftables 能力探测，因此仅用其检查参考解执行与工作区采集；完整独立验证继续使用现有 Harbor/AGS 后端。不能将跳过验证的 Docker 运行当作验收通过。

跨机器搬运任务包时须保留文件权限，例如 `tar -xzpf tasks.tar.gz`；默认解包的 umask 可能使 AGS 中的普通用户失去工作区写权限。

## search

通过 v4 来源交接校验并输出 READY 后自动生成 `tasks/<task_id>/harbor/<digest>/task/`，`result.json` 返回 `harbor_task` 和 `harbor_rollout_args`。直接检索 rollout 与 Harbor 包使用同一批公开材料；该 runner 的实际答案和工具轨迹会返回持续 researcher 复核。原生 Harbor 的执行情况单独记录，不能把直接 runner 的结果说成原生 Harbor 已运行。

任务说明保留原用户要求及必要指代上下文，`workspace/evidence.json` 默认携带全部已返回记录与实际抓取资料，必要排除只限任务答案或解题后状态，必须给出事件索引、分类、逐字原文和原因；保留原文和文件坐标，隔离解析模型的分析意见。历史方案按原消息恢复，不使用自由生成的 context_note。旧检索片段、摘要或重复材料不能因为已有新来源而删除。分类与引文只提供可审查依据，不能代替语义核对；旧 v3 环境须重新补全后再续跑。补全 agent 根据原任务声明 `requires_live_web` 和依据；这不改变调用方指定的 domain。

`evidence-index.json` 为每份来源索引元数据 JSON 和正文阅读视图。直接抓取的 PDF 全部按哈希核验并交付原件，不以是否做过 OCR 为条件；索引的 `pdf_path` 和 `pdf_sha256` 绑定实际文件。缺失或损坏原件时导出失败；Reader 返回的文本与响应哈希不能冒充原 PDF。 直接取得的 JPEG/PNG 同样交付原件，索引的 `image_path`、`image_sha256` 和 `mime_type` 绑定实际文件；缺失、改动或格式不符时禁止导出。原生 solver 使用已配置的视觉工具读取原件；能够索引或下载图片不表示已完成视觉核对。完整原始内容仍在 `evidence.json`；原 PDF、页图和 OCR 返回保持不变。正文含 NUL 时，只有派生 txt 将其显示为 `␀`，避免文本检索误判为二进制；索引 `body_projection` 记录从 0 开始的 Unicode 码点位置与原正文 SHA256，可精确逆转，原有字面 `␀` 不变。正文行号不变，`body_sha256` 和字节数绑定实际视图；无 NUL 的正文不改写。

本地代码、文档检索通过 `search_evidence/read_evidence` 查询捕获语料；READY 前必须逐项说明原要求所需材料，并实际读取引用证据。Harbor solver 可用 Python 读取同一 `evidence.json`，保留当前文件与历史版本的区别。这类任务不安装网页检索脚本，也不要求 Serper/Jina 密钥。

需要公开来源的任务仍须实证完成搜索和页面读取。公开检索工具复用现有实现，在容器中执行：

```bash
traceforge-search search "查询内容"
traceforge-search open "https://example.org/source" --offset 0 --limit 8000
```

原始返回、抓取摘要和调用记录写入 `/logs/artifacts/search/`；不同命令进程的翻页复用第一次保存的正文。 原生轨迹读取器核对 Harbor 的目录回收记录，并将检索缓存逐文件 SHA256 加入同一证据清单。缓存说明实际回收了哪些来源，solver 实际看到的内容仍取完整原生工具返回：`python` 提取字段、`head` 截断及解析错误都原样保留。后审不要求这些终端输出仍是完整 JSON，也不能以缓存全文代替实际所见；缺少绑定或哈希不一致时仍停止后审。任务配置仅包含 `SERPER_API_KEY`、`JINA_API_KEY`、`TRACEFORGE_FETCH_PROVIDER` 的环境变量模板，实际凭据由运行方注入，不随任务交付。

目前 search 自由文本回答没有自动 verifier。按已约定的人工核查方式，使用 Harbor 的 `--disable-verification`，不伪造 `test.sh`、参考答案或成功奖励。`tests/README.md` 和任务外的 `delivery.json` 明确记录此限制。

```bash
# 只验证环境启动；nop 不生成答案，也不构成内容通过。
harbor run --path /path/to/search/task --agent nop --env docker \
  --disable-verification --yes
```

生产 rollout 将 `nop` 替换为已配置的求解 agent 和模型，并保留 `--disable-verification`。环境可查询与回答内容正确分别记录，不能以网页或本地工具调用成功代替内容核查。


### AGS 执行

检索依赖随交付包离线提供。项目固定 pypdf 6.19.0 与 fonttools 4.66.1 的官方
`py3-none-any` wheel，适用当前 AGS Python 3.11 与 Docker Python 3.12；
来源 URL、许可证和 SHA256 记录于
`src/traceforge/reconstruction/search_vendor_lock.json`，许可证原文保留在 wheel 内。
导出复用既有 Python runtime 的哈希锁；setup 以 `--no-index --require-hashes`
安装到任务环境的私有目录，避免系统 Python 限制和每个沙箱重复下载。

已部署的 `harbor_ags.environment:AGSPrebuiltEnvironment` 本身是原生 Harbor 后端，
直接创建云端预置沙盒，无需执行机安装 Docker。search 使用本仓库的
`traceforge.harbor_ags.search:SearchAGSEnvironment`，保留任务显式声明的检索变量，
继续剥离模型凭据；初始化脚本按实际上传目录定位，不依赖 Docker 中的安装路径。

search 的原生求解角色另部署一行标准 rg 配置，统一上下文行与匹配行的字段分隔符，
避免正文中的 `-数字-` 被 Hermes 误当路径或行号。配置生成随环境适配源码冻结；
上传失败直接终止。terminal 和 verifier 不启用该配置，原始文件、二进制判断、
文件名查询和错误报告保持不变。

在已配置的 Harbor/AGS 运行时目录执行，令 `SEARCH_TASK` 指向本次新导出的任务目录，
`HARBOR_JOBS` 指向本次结果目录。项目 `src` 必须在 `PYTHONPATH` 中：

```bash
.venv/bin/harbor run -c configs/hermes-batch.yaml -p "$SEARCH_TASK" \
  --env traceforge.harbor_ags.search:SearchAGSEnvironment \
  --disable-verification --n-concurrent 1 --n-concurrent-agents 1 \
  --ak max_iterations=80 --ek sandbox_timeout_sec=3600 \
  --ek request_timeout_sec=3600 --jobs-dir "$HARBOR_JOBS" --yes
```

运行方从私有配置向进程注入 AGS、模型以及任务声明的检索凭据；不要把凭据值写入命令、
任务包、模型提示或日志。环境变量是运行时传递方式，不提供对有终端权限的进程的密钥隔离。
模型 ID 与 `expected_commit` 必须匹配实际通道和 AGS 模板中的 Hermes 版本。
`--print-config` 只核对配置，不能替代实际创建、工具执行、回答、轨迹收集和沙盒清理。
正式入口使用 `traceforge harbor-ags prepare-rollout` / `traceforge harbor-ags execute-rollout`，复用冻结、执行和清理回执；
原始会话管线也调用同一执行层。计划根据显式 search metadata 和完整 `delivery.json`
哈希识别任务，自动选择此环境并禁用评分。无需生成 `solution/` 或 `tests/control/`。
上述直接 Harbor 命令仅供诊断。原生结果须完成请求/响应对账、任务输入绑定和沙盒清理，
才记录执行完成；没有 reward 的自由文本回答始终标记 `NOT_ASSESSED`，不能视为 PASS。


### 原生 rollout 的网关传输

正式 Hermes 计划使用本仓库 GatewayHermesAgent，原生 Harbor 的环境准备、输入、
工具、捕获、对账与清理继续复用。仓库内小入口加载原始 harness，禁止流式，并在
Messages SDK 请求边界保留完整网关 model 字面值，避免 Hermes 将网关路由当作
Anthropic 官方模型名再次归一化。adapter 和入口源码哈希绑定到计划；旧计划按其
原有 runtime 记录验证，不追溯添加新的约束。

HTTP 200 或最终回答存在不等于轨迹完整。中间响应缺失、残缺参数被 harness 修复后
实际执行的工具调用，仍保留原始证据与失败状态；此适配不放宽捕获认证。


### terminal 的行为与文件语义联合验收

候选验证器可用 `file_semantic_checks` 声明必须检查实际代码变更的 FILE 义务。
判据只进入隐藏 `tests/control/input-manifest.json` 的 `task_acceptance`，不放入
solver 指令或公开 workspace。没有此类判据的任务保持原有验收方式。

pytest 验证输入输出和保护行为，原始 reward 不变。重构是否真正转移业务、
入口是否消费新实现，由只读审查比较冻结初态与真实完成态判断。终态复用现有
`verifier.collect` 从主环境 `/home/user/workspace` 回收至
`artifacts/logs/artifacts/traceforge/workspace`；不是 verifier 临时工作区。

收集器仅排除本次新增、带真实 pyvenv.cfg 的 Python 虚拟环境和 Python 字节码缓存；
初态已有文件及任务显式交付路径受到保护。其他符号链接或特殊文件会明确失败。
新任务包同时回收 workspace-collection.json，记录排除原因与每个业务文件的 SHA256；
结果读取要求收集成功、文件清单和实际终态逐项匹配，不能把下载失败当作空产物继续验收。

每个 trial 的 `verifier/file-semantic-review.json` 绑定任务、测试、判据、
初终态文件树和执行记录哈希。审查者获得真实测试源码、存在的完整轨迹或
Oracle 执行日志；每项结论必须引用完成态实际文件，删除则核对初态确有该文件。
NOP 和变异校准允许行为通过而语义拒绝；正式 rollout 必须行为通过且全部语义接受。
每条 trial 按其自身实际执行与捕获证据审查，一条失败不能跳过另一条的文件审查；
整项任务仍需满足要求的全部复验，FILE 通过不清除未核查的分析回答。

独立 `read-results --plan-dir` 只复核已有回执，不自动调用模型。语义回执缺失、
拒绝、材料不足、哈希过期或引用不符时仍为 `REVIEW`，不能由 pytest 的成功奖励
清除该 FILE 义务。此回执不替代轨迹认证、响应合同或沙盒清理回执。
