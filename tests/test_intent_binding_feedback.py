"""Intent 绑定反馈的合成回归；不调用模型、AGS 或真实会话。"""

from __future__ import annotations

import copy
import json

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


@pytest.mark.parametrize("change", [
    "downgrade", "drop_obligation", "task_id", "obligation_id",
    "evidence_ref_ids", "has_examples",
    "response_contract", "specified_output_format",
])
def test_binding_correction_cannot_change_task_or_downgrade_file(tmp_path, change):
    corrected = _payload(bound=True)
    if change == "downgrade":
        corrected["environment_bindings"][0].update(
            verifier_kind="NON_FILE", required_paths=[], initial_required_paths=[],
        )
    elif change == "drop_obligation":
        corrected["acceptance_obligations"] = []
    elif change == "obligation_id":
        corrected["acceptance_obligations"][0]["id"] = "obl-other"
    elif change == "evidence_ref_ids":
        corrected["acceptance_obligations"][0]["evidence_ref_ids"] = ["user:1"]
    elif change == "task_id":
        corrected["task_id"] = "task-other"
    else:
        corrected[change] = ["新增要求"]
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


def test_unrequested_output_name_is_corrected_across_task_fields(tmp_path):
    source = _source()
    source["raw_session"]["messages"][0]["content"] = (
        "把 src/importer.py 的表头逻辑提取到一个新 py 文件供原模块调用，并分析代码质量。"
    )
    source["raw_session"]["messages"][1]["content"] = "我打算创建 src/header_helper.py。"
    source["tool_timeline"] = [{
        "name": "write_file", "arguments": {"path": "src/header_helper.py"},
    }]
    first = _payload(bound=True)
    first["task_instruction"] = (
        "把 src/importer.py 的表头逻辑提取到 src/header_helper.py，接入调用并分析代码质量。"
    )
    first["core_objective"] = "提取到 src/header_helper.py 并分析代码质量"
    first["success_criteria"] = ["src/header_helper.py 的表头逻辑由原模块调用"]
    first["mandatory_constraints"] = ["新增文件必须命名为 src/header_helper.py"]
    first["prohibitions"] = ["不能使用 src/header_helper.py 之外的文件名"]
    first["acceptance_obligations"][0]["text"] = "新增 src/header_helper.py 并接入原模块调用"
    first["acceptance_obligations"].append({
        "id": "obl-002", "text": "分析代码质量", "evidence_ref_ids": ["user:0"],
    })
    first["environment_bindings"][0].update(
        required_paths=["src/importer.py", "src/header_helper.py"],
        output_paths=["src/header_helper.py"],
        observable="新增 src/header_helper.py 并接入原模块调用",
    )
    first["environment_bindings"].append({
        "obligation_id": "obl-002", "verifier_kind": "NON_FILE",
        "required_paths": [], "initial_required_paths": [], "output_paths": [],
        "observable": "回答包含对原代码质量的分析",
    })
    corrected = copy.deepcopy(first)
    corrected["core_objective"] = "提取表头逻辑并分析代码质量"
    corrected["success_criteria"] = ["新 Python 文件的表头逻辑由原模块调用"]
    corrected["mandatory_constraints"] = []
    corrected["prohibitions"] = []
    corrected["acceptance_obligations"][0]["text"] = "新增独立 Python 文件并接入原模块调用"
    corrected["task_instruction"] = (
        "把 src/importer.py 的表头逻辑提取到一个新 Python 文件，接入调用并分析代码质量。"
    )
    corrected["environment_bindings"][0].update(
        required_paths=["src/importer.py"], output_paths=[],
        observable="新增独立 Python 文件并接入原模块调用；用户未指定新文件名",
    )
    original_source = copy.deepcopy(source)
    outcome, runtime = _run(
        tmp_path, [(first, [], True), (corrected, [], True)], source=source,
    )
    assert outcome["status"] == "READY"
    assert outcome["errors"] == []
    assert len(runtime.calls) == 2
    assert "BINDING_OUTPUT_PATH_NOT_EXPLICIT:obl-001:src/header_helper.py" in (
        outcome["tasks"][0]["agent"]["attempts"][0]["errors"]
    )
    for field in ("task_instruction", "core_objective", "acceptance_obligations",
                  "success_criteria", "mandatory_constraints", "prohibitions"):
        assert "src/header_helper.py" not in json.dumps(outcome["task"][field])
    assert "分析代码质量" in outcome["task"]["task_instruction"]
    assert [item["id"] for item in outcome["task"]["acceptance_obligations"]] == [
        "obl-001", "obl-002",
    ]
    assert outcome["task"]["environment_bindings"][0]["verifier_kind"] == "FILE"
    assert source == original_source


@pytest.mark.parametrize("mutation", ["reorder", "duplicate", "change_valid_evidence"])
def test_binding_repair_preserves_obligation_identity_and_source(tmp_path, mutation):
    source = _source()
    source["tasks"][0]["message_indices"] = [0, 2]
    source["raw_session"]["messages"].append({"role": "user", "content": "同时分析代码质量"})
    first = _payload()
    first["acceptance_obligations"].append({
        "id": "obl-002", "text": "分析代码质量", "evidence_ref_ids": ["user:2"],
    })
    first["environment_bindings"].append({
        "obligation_id": "obl-002", "verifier_kind": "NON_FILE",
        "required_paths": [], "observable": "包含代码质量分析",
    })
    corrected = copy.deepcopy(first)
    corrected["environment_bindings"][0] = _payload(bound=True)["environment_bindings"][0]
    if mutation == "reorder":
        corrected["acceptance_obligations"].reverse()
    elif mutation == "duplicate":
        corrected["acceptance_obligations"].append(copy.deepcopy(corrected["acceptance_obligations"][0]))
    else:
        corrected["acceptance_obligations"][0]["evidence_ref_ids"] = ["user:2"]
    outcome, _ = _run(tmp_path, [(first, [], True), (corrected, [], True)], source=source)
    assert outcome["status"] == "REVIEW"
    assert "INTENT_BINDING_REPAIR_CHANGED_TASK" in outcome["errors"]
