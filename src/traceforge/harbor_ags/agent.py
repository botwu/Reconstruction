"""复用原生 Harbor 生命周期，仅补足网关请求的运行契约。"""

from importlib.resources import as_file, files
from pathlib import Path

from harbor_ags.agent import LosslessHermesAgent


class GatewayHermesAgent(LosslessHermesAgent):
    """原生捕获、对账和清理不变；独立入口约束流式及模型字面值。"""

    async def setup(self, environment):
        await super().setup(environment)
        resource = files("harbor_ags.resources").joinpath("hermes_harness.py")
        with as_file(resource) as source:
            await environment.upload_file(source, "/tmp/harbor_ags_runtime/hermes_harness_base.py")
        await environment.upload_file(
            Path(__file__).with_name("gateway_harness.py"),
            "/tmp/harbor_ags_runtime/hermes_harness.py",
        )
