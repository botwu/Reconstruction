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
现有运行时位于 dev-wj：

- Hermes：`/tmp/researcher-hermes-runtime-source`。
- Harbor/AGS：`/tmp/researcher-local-inputs-13/harbor`。
- 实际运行的 Python：`/tmp/researcher-runtime-312.venv/bin/python`。

不要直接复制虚拟环境到另一种操作系统。将实际使用的源码同步到本机目录，在 Python 3.12
环境重新安装，并通过 `--hermes-home`、`--harbor-root` 指定。
Harbor/AGS 的源码快照、边界和部署校验见 [运行时绑定](harbor_ags/README.md) 与
[terminal 预检](terminal-run-config.md)。直接安装任意最新版不能保证与本次实跑相同。

## 私有配置与真实入口

按照[模型连接](model-gateway-config.md)和[角色配置](terminal-run-config.md)创建本机
`config.yaml`；TokenHub、AGS 凭据不写进 Git。需要公开网页的 search 另需
`SERPER_API_KEY` / `JINA_API_KEY` 或私有搜索配置，见[检索配置](search-reconstruction.md)。
原始 JSONL 原样保存与不提交部署凭据是两件事。

下面的输出目录必须是新目录；运行会真实调用模型和相应外部服务：

```bash
uv run traceforge reconstruct raw-run \
  --input return_data/four_batch/by-rubric/R01.jsonl \
  --line-number 559 --domain search \
  --output artifacts/new-machine-search559 --config config.yaml \
  --hermes-home /absolute/path/to/hermes-agent \
  --execute-rollout --rollout-trials 1 --manual-response-review

uv run traceforge reconstruct raw-run \
  --input return_data/four_batch/by-rubric/R04.jsonl \
  --line-number 1 --domain terminal \
  --output artifacts/new-machine-terminal1 --config config.yaml \
  --hermes-home /absolute/path/to/hermes-agent \
  --harbor-root /absolute/path/to/harbor-ags \
  --execute-red --execute-rollout --rollout-trials 2 --manual-response-review
```

`R01:559` 与 `R04:1` 指从 1 起算的物理行，不能用筛选后的序号替代。
数据清单中的样本哈希用于确认抽取了同一条原始会话。
迁移校验只证明代码和输入完整、基础入口可用；不会把已有
`NEEDS_CORRECTION` / `NOT_ASSESSED` 改成验收通过。
