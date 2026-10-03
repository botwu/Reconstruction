"""Harbor/AGS 集成使用的稳定异常类型。

异常类名会进入 Harbor ``result.json`` 并参与 retry 白名单，因此不可随意改名。
"""


class HarborAGSIntegrationError(RuntimeError):
    """集成层基础异常。"""


class EnvironmentInfrastructureError(HarborAGSIntegrationError):
    """AGS 环境创建、传输或执行失败。"""


class EnvironmentCleanupError(EnvironmentInfrastructureError):
    """无法确认 sandbox 已被回收。"""


class CaptureInfrastructureError(HarborAGSIntegrationError):
    """Anthropic 请求/响应采集不完整。"""


class TrajectoryCaptureError(CaptureInfrastructureError):
    """原始证据无法生成完整轨迹或 ATIF。"""


class VerifierInfrastructureError(HarborAGSIntegrationError):
    """Verifier 自身未能正常完成。"""


class ArtifactValidationError(HarborAGSIntegrationError):
    """Artifact 结构不安全或不可解析。"""


class HarnessDriftError(HarborAGSIntegrationError):
    """Hermes 版本、system prompt 或工具 schema 与冻结值不同。"""
