"""未返回的未来补丁不得进入 Completion 可访问的任何公开证据入口。"""

import copy
import json
from pathlib import Path

import pytest

from traceforge.reconstruction import workspace_completion as completion
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.session import AgentSession, execute_tool
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.session_source import tool_timeline

PATCH_MARKER = "FUTURE_PATCH_MUST_STAY_PRIVATE"


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
def test_pending_patch_is_not_reachable_from_completion(tmp_path: Path, origin: str):
    timeline = [observed_read(), pending_patch()]
    private_snapshot = copy.deepcopy(timeline)
    replay = replay_from_timeline(timeline)
    runtime = EvidenceRuntime()
    output = completion.run_workspace_completion(
        task={"core_objective": "修复 excel.py"},
        replay=replay, timeline=timeline, agent=runtime, env_origin=origin,
        output_root=tmp_path / "completion",
        source={"tool_timeline": timeline, "raw_session": {"private_patch": PATCH_MARKER}},
    )
    assert runtime.session is not None
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
    assert timeline == private_snapshot
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
