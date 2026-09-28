import pytest


@pytest.mark.parametrize("requested", [None, "claude-opus-4-8"])
def test_channel_model_field_overrides_default(tmp_path, requested):
    from traceforge.reconstruction.model_gateway import resolve_model_name

    config = tmp_path / "config.yaml"
    config.write_text(
        'deepseek:\n  {"key":"unit","url":"https://example","model":"bailian/deepseek-v4-flash-0731"}\n',
        encoding="utf-8",
    )
    assert (
        resolve_model_name(requested, config_path=config, channel="deepseek")
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


def test_parse_json_object_rejects_invalid_root_instead_of_nested_contract():
    import pytest

    from traceforge.reconstruction.model_gateway import ModelGatewayError, parse_json_object

    broken = (
        '{"task_instruction":"review","core_objective":"review","response_contract":{"checks":['
        '{"kind":"basic_summary","obligation_id":"o2","match_report":true"},'
        '{"kind":"acceptance_report","obligation_id":"o2","criterion_ids":["criterion-1"]}]}}'
    )
    fence = chr(96) * 3
    for wrapped in (broken, "结果如下:\n" + broken, fence + "json\n" + broken + "\n" + fence):
        with pytest.raises(ModelGatewayError) as error:
            parse_json_object(wrapped)
        assert error.value.code == "INVALID_JSON"


def test_parse_json_object_keeps_complete_valid_root_and_embedded_fence():
    import json

    from traceforge.reconstruction.model_gateway import parse_json_object

    payload = {"task_instruction": "Follow schema: " + chr(96) * 3 + "json\n{}\n" + chr(96) * 3,
               "response_contract": {"checks": [{"kind": "acceptance_report", "obligation_id": "o2"}]}}
    assert parse_json_object(json.dumps(payload)) == payload
