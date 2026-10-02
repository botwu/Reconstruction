"""AGS 内执行的小入口；原生 harness 的输入、工具和轨迹写入保持复用。"""

import runpy
from pathlib import Path


def gateway_agent_type(base):
    """仅约束实际 Messages 请求，不改第三方源码或模型返回。"""

    class GatewayAgent(base):
        def __init__(self, *args, **kwargs):
            model = kwargs.get("model")
            if not isinstance(model, str) or not model.strip():
                raise ValueError("网关模型必须是完整非空字面值")
            super().__init__(*args, **kwargs)
            if self.api_mode != "anthropic_messages":
                raise ValueError("原生网关 harness 仅支持 Anthropic Messages")
            self._gateway_model = model
            self._disable_streaming = True

        def _anthropic_messages_create(self, api_kwargs):
            # Hermes 会归一化模型名；最后的 SDK 调用边界恢复冻结的网关路由。
            return super()._anthropic_messages_create({
                **api_kwargs, "model": self._gateway_model, "stream": False,
            })

    return GatewayAgent


def main():
    import run_agent

    original = run_agent.AIAgent
    run_agent.AIAgent = gateway_agent_type(original)
    try:
        runpy.run_path(
            str(Path(__file__).with_name("hermes_harness_base.py")), run_name="__main__"
        )
    finally:
        run_agent.AIAgent = original


if __name__ == "__main__":
    main()
