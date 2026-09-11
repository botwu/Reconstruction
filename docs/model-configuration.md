# 模型配置与连通性

模型调用通过 traceforge.reconstruction.model_gateway 的窄接口完成。默认行为保持原有的 Claude 环境变量客户端；传入 --config 后使用 NewAPI/TokenHub 的 OpenAI-compatible /v1/chat/completions 接口。

## 配置约束

config.yaml 只应保存在部署机，并已被 .gitignore 忽略。配置中的 key 仅在进程内存中读取，不写入请求回执、轨迹 artifact、异常文本或日志。不要把配置文件复制到数据集、Harbor task bundle 或提交记录中。

## 命令示例

    traceforge failure-analysis agentrx \
      --trajectory-json <trajectory.json> \
      --output <diagnosis.json> \
      --config /mnt/afs_toolcall/wujian1/Projects/workspace/RraceRconstruction/config.yaml \
      --channel gemini \
      --model-name gemini-2.5-pro

reconstruct workflow 和 failure-analysis model-judge 支持同样的 --config、--channel 和 --model-name 参数。指定配置且没有覆盖旧的 Claude 默认名时，程序会根据 channel 选择安全默认模型，避免把 Claude 模型名误发到 Gemini channel。

## 当前 dev-wj 探测结果

在 2026-09-11 对 dev-wj 的 gemini channel 做了最小真实请求：TokenHub endpoint 和凭据可用，OpenAI-compatible 请求格式可用；但该 channel 的 /v1/models 没有 Gemini 模型，常见 Gemini 模型名返回 HTTP 503（无可用后端路由）。因此当前不能把这次结果称为 Gemini 重建测试成功，需要先补充正确的 Gemini model ID/分组或修复 channel 路由。

同一 channel 用探测到的可用 GPT 模型验证了客户端请求、响应解析和调用回执链路；这只是协议兼容性测试，不替代 Gemini 模型测试。Claude 在 Gemini 路由问题解决前不启用。
