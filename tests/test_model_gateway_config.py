import json

import pytest

from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    ModelRequest,
    NewAPIClient,
    build_chat_model,
    load_channel_connection,
    load_e2b_api_key,
    resolve_model_name,
)


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="req-1",
        model="gemini-test",
        system="return JSON",
        prompt='{"ok": true}',
        response_schema="object",
    )


def test_newapi_config_loads_without_persisting_secret(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        'gemini:\n  {"_type":"newapi_channel_conn","key":"unit-secret","url":"https://gateway.example"}\n',
        encoding="utf-8",
    )
    captured = {}

    def transport(url, headers, body, timeout):
        captured.update(url=url, headers=dict(headers), body=json.loads(body))
        return 200, (
            b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}],'
            b'"usage":{"prompt_tokens":3,"completion_tokens":4}}'
        )

    client = NewAPIClient.from_config(config, transport=transport)
    response = client.complete(_request())

    assert captured["url"] == "https://gateway.example/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer unit-secret"
    assert captured["body"]["messages"][0]["role"] == "system"
    assert response.text == '{"ok":true}'
    assert response.provider == "newapi:gemini"
    assert "unit-secret" not in repr(response)


def test_newapi_supports_v1_endpoint_and_content_parts():
    def transport(url, headers, body, timeout):
        assert url == "https://gateway.example/v1/chat/completions"
        return 200, (
            b'{"choices":[{"message":{"content":[{"type":"text","text":"a"},{"text":"b"}]}}]}'
        )

    client = NewAPIClient(api_key="k", base_url="https://gateway.example/v1", transport=transport)
    assert client.complete(_request()).text == "ab"


def test_newapi_config_errors_do_not_expose_secret(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        'claude:\n  {"key":"secret-value","url":"https://gateway.example"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelGatewayError, match="channel") as exc_info:
        NewAPIClient.from_config(config, channel="gemini")
    assert "secret-value" not in str(exc_info.value)


def test_build_chat_model_uses_config_channel(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text('gemini:\n  {"key":"k","url":"https://gateway.example"}\n', encoding="utf-8")
    assert build_chat_model(config_path=config).__class__ is NewAPIClient
    assert (
        resolve_model_name("claude-opus-4-8", config_path=config, channel="gemini")
        == "gemini-2.5-pro"
    )
    assert (
        resolve_model_name("gemini-2.5-flash", config_path=config, channel="gemini")
        == "gemini-2.5-flash"
    )


def test_config_loads_e2b_scalar_and_channel_without_exposing_secret(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "e2bapikey:\n"
        "  e2b_unit_sandbox\n"
        "deepseek:\n"
        '  {"_type":"newapi_channel_conn","key":"unit-channel-secret",'
        '"url":"https://tokenhub.example"}\n',
        encoding="utf-8",
    )
    assert load_e2b_api_key(config) == "e2b_unit_sandbox"
    url, key = load_channel_connection(config, "deepseek")
    assert url == "https://tokenhub.example"
    assert key == "unit-channel-secret"
    with pytest.raises(ModelGatewayError, match="channel") as exc_info:
        load_channel_connection(config, "missing")
    assert "unit-channel-secret" not in str(exc_info.value)
    assert "e2b_unit_sandbox" not in str(exc_info.value)


def test_timeout_is_normalized_to_gateway_error():
    def transport(*_args):
        raise TimeoutError("socket timed out")

    client = NewAPIClient(
        api_key="k", base_url="https://gateway.example/v1", max_retries=0, transport=transport
    )
    with pytest.raises(ModelGatewayError, match="超时") as exc_info:
        client.complete(ModelRequest("r", "model", "system", "prompt", "schema"))
    assert exc_info.value.code == "NETWORK_TIMEOUT"


@pytest.mark.parametrize("status", [400, 503])
def test_newapi_http_error_preserves_server_reason_without_credentials(status):
    client = NewAPIClient(
        api_key="deployment-secret", base_url="https://gateway.example", max_retries=0,
        transport=lambda *_: (status, json.dumps({"error": {
            "code": "invalid_value",
            "message": "max_tokens supports at most 128000; provided 131072; deployment-secret",
        }}).encode()),
    )
    with pytest.raises(ModelGatewayError) as error:
        client.complete(_request())
    assert "128000" in str(error.value)
    assert "invalid_value" in str(error.value)
    assert "deployment-secret" not in str(error.value)
    assert error.value.code == f"HTTP_{status}"
