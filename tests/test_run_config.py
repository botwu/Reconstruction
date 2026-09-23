from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.reconstruction.run_config import load_role_settings, resolve_role_matrix


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "gpt:\n"
        "  {\"key\":\"fixture\",\"url\":\"https://example.test\",\"model\":\"gpt-5\"}\n"
        "claude:\n"
        "  {\"key\":\"fixture\",\"url\":\"https://example.test\",\"model\":\"anthropic/claude-opus-4-8\"}\n"
        "roles:\n"
        "  {\"screening\":{\"channel\":\"deepseek\",\"model\":\"bailian/deepseek-v4-flash-0731\"},"
        "\"reconstruction\":{\"channel\":\"gpt\",\"model\":\"gpt-5\"},"
        "\"verifier\":{\"channel\":\"gpt\",\"model\":\"gpt-5\"},"
        "\"rollout\":{\"channel\":\"claude\",\"model\":\"anthropic/claude-opus-4-8\"}}\n",
        encoding="utf-8",
    )
    return path


def test_role_matrix_resolves_public_metadata_without_keys(tmp_path: Path) -> None:
    matrix = resolve_role_matrix(_config(tmp_path))
    assert matrix["reconstruction"].public() == {
        "role": "reconstruction",
        "channel": "gpt",
        "model": "gpt-5",
    }
    assert matrix["rollout"].model == "anthropic/claude-opus-4-8"
    assert "key" not in json.dumps({name: value.public() for name, value in matrix.items()})


def test_role_override_is_explicit(tmp_path: Path) -> None:
    role = load_role_settings(
        _config(tmp_path),
        "reconstruction",
        channel_override="gpt",
        model_override="gpt-5-mini",
    )
    assert role.channel == "gpt"
    assert role.model == "gpt-5-mini"


def test_same_reconstruction_and_rollout_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be different"):
        resolve_role_matrix(
            _config(tmp_path),
            overrides={
                "reconstruction": ("gpt", "same"),
                "rollout": ("gpt", "same"),
            },
        )


def test_canonical_claude_provider_alias_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be different"):
        resolve_role_matrix(
            _config(tmp_path),
            overrides={
                "reconstruction": ("claude", "claude-opus-4-8"),
                "rollout": ("claude", "anthropic/claude-opus-4-8"),
            },
        )


def test_screening_limits_are_validated_and_loaded(tmp_path: Path) -> None:
    path = _config(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        '"screening":{"channel":"deepseek","model":"bailian/deepseek-v4-flash-0731"}',
        '"screening":{"channel":"deepseek","model":"bailian/deepseek-v4-flash-0731",'
        '"max_input_chars":321,"max_messages_for_triage":7,'
        '"max_source_requests_for_triage":3}',
    )
    path.write_text(text, encoding="utf-8")
    from traceforge.reconstruction.run_config import load_screening_limits
    assert load_screening_limits(path) == (321, 7, 3)


def test_provider_prefix_alias_cannot_bypass_model_separation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be different"):
        resolve_role_matrix(
            _config(tmp_path),
            overrides={
                "reconstruction": ("gpt", "gpt-5"),
                "rollout": ("claude", "openai/gpt-5"),
            },
        )


def test_explicit_empty_role_model_is_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path)
    text = path.read_text(encoding="utf-8").replace('"reconstruction":{"channel":"gpt","model":"gpt-5"}', '"reconstruction":{"channel":"gpt","model":""}')
    path.write_text(text, encoding="utf-8")
    with pytest.raises(Exception, match="empty"):
        load_role_settings(path, "reconstruction")


def test_rollout_limits_load_config_and_explicit_overrides(tmp_path: Path) -> None:
    from traceforge.reconstruction.run_config import load_rollout_limits
    path = tmp_path / "config.yaml"
    path.write_text('roles:\n  {"rollout":{"timeout_seconds":1800,"max_iterations":75}}\n')
    assert load_rollout_limits(path) == (1800, 75)
    assert load_rollout_limits(path, max_iterations=90) == (1800, 90)
    assert load_rollout_limits(None) == (900, 60)


def test_rollout_limit_override_cannot_downgrade_reviewed_budget(tmp_path: Path) -> None:
    from traceforge.reconstruction.model_gateway import ModelGatewayError
    from traceforge.reconstruction.run_config import load_rollout_limits
    path = tmp_path / "config.yaml"
    path.write_text(
        'roles:\n  {"rollout":{"timeout_seconds":14400,"max_iterations":500}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelGatewayError, match=r"ROLLOUT_BUDGET_DOWNGRADE|lower than reviewed"):
        load_rollout_limits(path, timeout_seconds=300)
    with pytest.raises(ModelGatewayError, match=r"ROLLOUT_BUDGET_DOWNGRADE|lower than reviewed"):
        load_rollout_limits(path, max_iterations=3)
    assert load_rollout_limits(path, timeout_seconds=14400, max_iterations=500) == (14400, 500)
    assert load_rollout_limits(path, timeout_seconds=15000, max_iterations=600) == (15000, 600)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "900"])
def test_rollout_limits_reject_invalid_budget(tmp_path: Path, bad: object) -> None:
    from traceforge.reconstruction.model_gateway import ModelGatewayError
    from traceforge.reconstruction.run_config import load_rollout_limits
    path = tmp_path / "config.yaml"
    path.write_text("roles:\n  " + json.dumps({"rollout":{"timeout_seconds":bad}}) + "\n")
    with pytest.raises(ModelGatewayError, match="正整数"):
        load_rollout_limits(path)


def test_route_suffix_cannot_bypass_model_separation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be different"):
        resolve_role_matrix(_config(tmp_path), overrides={
            "reconstruction": ("claude", "claude-opus-4-8"),
            "rollout": ("claude", "anthropic/claude-opus-4-8/awsb_L/sfa"),
        })
    matrix = resolve_role_matrix(_config(tmp_path), overrides={
        "reconstruction": ("claude", "claude-sonnet-4-6/awsb_L/sfa"),
        "rollout": ("claude", "anthropic/claude-opus-4-8/awsb_L/sfa"),
    })
    assert matrix["reconstruction"].model != matrix["rollout"].model
