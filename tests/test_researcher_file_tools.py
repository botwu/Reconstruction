"""选定来源的拼接和当前候选编辑必须保留原始证据并拒绝冲突。"""

import copy
import json

import pytest

from traceforge.reconstruction.agents.session import AgentSession, execute_tool
from traceforge.reconstruction.capture_repair import capture_repair_error


def session(tmp_path):
    original = "title=\"乱码\"\nvalue=1\n"
    evidence = [
        {"evidence_ref_id": ref, "session_parse": {"file_ops": [{
            "kind": "read", "path": "source.py", "line_numbers": numbers,
            "line_contents": lines, "content": "\n".join(lines) + "\n",
        }]} }
        for ref, numbers, lines in [
            ("first", [1, 2], ['title="正确"', "value=1"]),
            ("tail", [2, 3], ["value=1", "print(value)"]),
        ]
    ]
    return AgentSession(workspace=tmp_path, replay_files={"source.py": original},
                        partial_files={"source.py": original}, evidence=evidence, allow_write=True)


def test_restore_overlapping_observations_then_edit_current_candidate(tmp_path):
    state = session(tmp_path)
    original = state.replay_files["source.py"]
    result = execute_tool("restore_observed_file", {"path": "source.py", "evidence_ref_ids": ["first", "tail"]}, state)
    assert not result.startswith("error:"), result
    assert (tmp_path / "source.py").read_text() == 'title="正确"\nvalue=1\nprint(value)\n'
    result = execute_tool("edit_candidate_file", {
        "path": "source.py", "old_text": "value=1", "new_text": "value=2", "reason": "测试修复",
        "evidence_ref_ids": ["first"],
    }, state)
    assert not result.startswith("error:"), result
    written = state.writes[-1]
    assert written["content"].endswith("value=2\nprint(value)\n")
    assert set(written["evidence_ref_ids"]) == {"first", "tail"}
    assert capture_repair_error(original, written["content"], written["capture_repairs"]) is None
    assert state.replay_files["source.py"] == original


def test_conflicting_or_incomplete_observations_are_not_silently_chosen(tmp_path):
    state = session(tmp_path)
    state.evidence[1]["session_parse"]["file_ops"][0]["line_contents"][0] = "value=99"
    result = execute_tool("restore_observed_file", {"path": "source.py", "evidence_ref_ids": ["first", "tail"]}, state)
    assert result.startswith("error:") and "冲突" in result
    assert not state.writes
    result = execute_tool("restore_observed_file", {"path": "source.py", "evidence_ref_ids": ["tail"]}, state)
    assert result.startswith("error:") and "缺失" in result


def test_read_then_edit_windows_capture_preserves_original(tmp_path):
    state = session(tmp_path)
    original = state.replay_files["source.py"].replace("\n", "\r\n")
    state.replay_files["source.py"] = state.partial_files["source.py"] = original
    (tmp_path / "source.py").write_bytes(original.encode())
    visible = execute_tool("read_file", {"path": "source.py"}, state)
    result = execute_tool("edit_candidate_file", {
        "path": "source.py", "old_text": visible, "new_text": visible.replace("乱码", "正确"),
        "reason": "依据清晰观察恢复采集文本", "evidence_ref_ids": ["first"],
    }, state)
    assert not result.startswith("error:"), result
    assert execute_tool("read_file", {"path": "source.py"}, state) == 'title="正确"\nvalue=1\n'
    assert state.replay_files["source.py"] == original
    written = state.writes[-1]
    assert capture_repair_error(original, written["content"], written["capture_repairs"]) is None


def test_restore_complete_observation_without_partial_line_metadata(tmp_path):
    state = session(tmp_path)
    original = state.replay_files["source.py"]
    state.evidence[0]["session_parse"]["file_ops"] = [{
        "kind": "read", "path": "source.py", "partial": False,
        "content": 'title="正确"\r\nvalue=1\r\n',
    }]
    result = execute_tool("restore_observed_file", {
        "path": "source.py", "evidence_ref_ids": ["first"],
    }, state)
    assert not result.startswith("error:"), result
    assert (tmp_path / "source.py").read_text() == 'title="正确"\nvalue=1\n'
    assert state.replay_files["source.py"] == original


def test_source_scope_and_write_permissions_are_enforced(tmp_path):
    state = session(tmp_path)
    args = {"path": "source.py", "evidence_ref_ids": ["unavailable"]}
    assert execute_tool("restore_observed_file", args, state).startswith("error:")
    state.allow_write = False
    args["evidence_ref_ids"] = ["first"]
    assert execute_tool("restore_observed_file", args, state).startswith("error:")
    assert not state.writes


def test_rejected_overwrite_can_be_corrected_without_discarding_result(tmp_path):
    from dataclasses import replace

    from traceforge.reconstruction.agents import COMPLETION_ROLE
    from traceforge.reconstruction.agents.runtime import HermesNativeRuntime

    original = 'title="乱码"\n'
    state = AgentSession(replay_files={"source.py": original}, complete_files={"source.py": original},
                         protected_paths={"source.py"}, evidence=[{"evidence_ref_id": "first"}],
                         allow_write=True)

    class Native:
        def run_conversation(self, instruction, **kwargs):
            rejected = self._invoke_tool("write_file", {
                "path": "source.py", "content": 'title="正确"\n', "evidence_ref_ids": ["first"],
            }, "completion")
            assert "PROTECTED_FILE_OVERWRITE" in rejected
            assert not state.writes
            accepted = self._invoke_tool("edit_candidate_file", {
                "path": "source.py", "old_text": "乱码", "new_text": "正确", "reason": "恢复观察原文",
                "evidence_ref_ids": ["first"],
            }, "completion")
            assert not accepted.startswith("error:"), accepted
            return {"completed": True, "final_response": '{"decision":"READY"}', "api_calls": 1}

    runtime = HermesNativeRuntime(factory=lambda **_: Native(), base_url="https://example.test",
                                  api_key="unit", model_name="fixture", provider="gpt")
    role = replace(COMPLETION_ROLE, tools=(*COMPLETION_ROLE.tools, "edit_candidate_file"))
    result = runtime.run(role=role, instruction="按证据恢复初态", session=state, output_root=tmp_path)
    assert result.completed and result.payload == {"decision": "READY"}
    assert not state.policy_errors
    assert state.tool_events[0]["ok"] is False
    assert state.writes[-1]["content"] == 'title="正确"\n'
    assert state.replay_files["source.py"] == original


@pytest.mark.parametrize(("group", "event_eligible", "reason"), [
    ("reference_file_ops", True, "stale_prior_observation"),
    ("reference_file_ops", True, None),
    ("file_ops", False, "read_after_unparsed_mutation"),
])
def test_historical_file_read_matches_restore_eligibility(
    tmp_path, group, event_eligible, reason,
):
    state = session(tmp_path)
    record = state.evidence[0]
    observation = record["session_parse"]["file_ops"].pop()
    if reason:
        observation["initial_state_blockers"] = [
            {"reason": reason, "path": "source.py", "source_event_id": "first"},
            {"reason": "unrelated_reason", "path": "other.py", "source_event_id": "other"},
        ]
    record["initial_state_eligible"] = event_eligible
    record["session_parse"][group] = [observation]
    if group == "reference_file_ops":
        record["session_parse"]["file_ops"] = [
            {"kind": "read", "path": "other.py", "partial": False, "content": "x = 1\n"},
        ]
    original = copy.deepcopy(state.evidence)

    visible = json.loads(execute_tool(
        "read_evidence", {"id": "first", "path": "source.py"}, state,
    ))
    assert visible["initial_state_eligible"] is False
    assert visible["observations"] == [{**observation, "reference_only": True}]
    rejected = execute_tool("restore_observed_file", {
        "path": "source.py", "evidence_ref_ids": ["first"],
    }, state)
    assert rejected.startswith("error:")
    assert "read_evidence" in rejected and "first" in rejected and "source.py" in rejected
    assert "unrelated_reason" not in rejected
    if reason:
        assert reason in rejected
    else:
        assert "stale_prior_observation" not in rejected
    assert not state.writes and not (tmp_path / "source.py").exists()
    assert state.evidence == original


def test_current_observation_restores_without_using_historical_content(tmp_path):
    state = session(tmp_path)
    state.evidence[0]["session_parse"]["reference_file_ops"] = [{
        "kind": "read", "path": "source.py", "partial": False,
        "content": "不可用于恢复的历史版本\n",
    }]
    visible = json.loads(execute_tool(
        "read_evidence", {"id": "first", "path": "source.py"}, state,
    ))
    assert visible["initial_state_eligible"] is True
    assert "reference_only" not in visible["observations"][0]
    assert visible["observations"][1]["reference_only"] is True
    restored = execute_tool("restore_observed_file", {
        "path": "source.py", "evidence_ref_ids": ["first"],
    }, state)
    assert not restored.startswith("error:"), restored
    assert (tmp_path / "source.py").read_text() == 'title="正确"\nvalue=1\n'
