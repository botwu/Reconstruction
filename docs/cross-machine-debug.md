# 跨机器调试

代码远程为 `git@github.com:botwu/Reconstruction.git`，在 dev-wj 上名为
`reconstruction`；原有 `origin` 和 `upstream` 保留。迁移不改变 search /
terminal 的处理策略，也不表示现有真实回答全部通过，见[当前状态](current-status.md)。

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
Hermes 源码及其依赖，terminal 另需 Harbor/AGS 适配运行时和沙盒权限。
这些运行时不能由原始 session 的 harness 名称替代，也不属于本项目的基础依赖。
Hermes 对应仓库为 `SenseTime-FVG/hermes-agent`，固定提交
`83c2ca5b2e250d69ce301c751ea83fd425eb2de1`；已核对当前实跑的入口、依赖声明、
锁文件和上下文压缩模块与该提交一致。Harbor/AGS 是现有适配源码，仍需通过 dev-wj
私有同步。当前兼容快照包含 src/resources、配置、pyproject、uv.lock 和 version-lock，
保存在下面的项目内固定路径；逐文件哈希位于快照内 source-manifest.json，
导入检查位于快照上层的 import-verification.json。它支持项目使用的 preserve_source_literals 认证参数。
旧 repository-transfer-20261001 快照不支持该参数，不能作为当前运行时。
新快照已完成本地导入与接口检查，真实端到端验证仍以当前状态记录为准。

在仓库根目录执行以下步骤；需要已有的 dev-wj SSH 访问权限：

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
