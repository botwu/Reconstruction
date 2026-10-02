# 跨机器调试

代码远程为 `git@github.com:botwu/Reconstruction.git`，在 dev-wj 上名为
`reconstruction`；原有 `origin` 和 `upstream` 保留。迁移不改变 search /
terminal 的处理策略，也不表示现有真实回答全部通过，见[当前状态](current-status.md)。
公开仓库提供代码和原始数据；完整实跑仍依赖私有 Hermes 源码、自定义 Harbor/AGS 运行时
及 AGS 模板访问权限，不能仅凭公开 clone 在新机器上直接执行。

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
| 自定义 `harbor_ags` | 原目录为 `/mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags`；原源码仓库及代码归属尚待确认，未纳入本仓库。它与公开 Harbor 核心是两份不同代码，不能只安装 `harbor==0.22.0` 替代。 |
| AGS 模板 | 需要北京 AGS 服务 `ap-beijing.tencentags.com` 的凭据及 `node-python-hermes` 模板使用权限；当前原生 rollout 核对的模板内 Hermes 提交为 `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43`，与宿主 Hermes 分别固定。模板镜像构建定义和 digest 尚未随仓库交付。 |

官方 `harbor-0.22.0-py3-none-any.whl` 的 SHA256 为
`4c4c6571b3d160ed0cb45b82918136751fb08e7b8596412723ac00dde12eeabb`。

自定义运行时目前只能通过已有 dev-wj 权限私有同步。兼容快照包含 src/resources、配置、
pyproject、uv.lock 和 version-lock，逐文件哈希在 `source-manifest.json`，
导入检查在快照上层的 `import-verification.json`。该快照支持项目使用的
`preserve_source_literals` 认证参数；旧 `repository-transfer-20261001` 快照不支持，不能替代。
本仓库 `docs/harbor_ags/` 的历史补丁也不足以恢复完整的当前运行时。
已完成的本地导入与接口检查不等于新机器安装验证；真实端到端状态见[当前状态](current-status.md)。

以下是已有私有源码和 dev-wj SSH 权限时的迁移步骤，尚未完成全新机器实测。
其中 `uv pip install` 不读取项目或复制的运行时 `uv.lock`；该命令不保证整套传递依赖
与现有实跑环境一致，统一锁定安装仍待补齐。

```bash
mkdir -p .runtime
git clone git@github.com:SenseTime-FVG/hermes-agent.git .runtime/hermes-agent
git -C .runtime/hermes-agent checkout 83c2ca5b2e250d69ce301c751ea83fd425eb2de1
rsync -a --exclude=.venv --exclude=.git --exclude=__pycache__ \
  dev-wj:/mnt/afs_toolcall/wujian1/Projects/workspace/TraceRconstruction/artifacts/pipeline-debug-20261002/runtime-compatible/harbor/ .runtime/harbor-ags/
uv venv --python 3.12 .runtime/harbor-ags/.venv
uv pip install --python .runtime/harbor-ags/.venv/bin/python \
  -e . -e ".runtime/hermes-agent[anthropic]" -e .runtime/harbor-ags "pytest==8.4.2"
export TRACEFORGE_PYTHON="$PWD/.runtime/harbor-ags/.venv/bin/python"
```

运行环境放在 Harbor/AGS 目录的 `.venv`，满足它现有的可执行入口约定。
不要复制旧虚拟环境，也不要对这个实跑环境执行会移除额外依赖的基础 `uv sync`。
`.runtime/` 已忽略。Hermes 与 Harbor/AGS 的完整部署及网络权限仍须在目标机器
预检；上述说明不把源码同步当成新机器上已经完成真实 rollout。

## 私有配置与真实入口

按照[模型连接](model-gateway-config.md)和[角色配置](terminal-run-config.md)创建本机
`config.yaml`；TokenHub、AGS 凭据不写进 Git。需要公开网页的 search 另需
`SERPER_API_KEY` / `JINA_API_KEY` 或私有搜索配置，见[检索配置](search-reconstruction.md)。
原始 JSONL 原样保存与不提交部署凭据是两件事。

下面的输出目录必须是新目录；运行会真实调用模型和相应外部服务：

```bash
"$TRACEFORGE_PYTHON" -m traceforge reconstruct raw-run \
  --input return_data/four_batch/by-rubric/R01.jsonl \
  --line-number 559 --domain search \
  --output artifacts/new-machine-search559 --config config.yaml \
  --hermes-home "$PWD/.runtime/hermes-agent" \
  --harbor-root "$PWD/.runtime/harbor-ags" \
  --execute-rollout --rollout-trials 1 --manual-response-review

"$TRACEFORGE_PYTHON" -m traceforge reconstruct raw-run \
  --input return_data/four_batch/by-rubric/R04.jsonl \
  --line-number 1 --domain terminal \
  --output artifacts/new-machine-terminal1 --config config.yaml \
  --hermes-home "$PWD/.runtime/hermes-agent" \
  --harbor-root "$PWD/.runtime/harbor-ags" \
  --execute-red --execute-rollout --rollout-trials 2 --manual-response-review
```

`R01:559` 与 `R04:1` 指从 1 起算的物理行，不能用筛选后的序号替代。
数据清单中的样本哈希用于确认抽取了同一条原始会话。
迁移校验只证明代码和输入完整、基础入口可用；不会把已有
`NEEDS_CORRECTION` / `NOT_ASSESSED` 改成验收通过。

批量执行、逐条结果及中断续跑见[批量重建](batch-reconstruction.md)。先验证小批次的真实产物，再扩大处理范围。
