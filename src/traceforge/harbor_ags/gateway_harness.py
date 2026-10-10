"""AGS 内执行的小入口；原生 harness 的输入、工具和轨迹写入保持复用。"""

import atexit
import os
import runpy
import shutil
import tempfile
from contextlib import contextmanager
from importlib import import_module
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


@contextmanager
def _native_vision_home():
    """显式视觉工具仅使用当前模型的像素通路，不改模板配置或调用辅助视觉模型。"""
    toolsets = os.environ.get("HERMES_TOOLSETS", "file,terminal").split(",")
    if "vision" not in {name.strip() for name in toolsets}:
        yield
        return
    previous = os.environ.get("HERMES_HOME")
    home = tempfile.mkdtemp(prefix="traceforge-native-vision-")
    # 先注册，退出时最后清理，让 Hermes 后注册的清理函数仍可写入日志。
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    config = Path(home) / "config.yaml"
    config.write_text(
        "model:\n  supports_vision: true\nagent:\n  image_input_mode: native\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    os.environ["HERMES_HOME"] = home
    try:
        vision = import_module("tools.vision_tools")
        registry = import_module("tools.registry").registry
        original_entry = registry.get_entry("vision_analyze")
        if original_entry is None:
            raise RuntimeError("Hermes 未注册原生图像工具 vision_analyze")
        original_native = vision._should_use_native_vision_fast_path
        metadata = {
            key: getattr(original_entry, key)
            for key in (
                "name", "toolset", "schema", "handler", "requires_env", "is_async",
                "description", "emoji", "max_result_size_chars", "dynamic_schema_overrides",
            )
        }

        def require_native():
            if original_native() is not True:
                raise RuntimeError("原生图像通路未启用，禁止调用辅助视觉模型")
            return True

        def register_vision(check_fn):
            registry.deregister("vision_analyze")
            registry.register(**metadata, check_fn=check_fn)

        # 模板的可用性检查要求辅助模型；原生模式只检查实际像素通路。
        # 同一判断也覆盖公共 handler 的顺序和并发工具执行。
        vision._should_use_native_vision_fast_path = require_native
        try:
            register_vision(require_native)
            yield
        finally:
            try:
                register_vision(original_entry.check_fn)
            finally:
                vision._should_use_native_vision_fast_path = original_native
    finally:
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


def main():
    with _native_vision_home():
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
