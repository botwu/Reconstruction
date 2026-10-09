"""验证实际 Hermes 压缩可行性检查与辅助 SDK 调用，不访问网络。"""

import json
from types import SimpleNamespace

import pytest

from traceforge.reconstruction.agents.runtime import pin_hermes_compression


@pytest.mark.parametrize("aux_model,aux_endpoint,same_route", [
    ("gpt-6-astra/azure/sfa", "https://tokenhub.example/v1/", True),
    ("smaller-model", "https://tokenhub.example/v1/", False),
    ("gpt-6-astra/azure/sfa", "https://other.example/v1/", False),
])
def test_native_compression_keeps_own_window_and_role_timeout(
    monkeypatch, aux_model, aux_endpoint, same_route,
):
    auxiliary = pytest.importorskip("agent.auxiliary_client")
    compression = pytest.importorskip("agent.conversation_compression")
    metadata = pytest.importorskip("agent.model_metadata")
    httpx = pytest.importorskip("httpx")
    openai = pytest.importorskip("openai")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "fixture-summary", "object": "chat.completion", "created": 0,
            "model": aux_model, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "完整摘要"},
                 "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    client = openai.OpenAI(
        api_key="fixture", base_url=aux_endpoint,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(auxiliary, "_get_auxiliary_task_config", lambda task: {
        "provider": "custom", "model": aux_model, "base_url": aux_endpoint, "timeout": 30,
    })
    monkeypatch.setattr(auxiliary, "get_text_auxiliary_client",
                        lambda *args, **kwargs: (client, aux_model))
    monkeypatch.setattr(auxiliary, "_get_cached_client",
                        lambda *args, **kwargs: (client, aux_model))
    # 复现真实网关别名缺失元数据时返回256k，检查显式窗口是否到达原生入口。
    monkeypatch.setattr(metadata, "get_model_context_length",
                        lambda *args, **kwargs: kwargs.get("config_context_length") or 256_000)
    compressor = SimpleNamespace(
        model="gpt-6-astra/azure/sfa", provider="custom",
        base_url="https://tokenhub.example/v1", api_key="fixture",
        api_mode="chat_completions", context_length=1_000_000,
        threshold_tokens=500_000, threshold_percent=0.5,
    )
    runtime = {key: getattr(compressor, key)
               for key in ("model", "provider", "base_url", "api_key", "api_mode")}
    agent = SimpleNamespace(
        context_compressor=compressor, compression_enabled=True,
        _current_main_runtime=lambda: runtime, _custom_providers=[],
        _aux_compression_context_length_config=None, model=compressor.model,
        provider="custom", _emit_status=lambda *args: None,
    )
    original_timeout = auxiliary._get_task_timeout
    try:
        with pin_hermes_compression(agent, context_length=1_000_000, timeout_seconds=120):
            compression.check_compression_model_feasibility(agent)
            assert compressor.threshold_tokens == (500_000 if same_route else 256_000)
            response = auxiliary.call_llm(
                task="compression", main_runtime=runtime,
                messages=[{"role": "user", "content": "真实分支的固定测试输入"}],
            )
            assert response.choices[0].message.content == "完整摘要"
            assert json.loads(requests[-1].content)["model"] == aux_model
            assert requests[-1].extensions["timeout"]["read"] == 120
            assert auxiliary._get_task_timeout("web_extract") == 30
        assert auxiliary._get_task_timeout is original_timeout
        with pytest.raises(RuntimeError, match="测试中断"):
            with pin_hermes_compression(agent, context_length=None, timeout_seconds=90):
                raise RuntimeError("测试中断")
        assert auxiliary._get_task_timeout is original_timeout
    finally:
        client.close()


@pytest.mark.parametrize("wrapper,system_head", [("terminal", False), ("terminal", True), ("search", True)])
@pytest.mark.parametrize("summary_ok", [True, False])
def test_native_compaction_preserves_original_session_and_records_usage(
    tmp_path, monkeypatch, wrapper, system_head, summary_ok,
):
    native = pytest.importorskip("agent.context_compressor")
    from traceforge.reconstruction.agents import AgentSession, INTENT_ROLE
    from traceforge.reconstruction.agents.session import AgentConversation
    from traceforge.reconstruction.agents.runtime import HermesNativeRuntime
    from traceforge.reconstruction.session_source import indexed_session, source_session_message_indices

    raw_session = {"harness": {"tools": ["exec"]}, "messages": [
        {"role": "system", "content": "原始工具约定", "message_index": 27},
        {"role": "user", "content": "原始任务\n逐字保留空白  "},
        {"role": "tool", "content": {"raw": [False, None, "完整返回"]}},
    ]}
    original = indexed_session(raw_session)
    content = ("阶段提示\nSOURCE_SESSION=" + json.dumps(original, ensure_ascii=False)
               if wrapper == "terminal" else json.dumps({"SOURCE_SESSION": original}, ensure_ascii=False))
    history = ([{"role": "system", "content": "运行身份"}] if system_head else []) + [
        {"role": "user", "content": "早期提示"}, {"role": "assistant", "content": "早期计划"},
        {"role": "user", "content": "早期反馈"}, {"role": "assistant", "content": "早期响应"},
        {"role": "user", "content": "跨阶段反馈"}, {"role": "assistant", "content": "跨阶段响应"},
        {"role": "user", "content": content},
    ]
    for index in range(30):
        history.extend([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"tool-{index}", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"tool-{index}", "content": f"旧输出{index} " + "x " * 4000},
        ])
    history.append({"role": "assistant", "content": "当前可恢复检查点"})
    compressor = native.ContextCompressor(
        model="fixture", config_context_length=100_000, quiet_mode=True,
        abort_on_summary_failure=True,
    )
    summary_inputs = []

    def summarize(messages, **kwargs):
        summary_inputs.extend(messages)
        return "固定的旧工具摘要" if summary_ok else None

    monkeypatch.setattr(compressor, "_generate_summary", summarize)
    returned_usage = {"input_tokens": 301, "output_tokens": 7, "cache_read_tokens": 280,
                      "last_prompt_tokens": 99, "total_tokens": 308}
    instruction = "继续验证当前产物"

    class Native:
        context_compressor = compressor

        def _compress_context(self, messages, system_message, **kwargs):
            return self.context_compressor.compress(messages, force=True), system_message

        def run_conversation(self, current_instruction, **kwargs):
            messages = [*kwargs["conversation_history"], {"role": "user", "content": current_instruction}]
            compacted, _ = self._compress_context(messages, "运行身份")
            if summary_ok:
                assert compacted[-1]["role"] == "user"
                assert "摘要不是任务完成信号" in compacted[-1]["content"]
            return {"messages": compacted, "completed": True, "final_response": '{"ok":true}',
                    "api_calls": 1, **returned_usage}

    runtime = HermesNativeRuntime(
        factory=lambda **kwargs: Native(), base_url="https://example.test", api_key="fixture",
        provider="gpt", model_name="fixture",
    )
    conversation = AgentConversation(messages=history)
    result = runtime.run(role=INTENT_ROLE, instruction=instruction, output_root=tmp_path,
                         session=AgentSession(conversation=conversation,
                                              session_context=json.dumps(raw_session)))
    assert result.completed, result.errors
    indices = source_session_message_indices(conversation.messages, raw_session)
    assert indices and conversation.messages[indices[0]]["content"] == content
    assert summary_inputs and not source_session_message_indices(summary_inputs, raw_session)
    assert any(message.get("role") == "tool" for message in summary_inputs)
    if summary_ok:
        assert compressor.compression_count == 1
        assert len(conversation.messages) < len(history)
        assert not any(message.get("content") == summary_inputs[1].get("content")
                       for message in conversation.messages)
    else:
        assert compressor.compression_count == 0
        assert conversation.messages == [*history, {"role": "user", "content": instruction}]
    turn = json.loads((tmp_path / "private/agent_trace.json").read_text())["turns"][0]
    assert turn["source_history_protection"]["status"] == "PROTECTED"
    assert turn["source_history_protection"]["returned_source_message_indices"] == list(indices)
    assert turn["source_history_protection"]["native_protect_first_n"] == 7
    assert {key: turn["native_usage"][key] for key in returned_usage} == returned_usage
    assert "completion_tokens" not in turn["native_usage"]


def test_source_protection_requires_complete_matching_raw_context():
    from traceforge.reconstruction.agents.runtime import protect_source_history
    from traceforge.reconstruction.session_source import indexed_session, source_session_message_indices

    original = {"metadata": {"id": "fixture"}, "messages": [{"role": "user", "content": "任务原文"}]}
    altered = {"metadata": {"id": "fixture"}, "messages": [{"role": "user", "content": "摘要代替原文"}]}
    messages = [
        {"role": "user", "content": "SOURCE_SESSION={invalid"},
        {"role": "user", "content": json.dumps({"SOURCE_SESSION": indexed_session(altered)})},
        {"role": "assistant", "content": "SOURCE_SESSION=" + json.dumps(indexed_session(original))},
    ]
    compressor = SimpleNamespace(protect_first_n=3)
    assert source_session_message_indices(messages, original) == ()
    assert protect_source_history(compressor, messages, original)["status"] == "SOURCE_NOT_PRESENT"
    assert protect_source_history(compressor, messages, None)["status"] == "SOURCE_CONTEXT_UNKNOWN"
    assert compressor.protect_first_n == 3
    exact = {"role": "user", "content": [{"type": "text", "text": json.dumps(
        {"SOURCE_SESSION": indexed_session(original)}, indent=2)}]}
    assert source_session_message_indices([exact], original) == (0,)
    assert protect_source_history(None, [exact], original)["status"] == "NATIVE_COMPRESSOR_UNAVAILABLE"



@pytest.mark.parametrize("native_max_tokens,role_max_tokens,threshold,error", [
    (None, None, 850_000, None),
    (200_000, None, 800_000, None),
    (None, 300_000, 700_000, None),
    (1_000_000, None, None, "CONTEXT_BUDGET_EXHAUSTED"),
])
def test_explicit_window_reserves_output_without_compacting_large_fixed_prefix(
    tmp_path, monkeypatch, native_max_tokens, role_max_tokens, threshold, error,
):
    from dataclasses import replace

    from traceforge.reconstruction.agents import SUFFICIENCY_ROLE, AgentSession
    from traceforge.reconstruction.agents.runtime import HermesNativeRuntime

    native = pytest.importorskip("agent.context_compressor")
    auxiliary = pytest.importorskip("agent.auxiliary_client")
    compressor = native.ContextCompressor(
        model="fixture", config_context_length=1_000_000, quiet_mode=True,
    )
    monkeypatch.setattr(auxiliary, "get_text_auxiliary_client", lambda *a, **kw: (None, ""))
    calls = []

    class Native:
        context_compressor = compressor
        max_tokens = native_max_tokens

        def run_conversation(self, instruction, **kwargs):
            calls.append(instruction)
            # 真实故障的主请求使用量：原生 50% 会触发，修复后应继续使用显式 1M。
            assert not compressor.should_compress(542_554)
            return {"messages": [{"role": "user", "content": instruction}], "completed": True,
                    "final_response": '{"ok":true}', "api_calls": 1}

    runtime = HermesNativeRuntime(
        factory=lambda **kwargs: Native(), base_url="https://example.test", api_key="fixture",
        provider="gpt", model_name="fixture", agent_context_length=1_000_000,
    )
    result = runtime.run(
        role=replace(SUFFICIENCY_ROLE, max_output_tokens=role_max_tokens),
        instruction="完整受保护阶段请求", session=AgentSession(), output_root=tmp_path,
    )
    if error:
        assert not result.completed and result.errors == [error] and calls == []
    else:
        assert result.completed, result.errors
        assert compressor.context_length == 1_000_000
        assert compressor.threshold_tokens == threshold
        assert compressor.tail_token_budget == int(threshold * compressor.summary_target_ratio)
        assert compressor.should_compress(threshold)
        assert result.turns[0]["resolved_max_output_tokens"] == native_max_tokens


@pytest.mark.parametrize("mode,error", [
    ("compressed", None), ("noop", None), ("aborted", None),
    ("instruction_lost", "ACTIVE_INSTRUCTION_LOST"), ("source_lost", "SOURCE_CONTEXT_LOST"),
])
def test_compression_continuation_checks_complete_context_and_restores_instance(
    monkeypatch, mode, error,
):
    import hashlib

    from traceforge.reconstruction.agents import INTENT_ROLE
    from traceforge.reconstruction.model_gateway import ModelGatewayError
    from traceforge.reconstruction.session_source import indexed_session

    auxiliary = pytest.importorskip("agent.auxiliary_client")
    original_timeout = auxiliary._get_task_timeout
    raw = {"messages": [{"role": "user", "content": "源轨迹原文"}]}
    source = {"role": "user", "content": "SOURCE_SESSION=" + json.dumps(indexed_session(raw))}
    instruction = "完整活动请求：按每项义务检查，不得遗漏"
    messages = [source, {"role": "user", "content": [{"type": "text", "text": instruction}]}]
    compressor = SimpleNamespace(compression_count=0)

    class Native:
        context_compressor = compressor

        def _compress_context(self, messages, system_message, **kwargs):
            if mode in {"noop", "aborted"}:
                return messages, system_message
            compressor.compression_count += 1
            retained = list(messages)
            if mode == "instruction_lost":
                retained = retained[:-1]
            if mode == "source_lost":
                retained = retained[1:]
            # 即使摘要宣称旧请求已结束，也必须在它后面明确激活当前完整请求。
            return [*retained, {"role": "assistant", "content":
                    "Respond ONLY to latest user message AFTER this summary."}], system_message

    agent = Native()
    original_function = agent._compress_context.__func__
    with pin_hermes_compression(
        agent, context_length=None, timeout_seconds=90,
        instruction=instruction, role=INTENT_ROLE, raw_session=raw,
    ) as receipts:
        if error:
            with pytest.raises(ModelGatewayError) as exc:
                agent._compress_context(messages, "identity")
            assert exc.value.code == error
        else:
            output, system = agent._compress_context(messages, "identity")
            assert system == "identity"
            if mode == "compressed":
                assert output[-1]["role"] == "user"
                assert "摘要不是任务完成信号" in output[-1]["content"]
                assert hashlib.sha256(instruction.encode()).hexdigest() in output[-1]["content"]
                assert instruction not in output[-1]["content"]  # 不重复原文
                assert len(receipts) == 1
            else:
                assert output is messages and receipts == []
    assert "_compress_context" not in vars(agent)
    assert agent._compress_context.__func__ is original_function
    assert auxiliary._get_task_timeout is original_timeout


def test_each_stage_and_json_correction_keep_their_own_active_instruction(monkeypatch):
    import hashlib

    from traceforge.reconstruction.agents import INTENT_ROLE

    pytest.importorskip("agent.auxiliary_client")
    compressor = SimpleNamespace(compression_count=0)
    agent = SimpleNamespace(context_compressor=compressor)

    def compress(messages, system_message, **kwargs):
        compressor.compression_count += 1
        return [*messages, {"role": "assistant", "content": "CONTEXT SUMMARY"}], system_message

    agent._compress_context = compress
    messages = []
    instructions = ["初始解析完整轨迹", "仅补正上一条 JSON；不要重新执行任务或调用工具"]
    for instruction in instructions:
        messages.append({"role": "user", "content": instruction})
        with pin_hermes_compression(
            agent, context_length=None, timeout_seconds=90,
            instruction=instruction, role=INTENT_ROLE,
        ) as receipts:
            messages, _ = agent._compress_context(messages, "identity")
            digest = hashlib.sha256(instruction.encode()).hexdigest()
            assert receipts[-1]["instruction_sha256"] == digest
            assert digest in messages[-1]["content"]
        assert agent._compress_context is compress
    assert hashlib.sha256(instructions[0].encode()).hexdigest() not in messages[-1]["content"]
