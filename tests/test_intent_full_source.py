"""意图输入主动保留全轨迹，不依赖模型猜测原消息索引。"""

import json
from copy import deepcopy

from traceforge.reconstruction.agents.roles import INTENT_ROLE
from traceforge.reconstruction.intent_recovery import _prompt
from traceforge.reconstruction.session_source import indexed_session, source_session_message_indices


def _raw_session() -> dict:
    return {
        "tools": [
            {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}},
        ],
        "meta": {"harness": "原框架", "timestamp": "原时间"},
        "messages": [
            {"role": "system", "content": "原系统约束", "message_index": 99},
            {"role": "user", "content": "美化这个弹窗"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "read", "arguments": {"path": "ui/Dialog.vue"}},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": [
                {"type": "text", "text": "  <Dialog title='选择审批人'/>\\n"},
                {"type": "image_url", "image_url": {"url": "attachment://原截图"}},
            ], "is_error": False},
            {"role": "assistant", "content": "后续说明定位 ui/Dialog.vue，建议布局仅是历史方案。"},
            {"role": "user", "content": "另一项无关任务，不能合并为当前目标"},
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": "native-1", "name": "Read",
                "input": {"file_path": "other.txt"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "native-1",
                "content": "原始失败返回", "is_error": True,
            }]},
        ],
    }


def _instruction(raw: dict) -> str:
    return _prompt(
        {"raw_session": raw},
        {"task_id": "selected", "message_indices": [1]},
        [{"id": "user:1", "message_index": 1, "text": "美化这个弹窗"}],
        ["ui/Dialog.vue"], ["ui/Dialog.vue"],
    )


def test_prompt_preserves_full_source_fields_and_native_tool_pairs() -> None:
    raw = _raw_session()
    before = deepcopy(raw)
    prompt = _instruction(raw)
    lines = [line for line in prompt.splitlines() if line.startswith("SOURCE_SESSION=")]
    assert len(lines) == 1
    delivered = json.loads(lines[0].removeprefix("SOURCE_SESSION="))
    assert delivered == indexed_session(raw)
    assert delivered["messages"][0]["message_index"] == 0
    assert delivered["messages"][0]["message"]["message_index"] == 99
    assert delivered["messages"][2]["message"]["tool_calls"][0]["id"] == "call-1"
    assert delivered["messages"][3]["message"]["tool_call_id"] == "call-1"
    assert delivered["messages"][6]["message"]["content"][0]["id"] == "native-1"
    assert delivered["messages"][7]["message"]["content"][0]["tool_use_id"] == "native-1"
    assert raw == before
    assert source_session_message_indices([{"role": "user", "content": prompt}], raw) == (0,)
    anchor = next(line for line in prompt.splitlines() if line.startswith("TASK_USER_MESSAGES="))
    assert [record["id"] for record in json.loads(anchor.split("=", 1)[1])] == ["user:1"]
    assert "TASK_ADJACENT_CONTEXT=" not in prompt
    assert "SOURCE_SYSTEM_MESSAGES=" not in prompt


def test_prompt_does_not_cut_long_late_messages_or_discourage_evidence_reading() -> None:
    raw = _raw_session()
    raw["messages"][-1]["content"][0]["content"] = "完整返回" * 3000 + "末尾独有证据"
    prompt = _instruction(raw)
    assert "末尾独有证据" in prompt
    assert "不要穷举读取工具输出、调查实现细节" not in prompt
    assert "意图明确后立即提交 JSON" not in prompt


def test_historical_input_policy_is_in_system_identity_and_preserves_source() -> None:
    raw = {
        "messages": [
            {"role": "system", "content": "确需看图时，调用历史媒体工具；文字不能冒充看图。"},
            {"role": "user", "content": "比较附件里的两家报价，给出适用条件。"},
            {"role": "assistant", "content": (
                "图中甲报价 10，乙报价 12。我推荐甲，假设订单重 1kg。"
            )},
            {"role": "user", "content": "另一任务：逐项核验原图的数字，不能只使用转述。"},
        ],
    }
    prompt = _prompt(
        {"raw_session": raw},
        {"task_id": "compare", "message_indices": [1]},
        [{"id": "user:1", "message_index": 1, "text": raw["messages"][1]["content"]}],
        [],
    )
    delivered = json.loads(next(
        line.removeprefix("SOURCE_SESSION=")
        for line in prompt.splitlines() if line.startswith("SOURCE_SESSION=")
    ))
    assert delivered == indexed_session(raw)
    identity = INTENT_ROLE.identity
    assert "输入记述可作为有条件分析的输入" in identity
    assert "条件性分析的完成不等于原件真实性已核验" in identity
    assert "明确要求识图、视觉比较或核验原件" in identity
    assert "历史排名、推荐、推导结果和无依据假设" in identity
    assert "user requirements, values" not in identity
    assert "输入记述可作为有条件分析的输入" not in prompt
    assert "历史回答中可逐字定位的输入记述与其分析结论分开" not in prompt
