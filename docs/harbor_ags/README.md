# Harbor AGS 运行时绑定

TraceForge 使用外部 harbor_root 中的 Harbor AGS。每个 rollout 计划记录
agent.py、capture.py、evidence.py 和 validator.py 的 SHA-256；
执行时若文件变更或缺失，立即拒绝运行。

仓库中的 evidence-compaction-binding.patch 和 validator-compaction-binding.patch
记录了相对于 /mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags 下
.pre-compaction-20260924 快照的运行时修改。它们通过 tool_call_id 将 Hermes
assistant 消息与捕获的 exchange 绑定，保留被上下文压缩省略的原始调用证据，
并根据轨迹实际表示的调用计算 ATIF 用量，同时保留原始总用量。
只跳过上下文压缩后已失去定义的顺序比对；产物清单仍校验路径、文件类型、
符号链接和硬链接。

capture-transport.patch 记录 capture/agent 修改和重试回归测试。
代理使用 read1 实时转发 SSE，无需等满 64 KiB 缓冲区。
capture_timeout_sec 是独立的上游 socket 空闲超时，默认 300 秒；
它与 AGS API 超时、整轮 rollout 截止时间分别配置。
桥接层保留用户显式配置的 agent kwargs。

验证器保留并校验每一次失败的捕获。只有空响应传输失败，随后出现原始请求
字节、endpoint 和已记录请求头均相同的成功请求时，才免除该失败尝试的
完整性错误。部分输出、无效哈希、泄露的认证头、不同请求和先前的成功请求
仍不能通过验收。恢复情况记录为 RETRY_SUPERSEDED_TRANSPORT_ERROR，
失败尝试不会从证据中删除。

上述 patch 保留历史变更。当前外部 Harbor 源码单独归档，完整哈希、部署回执和固定的核心提交见项目 `artifacts/pipeline-debug-20260929/default-integration/release-freeze.json`；每份旧执行仍绑定自己的运行时，不随新版本改写。

原文保留模式只认可经过哈希绑定的初始工作区中已有的常量，以及能从该原文证明的显示缩略。检测记录保留，新增凭据和显式运行密钥仍拒收。源码常量是否属于原输入，与任务行为是否通过分别判定；失败产物仍保持 TASK_FAIL。
