"""完整轨迹可供补全参考，未返回补丁不能直接成为初态文件证据。"""

import copy
import json
from pathlib import Path

import pytest

from traceforge.reconstruction import workspace_completion as completion
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.session import AgentSession, execute_tool
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.session_source import tool_timeline

PATCH_MARKER = "PENDING_PATCH_REFERENCE_ONLY"


def pending_patch(call_id="pending-patch"):
    return {
        "call_id": call_id,
        "name": "exec",
        "arguments": {
            "input": "const change = await tools.apply_patch("
            + json.dumps("*** Begin Patch\n*** Update File: excel.py\n+"
                         + PATCH_MARKER + "\n*** End Patch")
            + "); text(change);",
        },
        "pending": True,
        "result_text": None,
        "result_blocks": [],
        "tool_message_index": None,
    }


def observed_read(call_id="read-excel"):
    return {
        "call_id": call_id, "name": "read_file",
        "arguments": {"path": "excel.py", "limit": 10},
        "result_text": "VALUE = 1\n", "pending": False, "tool_message_index": 2,
    }


class EvidenceRuntime:
    backend = "fixture"
    model_name = "deterministic"

    def run(self, *, role, instruction, session, output_root):
        self.role = role
        self.instruction = instruction
        self.session = session
        return AgentResult(
            role=role.name, backend=self.backend, completed=True,
            payload={"candidates": [{
                "files": [], "dependencies": [], "runtime_constraints": [],
                "uncertainties": [], "decision": "REVIEW",
            }]},
        )


@pytest.mark.parametrize("origin", ["REPLAYED", "DEFAULT_EMPTY"])
@pytest.mark.parametrize("repair", [False, True])
def test_session_is_reference_but_pending_patch_is_not_initial_evidence(
    tmp_path: Path, origin: str, repair: bool,
):
    timeline = [observed_read(), pending_patch()]
    timeline[0]["tool_message_index"] = 3
    raw_session = {"messages": [
        {"role": "system", "content": "原环境声明只读，调用无返回时不能断言未执行。"},
        {"role": "user", "content": "原始上下文\n" * 1800 + "完整消息末尾"},
        {"role": "assistant", "content": "历史方案仅供参考"},
        {"role": "tool", "tool_call_id": "read-excel", "content": "VALUE = 1\n"},
        {"role": "assistant", "tool_calls": [pending_patch()]},
    ]}
    system_context = [{"message_indices": [0], "interpretation": "只读声明不证明调用结果。"}]
    source = {"tool_timeline": timeline, "raw_session": raw_session,
              "session_parser": {"system_context": system_context}}
    snapshot = copy.deepcopy(source)
    replay = replay_from_timeline(timeline)
    runtime = EvidenceRuntime()
    kwargs = dict(
        task={"core_objective": "修复 excel.py"},
        replay=replay, timeline=timeline, agent=runtime, env_origin=origin,
        output_root=tmp_path / "completion", source=source,
    )
    if repair:
        workspace = tmp_path / "seed"
        workspace.mkdir()
        output = completion.repair_workspace_completion(
            **kwargs, candidate={"workspace": str(workspace)},
            feedback={"missing": "excel.py 的未观察部分"},
        )
    else:
        output = completion.run_workspace_completion(**kwargs)
    assert runtime.session is not None
    assert {"read_session_message", "read_session_context"} <= set(runtime.role.tools)
    assert json.loads(runtime.session.session_context) == raw_session
    assert json.dumps(system_context, ensure_ascii=False) in runtime.instruction
    assert json.dumps([{"message_index": 0, "message": raw_session["messages"][0]}],
                      ensure_ascii=False) in runtime.instruction
    assert json.loads(execute_tool("read_session_message", {"index": 0}, runtime.session)) == (
        raw_session["messages"][0]
    )
    pending_message = execute_tool("read_session_message", {"index": 4}, runtime.session)
    assert json.loads(pending_message) == raw_session["messages"][4]
    assert PATCH_MARKER in pending_message
    original_text = json.dumps(raw_session["messages"][1], ensure_ascii=False)
    first_page = execute_tool("read_session_message", {"index": 1}, runtime.session)
    assert first_page.startswith(original_text[:8000])
    assert "use offset=8000" in first_page
    assert execute_tool("read_session_message", {"index": 1, "offset": 8000}, runtime.session) == (
        original_text[8000:]
    )
    assert execute_tool("read_session_context", {"limit": 80}, runtime.session).startswith(
        runtime.session.session_context[:80]
    )
    assert '"message_index": 4, "role": "assistant"' in runtime.instruction
    public_refs = {"read-excel"} | ({"task:q"} if origin == "DEFAULT_EMPTY" else set())
    assert set(output["evidence_ref_ids"]) == public_refs
    assert {row["evidence_ref_id"] for row in runtime.session.evidence} == public_refs
    assert "pending-patch" not in execute_tool("list_evidence", {}, runtime.session)
    assert execute_tool("read_evidence", {"id": "pending-patch"}, runtime.session) == (
        "error: unknown evidence_ref_id"
    )
    assert "VALUE = 1" in execute_tool("read_evidence", {"id": "read-excel"}, runtime.session)
    assert PATCH_MARKER not in json.dumps(runtime.session.evidence)
    assert PATCH_MARKER not in runtime.instruction
    assert "pending-patch" not in runtime.instruction
    assert execute_tool("write_file", {
        "path": "neighbor.py", "content": "VALUE = 2\n",
        "evidence_ref_ids": ["pending-patch"],
    }, runtime.session) == "error: unknown evidence_ref_id"
    assert source == snapshot
    assert PATCH_MARKER in timeline[1]["arguments"]["input"]
    cards = completion._hole_cards(replay, output["holes"], timeline)
    excel = next(card for card in cards if card["path"] == "excel.py")
    assert excel["excerpt_event_ids"] == ["read-excel"]
    assert set(excel["excerpt_event_ids"]) <= public_refs


def test_direct_evidence_and_excerpt_projection_also_excludes_pending():
    timeline = [observed_read(), pending_patch()]
    replay = replay_from_timeline(timeline)
    evidence = completion.timeline_evidence(timeline)
    session = AgentSession(evidence=evidence)
    assert [row["evidence_ref_id"] for row in evidence] == ["read-excel"]
    assert execute_tool("read_evidence", {"id": "pending-patch"}, session).startswith("error:")
    assert completion._excerpt_ids("excel.py", replay, timeline) == ["read-excel"]


def test_pending_duplicate_does_not_hide_valid_returned_call(tmp_path: Path):
    timeline = [pending_patch("same-call"), observed_read("same-call")]
    replay = replay_from_timeline(timeline)
    runtime = EvidenceRuntime()
    output = completion.run_workspace_completion(
        task={"core_objective": "修复 excel.py"}, replay=replay, timeline=timeline,
        agent=runtime, output_root=tmp_path / "completion",
    )
    assert output["evidence_ref_ids"] == ["same-call"]
    visible = json.loads(execute_tool("read_evidence", {"id": "same-call"}, runtime.session))
    assert visible["name"] == "read_file"
    assert visible["result_text"] == "VALUE = 1\n"
    assert PATCH_MARKER not in json.dumps(visible)
    assert runtime.session.session_context is None
    assert execute_tool("read_session_message", {"index": 0}, runtime.session) == (
        "error: session context unavailable"
    )


def test_normally_paired_call_remains_available_after_tool_returns():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "paired", "name": "read_file",
                            "arguments": {"path": "excel.py"}}],
        },
        {"role": "tool", "tool_call_id": "paired", "content": "VALUE = 1\n"},
    ]
    timeline = tool_timeline(messages, [])
    assert timeline[0]["pending"] is False
    evidence = completion.timeline_evidence(timeline)
    assert evidence[0]["evidence_ref_id"] == "paired"
    assert evidence[0]["result_text"] == "VALUE = 1"


def test_completion_evidence_preserves_source_values_without_mutating_timeline():
    event = observed_read()
    event["arguments"].update({"max_output_tokens": 4096, "token": "业务字段值"})
    event["result_text"] = "  token = original_value\napi_key=sk-fixture-only-value\n"
    event["result_blocks"] = [{"text": event["result_text"], "secret_count": 2}]
    timeline = [event, pending_patch()]
    before = copy.deepcopy(timeline)
    evidence = completion.timeline_evidence(timeline)
    assert evidence == [event | {"evidence_ref_id": "read-excel", "text": event["result_text"]}]
    evidence[0]["arguments"]["token"] = "changed"
    evidence[0]["result_blocks"][0]["text"] = "changed"
    assert timeline == before


def test_parsed_file_observation_is_read_without_other_parallel_results():
    observation = {"kind": "read", "path": "module.py", "content": "原文\r\n",
                   "partial": True, "content_ref": {"block_index": 2, "start_line": 161}}
    session = AgentSession(evidence=[{
        "evidence_ref_id": "parallel", "result_text": "其他命令的长输出",
        "session_parse": {"reason": "UTF8 后段读取", "file_ops": [observation]},
    }])
    result = execute_tool("read_evidence", {"id": "parallel", "path": "module.py"}, session)
    assert json.loads(result) == {"evidence_ref_id": "parallel", "observations": [observation]}
    missing = execute_tool("read_evidence", {"id": "parallel", "path": "missing.py"}, session)
    assert missing.startswith("error:")
