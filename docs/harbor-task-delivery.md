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

原 bundle 编译器保留公开初态、冻结依赖、隐藏测试及参考解，同时生成原生容器定义和 workspace 采集 hook；原 AGS 执行路径继续使用原有 setup 和目录。独立 verifier 读取求解后的副本，保持无网络验证。已有旧 bundle 在发布阶段补上同一采集 hook。

```bash
harbor run --path /path/to/published/task --agent oracle --env docker
```

oracle 只用于校准参考解与验证器；实际 rollout 由下游配置求解 agent、模型和调用凭据。文件验证通过不代表自由文本分析通过，既有人工核查状态继续保留。

无网 verifier 需要执行后端支持相应隔离。本次本机 Docker 内核未通过 Harbor 的 nftables 能力探测，因此仅用其检查参考解执行与工作区采集；完整独立验证继续使用现有 Harbor/AGS 后端。不能将跳过验证的 Docker 运行当作验收通过。

跨机器搬运任务包时须保留文件权限，例如 `tar -xzpf tasks.tar.gz`；默认解包的 umask 可能使 AGS 中的普通用户失去工作区写权限。

## search

补全输出 READY 后自动生成 `tasks/<task_id>/harbor/<digest>/task/`，`result.json` 返回 `harbor_task` 和 `harbor_rollout_args`。原有直接检索 rollout 仍保留；这一新增任务路径供下游独立 Harbor runner 使用，不表示旧 runner 已整体替换。

任务说明保留原用户要求及必要指代上下文，`workspace/evidence.json` 原样携带选定的捕获记录与实际抓取资料，并保留解析后的文件来源和坐标。补全 agent 根据原任务声明 `requires_live_web` 和依据；这不改变调用方指定的 domain。

本地代码、文档检索通过 `search_evidence/read_evidence` 查询捕获语料；READY 前必须实际读取所选证据。Harbor solver 可用 Python 读取同一 `evidence.json`，保留当前文件与历史版本的区别。这类任务不安装网页检索脚本，也不要求 Serper/Jina 密钥。

需要公开来源的任务仍须实证完成搜索和页面读取。公开检索工具复用现有实现，在容器中执行：

```bash
traceforge-search search "查询内容"
traceforge-search open "https://example.org/source" --offset 0 --limit 8000
```

原始返回、抓取摘要和调用记录写入 `/logs/artifacts/search/`；不同命令进程的翻页复用第一次保存的正文。任务配置仅包含 `SERPER_API_KEY`、`JINA_API_KEY`、`TRACEFORGE_FETCH_PROVIDER` 的环境变量模板，实际凭据由运行方注入，不随任务交付。

目前 search 自由文本回答没有自动 verifier。按已约定的人工核查方式，使用 Harbor 的 `--disable-verification`，不伪造 `test.sh`、参考答案或成功奖励。`tests/README.md` 和任务外的 `delivery.json` 明确记录此限制。

```bash
# 只验证环境启动；nop 不生成答案，也不构成内容通过。
harbor run --path /path/to/search/task --agent nop --env docker \
  --disable-verification --yes
```

生产 rollout 将 `nop` 替换为已配置的求解 agent 和模型，并保留 `--disable-verification`。环境可查询与回答内容正确分别记录，不能以网页或本地工具调用成功代替内容核查。
