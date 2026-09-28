"""Intent 绑定反馈的合成回归；不调用模型、AGS 或真实会话。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from traceforge.reconstruction.agents import INTENT_ROLE
from traceforge.reconstruction.agents.runtime import AgentResult, write_agent_trace
from traceforge.reconstruction.agents.session import execute_tool, tool_schemas
from traceforge.reconstruction.intent_recovery import run_intent_recovery


def _source():
    return {
        "entry_mode": "RAW_SESSION",
        "tasks": [{"task_id": "task-header", "intake_selected": True, "message_indices": [0]}],
        "raw_session": {"messages": [
            {"role": "user", "content": "让导入器识别带附加文字的表头"},
            {"role": "assistant", "content": "表头逻辑位于 src/importer.py。"},
        ]},
        "tool_timeline": [],
    }


def _payload(*, bound=False):
    paths = ["src/importer.py"] if bound else []
    return {
        "task_id": "task-header",
        "task_instruction": "让导入器识别带附加文字的表头",
        "core_objective": "改进已有导入器的表头识别",
        "acceptance_obligations": [{
            "id": "obl-001", "text": "带附加文字的表头仍能识别",
            "evidence_ref_ids": ["user:0"],
        }],
        "environment_bindings": [{
            "obligation_id": "obl-001", "verifier_kind": "FILE",
            "required_paths": paths, "initial_required_paths": paths,
            "output_paths": [], "observable": "导入带附加文字的表头时映射正确",
        }],
    }


class SequenceRuntime:
    backend = "fixture"
    model_name = "fixture"

    def __init__(self, results):
        self.results = results
        self.calls = []

    def run(self, *, role, instruction, session, output_root):
        index = len(self.calls)
        self.calls.append((instruction, session, output_root))
        payload, errors, completed = self.results[min(index, len(self.results) - 1)]
        if index:
            assert execute_tool("read_session_message", {"index": 1}, session).startswith('{"')
            assert session.user_records[0]["id"] == "user:0"
            assert session.policy_errors == []
        result = AgentResult(
            role=role.name, backend=self.backend, payload=copy.deepcopy(payload),
            final_text=json.dumps(payload, ensure_ascii=False), completed=completed,
            errors=list(errors), turns=[{"api_calls": 1, "completed": completed}],
        )
        write_agent_trace(
            output_root, role=role, backend=self.backend, instruction=instruction,
            turns=result.turns, final_text=result.final_text, model_name=self.model_name,
        )
        return result


def _run(tmp_path, results, *, source=None):
    runtime = SequenceRuntime(results)
    outcome = run_intent_recovery(
        source=source or _source(), agent=runtime, output_root=tmp_path,
        replay_files_by_task={"task-header": ["src/importer.py"]},
    )
    return outcome, runtime


def test_empty_file_binding_receives_one_targeted_correction(tmp_path):
    first, corrected = _payload(), _payload(bound=True)
    outcome, runtime = _run(tmp_path, [(first, [], True), (corrected, [], True)])
    assert outcome["status"] == "READY"
    assert len(runtime.calls) == 2
    repair_instruction = runtime.calls[1][0]
    assert "BINDING_FILE_PATHS_REQUIRED:obl-001" in repair_instruction
    assert "PREVIOUS_RESULT=" in repair_instruction
    assert "FILE_BINDING_PATHS=" in repair_instruction
    assert runtime.calls[0][1] is not runtime.calls[1][1]
    assert outcome["task"]["environment_bindings"][0]["required_paths"] == ["src/importer.py"]
    agent = outcome["tasks"][0]["agent"]
    assert agent["turns"] == 2
    assert [item["status"] for item in agent["attempts"]] == ["REVIEW", "READY"]
    assert agent["attempts"][0]["errors"] == ["BINDING_FILE_PATHS_REQUIRED:obl-001"]
    roots = [call[2] for call in runtime.calls]
    assert roots[0] != roots[1]
    for root, payload, instruction in zip(roots, (first, corrected), (c[0] for c in runtime.calls)):
        exchange = json.loads((root / "private/model_exchange.json").read_text())
        trace = json.loads((root / "private/agent_trace.json").read_text())
        assert json.loads(exchange["response"]["text"]) == payload
        assert exchange["request"]["prompt"] == instruction
        assert json.loads(trace["final_text"]) == payload


def test_unchanged_bad_binding_stops_after_one_correction(tmp_path):
    outcome, runtime = _run(tmp_path, [(_payload(), [], True)])
    assert len(runtime.calls) == 2
    assert outcome["status"] == "REVIEW"
    assert outcome["errors"] == ["BINDING_FILE_PATHS_REQUIRED:obl-001"]


@pytest.mark.parametrize("change", ["downgrade", "drop_obligation", "rewrite_goal"])
def test_binding_correction_cannot_change_task_or_downgrade_file(tmp_path, change):
    corrected = _payload(bound=True)
    if change == "downgrade":
        corrected["environment_bindings"][0].update(
            verifier_kind="NON_FILE", required_paths=[], initial_required_paths=[],
        )
    elif change == "drop_obligation":
        corrected["acceptance_obligations"] = []
    else:
        corrected["core_objective"] = "只需解释表头规则"
    outcome, runtime = _run(tmp_path, [(_payload(), [], True), (corrected, [], True)])
    assert len(runtime.calls) == 2
    assert outcome["status"] == "REVIEW"
    assert "INTENT_BINDING_REPAIR_CHANGED_TASK" in outcome["errors"]


@pytest.mark.parametrize("errors,completed", [
    (["MODEL_TIMEOUT"], True), (["TOOL_NOT_ALLOWED:terminal"], True),
    (["SANDBOX_INIT_FAILED"], False), ([], False),
])
def test_runtime_failures_are_not_retried_as_binding_errors(tmp_path, errors, completed):
    outcome, runtime = _run(tmp_path, [(_payload(), errors, completed)])
    assert len(runtime.calls) == 1
    assert outcome["status"] == "REVIEW"


def test_valid_contract_does_not_request_another_attempt(tmp_path):
    outcome, runtime = _run(tmp_path, [(_payload(bound=True), [], True)])
    assert outcome["status"] == "READY"
    assert len(runtime.calls) == 1


def test_missing_or_foreign_user_evidence_is_not_a_binding_retry(tmp_path):
    payload = _payload(bound=True)
    payload["acceptance_obligations"][0]["evidence_ref_ids"] = ["user:20"]
    outcome, runtime = _run(tmp_path, [(payload, [], True)])
    assert outcome["status"] == "REVIEW"
    assert len(runtime.calls) == 1


def test_context_tool_description_matches_intent_permissions():
    specs = {item["function"]["name"]: item["function"] for item in tool_schemas(INTENT_ROLE.tools)}
    assert "read_session_context" in specs
    assert "Disabled for Intent" not in specs["read_session_context"]["description"]
    assert "may not page" not in specs["read_session_message"]["description"]


@pytest.mark.parametrize("bad_id", [[], {}, ["obl-001"], {"id": "obl-001"}])
def test_malformed_binding_identifier_in_correction_remains_review(tmp_path, bad_id):
    corrected = _payload(bound=True)
    corrected["environment_bindings"][0]["obligation_id"] = bad_id
    outcome, runtime = _run(tmp_path, [(_payload(), [], True), (corrected, [], True)])
    assert len(runtime.calls) == 2
    assert outcome["status"] == "REVIEW"
    assert "BINDING_OBLIGATION_ID_INVALID" in outcome["errors"]


@pytest.mark.parametrize("bad_id", [["obl-001"], {"id": "obl-001"}])
def test_malformed_initial_binding_identifier_can_be_corrected(tmp_path, bad_id):
    first = _payload()
    first["environment_bindings"][0]["obligation_id"] = bad_id
    outcome, runtime = _run(tmp_path, [(first, [], True), (_payload(bound=True), [], True)])
    assert len(runtime.calls) == 2
    assert outcome["status"] == "READY"


def test_correction_can_remove_binding_with_no_corresponding_obligation(tmp_path):
    first = _payload(bound=True)
    extra = copy.deepcopy(first["environment_bindings"][0])
    extra["obligation_id"] = "unknown-obligation"
    first["environment_bindings"].append(extra)
    outcome, runtime = _run(tmp_path, [(first, [], True), (_payload(bound=True), [], True)])
    assert len(runtime.calls) == 2
    assert outcome["status"] == "READY"
    assert len(outcome["task"]["environment_bindings"]) == 1


def test_intent_identity_allows_grounding_without_inventing_requirements():
    assert "Do not inject paths" not in INTENT_ROLE.identity
    assert "Observed paths may identify the object of the existing user request" in INTENT_ROLE.identity
    assert "Do not turn agent actions into new user requirements" in INTENT_ROLE.identity
