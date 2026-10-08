# 跨机器调试

代码远程为 `git@github.com:botwu/Reconstruction.git`，在 dev-wj 上名为
`reconstruction`；原有 `origin` 和 `upstream` 保留。迁移不改变 search /
terminal 的处理策略，也不表示现有真实回答全部通过，见[当前状态](current-status.md)。
公开仓库提供代码、原始数据和自建 Harbor/AGS 连接层；完整实跑仍需要私有 Hermes 源码、
模型配置与 AGS 模板访问权限。连接层源码已纳入本项目，不再依赖 dev-wj 项目外的目录。

## 代码与完整数据

两份完整原始数据经用户明确授权，作为本仓库的 Git LFS 对象公开发布。

要求 Python 3.12、Git；获取数据还需要 Git LFS。按以下步骤恢复输入：

```bash
git lfs install
git clone git@github.com:botwu/Reconstruction.git
cd Reconstruction
git lfs pull
python3.12 scripts/restore_debug_data.py
python3.12 scripts/restore_debug_data.py --verify-only
```

两份输入对应 `return_data/four_batch/by-rubric/R01.jsonl`（search，1,683 条）和
`R04.jsonl`（terminal，6,535 条）。`data/debug-datasets.json` 固定整文件与压缩包的
SHA256、字节数、物理行数及已使用样本的哈希。压缩只改变存储形式，解压后逐字节还原；
不脱敏、不筛选、不重排、不重新序列化。原文件合计约 3.25 GB，压缩包约 840 MB。

恢复脚本先校验压缩包，再校验临时解压文件，最后发布到原路径；已有数据只有哈希一致才复用。
大小或哈希不同会报错并保留现有文件，不自动覆盖。若压缩包通过其他私有渠道取得：

```bash
python3.12 scripts/restore_debug_data.py --archive-dir /absolute/path/to/archives
```

仅获取代码可用 `GIT_LFS_SKIP_SMUDGE=1 git clone ...`。若看到约百字节的 LFS 指针、
压缩包缺失或校验失败，先检查仓库访问权限并执行 `git lfs pull`，不要把指针当数据。

## 开发环境

```bash
uv sync --frozen --dev --python 3.12
uv run traceforge --help
uv run pytest -m 'not live'
uv run ruff check src scripts --select F,E9
```

`uv.lock` 固定项目的基础开发依赖；普通检查不发起模型请求；外部 Harbor 的认证与清理接口使用明确替身，
不依赖 dev-wj 的绝对路径，也不宣称验证了外部运行时本身。完整模型执行还需要
Hermes 源码及其依赖；search 和 terminal 的原生 rollout 都需要 Harbor/AGS 适配运行时。
这些运行时不能由原始 session 的 harness 名称替代，也不属于本项目的基础依赖。

已核实的运行依赖如下：

| 依赖 | 固定来源与迁移前提 |
| --- | --- |
| Harbor 核心 | [PyPI Harbor 0.22.0](https://pypi.org/project/harbor/0.22.0/)，对应源码提交 `4407eb5227a2ff4f0d3f16b2eb48849382fdf276`；现有 335 个 Python 文件与官方 wheel 一致，无须复制或修改核心源码。 |
| 宿主 Hermes | `git@github.com:SenseTime-FVG/hermes-agent.git`，固定提交 `83c2ca5b2e250d69ce301c751ea83fd425eb2de1`，需要该私有仓库访问权限；当前选取的 842 个运行源码文件均与提交匹配。 |
| 自建 `harbor_ags` | 已纳入 [integrations/harbor_ags](../integrations/harbor_ags/README.md)，来自真实实跑快照；`provenance.json` 记录原始哈希及精简范围。官方 Harbor 核心仍通过 PyPI 安装。 |
| AGS 模板 | 需要北京 AGS 服务 `ap-beijing.tencentags.com` 的凭据及 `node-python-hermes` 模板使用权限；当前原生 rollout 核对的模板内 Hermes 提交为 `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43`，与宿主 Hermes 分别固定。跨机调用同一云模板无需重新构建；只有独立复建镜像才需要目前尚未交付的构建定义与 digest。 |

官方 `harbor-0.22.0-py3-none-any.whl` 的 SHA256 为
`4c4c6571b3d160ed0cb45b82918136751fb08e7b8596412723ac00dde12eeabb`。

连接层的实际源码、配置与离线资源已随仓库交付；历史补丁和无关交易示例已移除。
当前使用源码 checkout 加 editable 安装，`--harbor-root` 指向 `integrations/harbor_ags`；
它依赖该目录的版本锁和配置文件，不宣称独立 wheel 安装具有完整运行入口。

统一 `runtime-requirements.txt` 同时解析本项目、连接层和上述固定 Hermes 的依赖，
以 SHA256 锁定官方 PyPI 包。锁对应本次验证的 Linux x86_64 / Python 3.12 宿主，
不把三份各自的锁直接拼接；其他宿主平台需另行解析和验证。已在项目内新建环境完成安装：125 个第三方包及三个源码包依赖一致，四个 CLI 可用，两份既有真实捕获回执复核通过；新环境也完成了真实 AGS 上传、执行、下载和回收。该结果不等于在新电脑上已经完成完整模型 rollout。安装步骤如下：

```bash
mkdir -p .runtime
git clone git@github.com:SenseTime-FVG/hermes-agent.git .runtime/hermes-agent
git -C .runtime/hermes-agent checkout 83c2ca5b2e250d69ce301c751ea83fd425eb2de1

uv venv --python 3.12 integrations/harbor_ags/.venv
export TRACEFORGE_PYTHON="$PWD/integrations/harbor_ags/.venv/bin/python"
uv pip sync --python "$TRACEFORGE_PYTHON" --require-hashes --only-binary :all: \
  integrations/harbor_ags/runtime-requirements.txt
uv pip install --python "$TRACEFORGE_PYTHON" --no-deps --no-build-isolation \
  -e . -e ".runtime/hermes-agent[anthropic]" -e integrations/harbor_ags
uv pip check --python "$TRACEFORGE_PYTHON"
```

解释器和 Harbor 命令位于连接层目录的 `.venv/bin/`，满足现有执行入口约定。
不要复制旧虚拟环境，也不要对该环境执行会移除 Hermes 依赖的基础 `uv sync`。
`.runtime/`、`.venv/` 均已忽略；私有 Hermes 源码和部署凭据不进入公开 Git。
新机器仍需验证网络、模型和 AGS 权限；安装或导入通过不能代替真实 rollout。

需要按新的源码版本重新解析锁时，在已取得固定 Hermes checkout 后执行：

```bash
uv pip compile --default-index https://pypi.org/simple --no-header --no-annotate \
  --generate-hashes --extra anthropic --python-version 3.12 \
  --python-platform x86_64-unknown-linux-gnu \
  --output-file integrations/harbor_ags/runtime-requirements.txt \
  pyproject.toml integrations/harbor_ags/pyproject.toml \
  .runtime/hermes-agent/pyproject.toml integrations/harbor_ags/runtime-extras.in
```

## 私有配置与真实入口

按照[模型连接](model-gateway-config.md)和[角色配置](terminal-run-config.md)创建本机
`config.yaml`；TokenHub、AGS 凭据不写进 Git。需要公开网页的 search 另需
`SERPER_API_KEY` / `JINA_API_KEY` 或私有搜索配置，见[检索配置](search-reconstruction.md)。
原始 JSONL 原样保存与不提交部署凭据是两件事。

下面的输出目录必须是新目录；运行会真实调用模型和相应外部服务：

```bash
"$TRACEFORGE_PYTHON" -m traceforge reconstruct raw-run \
  --input return_data/four_batch/by-rubric/R01.jsonl \
  --line-number 38 --domain search \
  --output artifacts/new-machine-search38 --config config.yaml \
  --hermes-home "$PWD/.runtime/hermes-agent" \
  --harbor-root "$PWD/integrations/harbor_ags" \
  --disable-verification --execute-rollout --rollout-trials 2

"$TRACEFORGE_PYTHON" -m traceforge reconstruct raw-run \
  --input return_data/four_batch/by-rubric/R04.jsonl \
  --line-number 1 --domain terminal \
  --output artifacts/new-machine-terminal1 --config config.yaml \
  --hermes-home "$PWD/.runtime/hermes-agent" \
  --harbor-root "$PWD/integrations/harbor_ags" \
  --disable-verification --execute-rollout --rollout-trials 2
```

以上先交付任务与环境，并完成两次未评分求解；这是本轮实际跑通的路径。
如需另行生成和校准自动文件评分器，terminal 示例将 `--disable-verification`
替换为 `--execute-red`；分析回答的人工核查另加 `--manual-response-review`，
它不代表模型回答已自动通过。

`R01:38` 与 `R04:1` 指从 1 起算的物理行，不能用筛选后的序号替代。
数据清单中的样本哈希用于确认抽取了同一条原始会话。
迁移校验只证明代码和输入完整、基础入口可用；不会把已有
`NEEDS_CORRECTION` / `NOT_ASSESSED` 改成验收通过。

批量执行、逐条结果及中断续跑见[批量重建](batch-reconstruction.md)。先验证小批次的真实产物，再扩大处理范围。
