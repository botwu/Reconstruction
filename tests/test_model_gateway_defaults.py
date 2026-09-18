def test_deepseek_config_default_is_a_real_model():
    from traceforge.reconstruction.model_gateway import resolve_model_name

    assert (
        resolve_model_name("claude-opus-4-8", config_path="config.yaml", channel="deepseek")
        == "bailian/deepseek-v4-flash-0731"
    )


def test_channel_model_field_overrides_default(tmp_path):
    from traceforge.reconstruction.model_gateway import resolve_model_name

    config = tmp_path / "config.yaml"
    config.write_text(
        'deepseek:\n  {"key":"unit","url":"https://example","model":"bailian/deepseek-v4-flash-0731"}\n',
        encoding="utf-8",
    )
    assert (
        resolve_model_name(None, config_path=config, channel="deepseek")
        == "bailian/deepseek-v4-flash-0731"
    )


def test_claude_channel_keeps_requested_opus_name():
    from traceforge.reconstruction.model_gateway import resolve_model_name

    assert (
        resolve_model_name(
            "claude-opus-4-6", config_path="config.yaml", channel="claude"
        )
        == "claude-opus-4-6"
    )


def test_parse_json_object_accepts_preamble_and_fence():
    from traceforge.reconstruction.model_gateway import parse_json_object

    fence = chr(96) * 3
    assert parse_json_object('Looking now\n{"a": 1}')["a"] == 1
    assert parse_json_object(f"{fence}json\n{{\"a\": 2}}\n{fence}")["a"] == 2


def test_parse_json_object_skips_stub_example_before_role_payload():
    from traceforge.reconstruction.model_gateway import parse_json_object

    fence = chr(96) * 3
    text = (
        'package.json stub — `{"_comment": "observed name, body unobserved"}`.\n'
        f"{fence}json\n"
        '{"label":"INSUFFICIENT","missing_context":["n8n"],"decision":"REVIEW"}\n'
        f"{fence}"
    )
    payload = parse_json_object(text)
    assert payload["label"] == "INSUFFICIENT"
    assert payload["missing_context"] == ["n8n"]
