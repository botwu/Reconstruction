"""重建各角色的模型与运行配置。

YAML/JSON 中的 channel 条目可包含凭据，roles 映射选择各阶段的模型；
对外仅返回角色元数据，凭据保留在模型网关内存中。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    iter_config_items,
    load_channel_model,
)

ROLE_DEFAULTS: dict[str, tuple[str, str]] = {
    "session_parser": ("deepseek", "bailian/deepseek-v4-flash-0731"),
    "reconstruction": ("gpt", "gpt-5"),
    "verifier": ("gpt", "gpt-5"),
    "rollout": ("claude", "anthropic/claude-opus-4-8"),
}


@dataclass(frozen=True, slots=True)
class RoleSettings:
    role: str
    channel: str
    model: str

    def public(self) -> dict[str, str]:
        return {"role": self.role, "channel": self.channel, "model": self.model}


def _role_entries(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    for name, value in iter_config_items(path):
        if name == "roles":
            if not isinstance(value, dict):
                raise ModelGatewayError("roles 配置必须为对象", code="ROLE_CONFIG_INVALID")
            return value
    return {}


def load_role_settings(
    path: str | Path | None,
    role: str,
    *,
    channel_override: str | None = None,
    model_override: str | None = None,
) -> RoleSettings:
    """Resolve one role without ever returning a credential."""

    if role not in ROLE_DEFAULTS:
        raise ValueError(f"unknown model role: {role}")
    default_channel, default_model = ROLE_DEFAULTS[role]
    entry = _role_entries(path).get(role, {})
    if not isinstance(entry, dict):
        raise ModelGatewayError(f"role config must be an object: {role}", code="ROLE_CONFIG_INVALID")
    configured_channel = entry.get("channel")
    if "channel" in entry and not isinstance(configured_channel, str):
        raise ModelGatewayError(f"role channel must be a string: {role}", code="ROLE_CONFIG_INVALID")
    if isinstance(configured_channel, str) and not configured_channel.strip():
        raise ModelGatewayError(f"role channel is empty: {role}", code="ROLE_CONFIG_INVALID")
    if channel_override is not None and not channel_override.strip():
        raise ModelGatewayError(f"role channel override is empty: {role}", code="ROLE_CONFIG_INVALID")
    channel = (channel_override if channel_override is not None else configured_channel or default_channel).strip()
    configured_model = entry["model"] if "model" in entry else entry.get("model_name")
    if ("model" in entry or "model_name" in entry) and not isinstance(configured_model, str):
        raise ModelGatewayError(f"role model must be a string: {role}", code="ROLE_CONFIG_INVALID")
    if isinstance(configured_model, str) and not configured_model.strip():
        raise ModelGatewayError(f"role model is empty: {role}", code="ROLE_CONFIG_INVALID")
    if model_override is not None and not model_override.strip():
        raise ModelGatewayError(f"role model override is empty: {role}", code="ROLE_CONFIG_INVALID")
    model = (model_override if model_override is not None else configured_model or (load_channel_model(path, channel) if path else None) or default_model).strip()
    if not model:
        raise ModelGatewayError(f"role model is empty: {role}", code="ROLE_CONFIG_INVALID")
    return RoleSettings(role=role, channel=channel, model=model)


def load_rollout_limits(
    path: str | Path | None,
    *,
    timeout_seconds: int | None = None,
    max_iterations: int | None = None,
) -> tuple[int, int]:
    """从 rollout 角色读取单次执行预算，CLI 显式值优先。"""
    entry = _role_entries(path).get("rollout", {})
    if not isinstance(entry, dict):
        raise ModelGatewayError("rollout 配置必须为对象", code="ROLE_CONFIG_INVALID")
    values = []
    for key, override, default in (
        ("timeout_seconds", timeout_seconds, 900),
        ("max_iterations", max_iterations, 60),
    ):
        configured = entry.get(key, default)
        if isinstance(configured, bool) or not isinstance(configured, int) or configured < 1:
            raise ModelGatewayError(
                f"rollout.{key} 必须是正整数", code="ROLE_CONFIG_INVALID"
            )
        # Explicit overrides may increase the reviewed budget, but cannot
        # silently shorten it and end a real rollout early.
        if override is not None and override < configured:
            raise ModelGatewayError(
                f"rollout.{key} lower than reviewed config budget {configured}",
                code="ROLLOUT_BUDGET_DOWNGRADE",
            )
        value = override if override is not None else configured
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ModelGatewayError(
                f"rollout.{key} 必须是正整数", code="ROLE_CONFIG_INVALID"
            )
        values.append(value)
    return values[0], values[1]


def resolve_role_matrix(
    path: str | Path | None,
    *,
    overrides: dict[str, tuple[str | None, str | None]] | None = None,
) -> dict[str, RoleSettings]:
    matrix = {}
    for role in ROLE_DEFAULTS:
        channel, model = (overrides or {}).get(role, (None, None))
        matrix[role] = load_role_settings(
            path,
            role,
            channel_override=channel,
            model_override=model,
        )
    def canonical_model(settings: RoleSettings) -> str:
        value = settings.model.strip().lower()
        # The provider is already selected by the role channel. Compare the
        # model identity across aliases such as gpt-5/openai/gpt-5 and
        # claude-opus-4-8/anthropic/claude-opus-4-8.
        parts = value.split("/")
        if len(parts) > 1 and parts[0] in {
            "anthropic", "openai", "deepseek", "vol", "bailian", "google", "gemini"
        }:
            parts = parts[1:]
        # TokenHub 的 /awsb_L/sfa 等后缀是路由，不是模型身份。
        return parts[0]

    if canonical_model(matrix["reconstruction"]) == canonical_model(matrix["rollout"]):
        raise ValueError("reconstruction and rollout models must be different")
    return matrix


__all__ = [
    "ROLE_DEFAULTS",
    "RoleSettings",
    "load_role_settings",
    "load_rollout_limits",
    "resolve_role_matrix",
]
