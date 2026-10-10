"""原图工具必须原样交付多模态结果，并保留完整回执。"""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from traceforge.reconstruction.agents.roles import INTENT_ROLE
from traceforge.reconstruction.agents.runtime import _bind_agent_tools, load_tool_results
from traceforge.reconstruction.agents.session import AgentSession, execute_tool, tool_schemas


def image_result():
    return {
        "_multimodal": True,
        "text_summary": '{"source_url":"https://example.org/rate.jpeg","raw_sha256":"来源哈希"}',
        "meta": {"source_url": "https://example.org/rate.jpeg", "raw_sha256": "来源哈希"},
        "content": [
            {"type": "text", "text": "来源信息，未识别图片文字"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/2Q=="}},
        ],
    }


def bound_agent(tmp_path, handler, *, vision=True, keep_images=True, tools=("view_image",)):
    session = AgentSession(view_image_handler=handler)
    agent = SimpleNamespace(
        _model_supports_vision=lambda: vision,
        _tool_result_content_for_active_model=lambda name, result: (
            result["content"] if keep_images else result["text_summary"]
        ),
    )
    _bind_agent_tools(
        agent, role=replace(INTENT_ROLE, tools=tools), session=session,
        trace_path=tmp_path / "private/tool_events.jsonl",
    )
    return agent, session


def call_image(agent):
    return agent._invoke_tool(
        "view_image", {"url": "https://example.org/rate.jpeg"}, "intent", "image-1",
    )


def test_session_exposes_only_url_and_preserves_original_native_result():
    schema = tool_schemas(("view_image",))[0]["function"]
    assert schema["parameters"]["properties"] == {"url": {"type": "string"}}
    assert schema["parameters"]["required"] == ["url"]
    original = image_result()
    handler = Mock(return_value=original)
    assert execute_tool("view_image", {"url": "https://example.org/rate.jpeg"},
                        AgentSession(view_image_handler=handler)) is original
    handler.assert_called_once_with("https://example.org/rate.jpeg")


@pytest.mark.parametrize("result", [
    "error: 原图哈希不匹配", {"success": True, "text": "只有文字，没有原图"},
    {"_multimodal": True, "content": [{"type": "text", "text": "只有摘要"}]},
])
def test_image_errors_or_text_only_results_cannot_be_success(tmp_path, result):
    agent, session = bound_agent(tmp_path, lambda url: result)
    output = call_image(agent)
    assert isinstance(output, str) and output.startswith("error:")
    if isinstance(result, str):
        assert output == result
    assert session.tool_events[0]["ok"] is False
    assert load_tool_results(tmp_path, session.tool_events)[0]["result"] == output


def test_missing_image_handler_is_explicit_failure():
    result = execute_tool("view_image", {"url": "https://example.org/rate.jpeg"}, AgentSession())
    assert isinstance(result, str) and result.startswith("error:")


def test_native_image_trace_roundtrip_and_image_tampering(tmp_path):
    original = image_result()
    agent, session = bound_agent(tmp_path, lambda url: original)
    assert call_image(agent) is original
    assert session.tool_events[0]["ok"] is True
    assert "base64" not in session.tool_events[0]["result_preview"]
    bound = load_tool_results(tmp_path, session.tool_events)
    assert bound[0]["result"] == original
    path = tmp_path / "private/tool_events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[-1]["result"]["content"][1]["image_url"]["url"] += "AA"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    with pytest.raises(ValueError, match="正文哈希"):
        load_tool_results(tmp_path, session.tool_events)


@pytest.mark.parametrize("vision,keep_images", [(False, True), (True, False)])
def test_nonvision_or_text_fallback_is_explicit_failure(tmp_path, vision, keep_images):
    handler = Mock(return_value=image_result())
    agent, session = bound_agent(tmp_path, handler, vision=vision, keep_images=keep_images)
    assert call_image(agent).startswith("error:")
    assert session.tool_events[0]["ok"] is False
    if not vision:
        handler.assert_not_called()


def test_image_tool_does_not_bypass_role_allowlist(tmp_path):
    handler = Mock(return_value=image_result())
    agent, session = bound_agent(tmp_path, handler, tools=("list_user_texts",))
    assert call_image(agent).startswith("error:")
    assert session.policy_errors == ["TOOL_NOT_ALLOWED:view_image"]
    handler.assert_not_called()


def test_native_receipt_hash_does_not_depend_on_json_object_key_order(tmp_path):
    original = image_result()
    agent, session = bound_agent(tmp_path, lambda url: original)
    call_image(agent)
    path = tmp_path / "private/tool_events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[-1]["result"] = dict(reversed(list(copy.deepcopy(original).items())))
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    assert load_tool_results(tmp_path, session.tool_events)[0]["result"] == original
