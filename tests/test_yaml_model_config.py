"""配置格式不能改变实际选择的模型或静默掩盖无效值。"""

from pathlib import Path

import pytest

from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    iter_config_items,
    load_channel_connection,
)
from traceforge.reconstruction.run_config import load_role_settings, load_rollout_limits


def test_nested_yaml_preserves_channel_role_and_limits(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "claude:\n"
        "  _type: newapi_channel_conn\n"
        "  key: unit-secret\n"
        "  url: https://gateway.example\n"
        "roles:\n"
        "  rollout:\n"
        "    channel: claude\n"
        "    model: claude-opus-4-8/awsb_L/sfa\n"
        "    timeout_seconds: 14400\n"
        "    max_iterations: 500\n",
        encoding="utf-8",
    )
    assert load_channel_connection(config, "claude") == (
        "https://gateway.example", "unit-secret"
    )
    role = load_role_settings(config, "rollout")
    assert role.model == "claude-opus-4-8/awsb_L/sfa"
    assert role.channel == "claude"
    assert load_rollout_limits(config) == (14400, 500)
    assert "unit-secret" not in repr(role)


@pytest.mark.parametrize("text", ["", "null\n", "- roles\n", "1: {}\n"])
def test_invalid_top_level_config_fails_explicitly(tmp_path: Path, text: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    with pytest.raises(ModelGatewayError) as error:
        load_role_settings(config, "rollout")
    assert error.value.code == "CONFIG_PARSE_ERROR"


def test_invalid_yaml_does_not_disclose_input_in_exception(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("claude: [unit-secret\n", encoding="utf-8")
    with pytest.raises(ModelGatewayError) as error:
        iter_config_items(config)
    assert error.value.code == "CONFIG_PARSE_ERROR"
    assert "unit-secret" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "text",
    [
        "roles: null\n",
        "roles: wrong\n",
        "roles:\n  rollout: null\n",
        "roles:\n  rollout:\n    channel: null\n",
        "roles:\n  rollout:\n    model: null\n",
        "roles:\n  rollout:\n    model_name: null\n",
    ],
)
def test_explicit_invalid_role_never_falls_back(tmp_path: Path, text: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    with pytest.raises(ModelGatewayError) as error:
        load_role_settings(config, "rollout")
    assert error.value.code == "ROLE_CONFIG_INVALID"


def test_absent_optional_role_still_uses_documented_default(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        '{"claude":{"key":"unit-secret","url":"https://gateway.example"}}',
        encoding="utf-8",
    )
    assert load_channel_connection(config, "claude")[0] == "https://gateway.example"
    from traceforge.reconstruction.agents.runtime import resolve_rollout_model

    role = load_role_settings(config, "rollout")
    assert role.model == "claude-opus-4-8"
    assert resolve_rollout_model(None, channel=role.channel, model_name=role.model) == (
        "anthropic/claude-opus-4-8"
    )
    assert load_rollout_limits(config) == (900, 60)
