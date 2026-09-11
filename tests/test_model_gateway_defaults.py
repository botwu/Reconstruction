def test_deepseek_config_default_is_a_real_model():
    from traceforge.reconstruction.model_gateway import resolve_model_name
    assert resolve_model_name("claude-opus-4-8", config_path="config.yaml", channel="deepseek") == "vol/deepseek-v4-flash-0731"
