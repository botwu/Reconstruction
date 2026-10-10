from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_fakes import raw_source

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.session import execute_tool
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.intent_recovery import run_intent_recovery
from traceforge.reconstruction.terminal_universe_environment import (
    ReplayResult,
    validate_completion_candidate,
)
from traceforge.reconstruction.workspace_completion import (
    run_workspace_completion,
    timeline_evidence,
)
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency
from traceforge.reconstruction.session_spans import build_spans


class ResultRuntime:
    model_name = "test-model"
    backend = "hermes-sandbox"

    def __init__(self, payload: dict[str, Any], *, completed: bool = True, errors=None):
        self.payload = payload
        self.completed = completed
        self.errors = errors or []
        self.instruction = ""
        self.session = None

    def run(self, *, role, instruction, session, output_root):
        self.instruction = instruction
        self.session = session
        return AgentResult(
            role=role.name,
            backend="hermes-sandbox",
            payload=self.payload,
            errors=list(self.errors),
            final_text=json.dumps(self.payload),
            completed=self.completed,
        )


def _payload() -> dict[str, Any]:
    return {
        "messages": [
            {"role": "user", "content": "修复 foo.py 的入口函数"},
            {"role": "user", "content": "补充限制：不要联网，保留原有接口"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {
                "name": "read_file", "arguments": {"path": "foo.py"}}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "first observation"},
            {"role": "user", "content": "另外做一个完全不同的任务：重构 bar.py 服务"},
            {"role": "assistant", "tool_calls": [{"id": "c2", "function": {
                "name": "browse_web", "arguments": {"url": "https://example.com"}}}]},
            {"role": "tool", "tool_call_id": "c2", "content": "late unselected context"},
        ],
        "tools": [{"name": "read_file"}, {"name": "browse_web"}],
        "meta": {"source": "real-session"},
    }


def _source(*, selected: list[int] | None = None) -> dict[str, Any]:
    payload = _payload()
    record = raw_source(json.dumps(payload), selected=selected, domain="")
    return record


def _candidate(files=None):
    return {"files": files or [], "dependencies": [], "runtime_constraints": [],
            "uncertainties": [], "decision": "READY"}


def test_raw_session_and_unselected_timeline_are_preserved() -> None:
    source = _source()
    assert source["schema_version"] == "traceforge.reconstruction-source.raw-session.v1"
    assert len(source["raw_session"]["messages"]) == 7
    assert source["raw_session"]["meta"] == {"source": "real-session"}
    assert [item["call_id"] for item in source["tool_timeline"]] == ["c1", "c2"]
    assert [item["call_id"] for item in source["selected_tool_timeline"]] == ["c1", "c2"]


def test_intent_keeps_selected_clarifications_and_drops_unselected_span(tmp_path: Path) -> None:
    source = _source()
    task = source["tasks"][0]
    runtime = ResultRuntime(
        {
            "task_id": task["task_id"],
            "task_instruction": "修复 foo.py 的入口函数，不要联网，保留原有接口",
            "core_objective": "修复入口",
            "acceptance_obligations": [
                {
                    "id": "obl-001",
                    "text": "入口工作",
                    "evidence_ref_ids": ["user:0"],
                }
            ],
            "environment_bindings": [{
                "obligation_id": "obl-001", "verifier_kind": "FILE",
                "required_paths": ["foo.py"], "initial_required_paths": ["foo.py"],
                "output_paths": [], "observable": "入口按用户要求工作",
            }],
            "success_criteria": ["入口工作"],
        }
    )
    result = run_intent_recovery(source=source, agent=runtime, output_root=tmp_path)
    assert result["status"] == "READY"
    assert "不要联网，保留原有接口" in runtime.instruction
    assert "重构 bar.py 服务" not in runtime.instruction
    # Tool names are session-level context; unselected tool result bodies stay hidden.
    assert "browse_web" in runtime.instruction
    assert "late unselected context" not in runtime.instruction
    assert "TASK_TAG" in runtime.instruction
    assert "TASK_USER_MESSAGES=" in runtime.instruction
    assert "FULL_SESSION_CONTEXT" not in runtime.instruction
    assert "raw_session" not in runtime.instruction


def test_intent_does_not_merge_other_selected_spans(tmp_path: Path) -> None:
    source = _source(selected=[0, 1])
    class MultiTaskIntentRuntime(ResultRuntime):
        def run(self, *, role, instruction, session, output_root):
            tag = json.loads(next(line.split("=", 1)[1] for line in instruction.splitlines() if line.startswith("TASK_TAG=")))
            records = json.loads(next(line.split("=", 1)[1] for line in instruction.splitlines() if line.startswith("TASK_USER_MESSAGES=")))
            path = "foo.py" if tag["task_id"] == source["tasks"][0]["task_id"] else "bar.py"
            payload = {
                "task_id": tag["task_id"],
                "task_instruction": "；".join(item["text"] for item in records),
                "core_objective": "按用户要求完成当前任务",
                "acceptance_obligations": [{"id": "obl-001", "text": "用户要求有明确交付", "evidence_ref_ids": [item["id"] for item in records]},],
                "environment_bindings": [{
                    "obligation_id": "obl-001", "verifier_kind": "FILE",
                    "required_paths": [path], "initial_required_paths": [path],
                    "output_paths": [], "observable": "指定源码按当前任务要求修改",
                }],
                "success_criteria": ["用户要求有明确交付"],
            }
            self.payload = payload
            return super().run(role=role, instruction=instruction, session=session, output_root=output_root)

    runtime = MultiTaskIntentRuntime({})
    result = run_intent_recovery(source=source, agent=runtime, output_root=tmp_path)
    assert result["status"] == "READY"
    assert len(result["tasks"]) == 2
    assert {item["task"]["task_id"] for item in result["tasks"]} == {task["task_id"] for task in source["tasks"]}
    assert "重构 bar.py 服务" in runtime.instruction
    missing_span = ResultRuntime(
        {
            "task_id": "wrong-task-id",
            "task_instruction": "修复 foo.py 的入口函数，不要联网，保留原有接口",
            "core_objective": "修复入口",
            "acceptance_obligations": [
                {
                    "id": "obl-001",
                    "text": "入口工作",
                    "evidence_ref_ids": ["user:0"],
                }
            ],
            "environment_bindings": [{
                "obligation_id": "obl-001", "verifier_kind": "FILE",
                "required_paths": ["foo.py"], "initial_required_paths": ["foo.py"],
                "output_paths": [], "observable": "入口按用户要求工作",
            }],
            "success_criteria": ["入口工作"],
        }
    )
    gated = run_intent_recovery(
        source=source, agent=missing_span, output_root=tmp_path / "no-span"
    )
    assert gated["status"] == "REVIEW"
    assert "TASK_ID_MISMATCH" in gated["errors"]


def test_completion_evidence_is_lossless_beyond_former_limits() -> None:
    timeline = [{"call_id": f"c{index}", "arguments": {"custom": "x" * 500},
                 "result_text": "y" * 10000, "pending": False} for index in range(25)]
    evidence = timeline_evidence(timeline)
    assert len(evidence) == 25
    assert evidence[-1]["text"] == "y" * 10000
    assert evidence[-1]["arguments"]["custom"] == "x" * 500
    duplicate = timeline_evidence([{}, {"call_id": "c1"}, {"call_id": "c1"}])
    assert len({item["evidence_ref_id"] for item in duplicate}) == 3


@pytest.mark.parametrize("env_origin", ["REPLAYED", "DEFAULT_EMPTY"])
@pytest.mark.parametrize("write_id", ["hidden-write", None])
def test_completion_excludes_hidden_output_and_post_write_evidence(
    tmp_path: Path, env_origin: str, write_id: str | None
) -> None:
    """新建隐藏报告及其重读不能经原 ID、重复 ID 或匿名 ID 进入补全。"""

    hidden_write = {
        "call_id": write_id,
        "name": "write",
        "arguments": {"path": "review.md", "content": "HIDDEN_REVIEW_PAYLOAD"},
        "result_text": "wrote review.md",
    }
    timeline = [
        {"call_id": "initial-read", "name": "read_file",
         "arguments": {"path": "src/context.py"}, "result_text": "INITIAL_SOURCE\n"},
        {"call_id": "initial-list", "name": "list_dir",
         "arguments": {"path": "src"}, "result_text": "context.py"},
        hidden_write,
        {"call_id": "post-read", "name": "read_file",
         "arguments": {"path": "review.md"}, "result_text": "HIDDEN_REVIEW_PAYLOAD"},
        dict(hidden_write),
        {"name": "lookup", "result_text": "ANONYMOUS_CONTEXT"},
        {"call_id": "duplicate", "name": "lookup", "result_text": "FIRST_CONTEXT"},
        {"call_id": "duplicate", "name": "lookup", "result_text": "SECOND_CONTEXT"},
        {"call_id": "later-initial-read", "name": "read_file",
         "arguments": {"path": "src/other.py"}, "result_text": "OTHER_INITIAL_SOURCE\n"},
    ]
    replay = replay_from_timeline(timeline)
    assert replay.withheld_changes[0].classification == "agent_created_file"
    assert any(item.get("reason") == "read_after_first_mutation"
               for item in replay.partial_evidence)
    runtime = ResultRuntime({"candidates": [_candidate()]})
    result = run_workspace_completion(
        task={"core_objective": "检查 src/context.py"},
        replay=replay,
        timeline=timeline,
        agent=runtime,
        output_root=tmp_path / "completion",
        env_origin=env_origin,
        source={
            "selected_span_ids": ["public-task"],
            "raw_session": {"messages": [{"role": "assistant", "content": "RAW_HIDDEN_ANSWER"}]},
            "tool_timeline": timeline,
            "selected_tool_timeline": timeline,
        },
    )
    assert runtime.session is not None
    public_refs = {"initial-read", "initial-list", "later-initial-read"}
    if env_origin == "DEFAULT_EMPTY":
        public_refs.add("task:q")
    assert set(result["evidence_ref_ids"]) == public_refs
    assert {item["evidence_ref_id"] for item in runtime.session.evidence} == public_refs
    assert "HIDDEN_REVIEW_PAYLOAD" not in json.dumps(runtime.session.evidence)
    assert "review.md" not in runtime.instruction
    assert "RAW_HIDDEN_ANSWER" not in runtime.instruction
    assert json.loads(runtime.session.session_context)["messages"][0]["content"] == "RAW_HIDDEN_ANSWER"
    assert runtime.session.user_records == []
    assert runtime.session.user_texts == []
    assert "HIDDEN_REVIEW_PAYLOAD" not in json.dumps(runtime.session.replay_files)
    assert "RAW_HIDDEN_ANSWER" in execute_tool("read_session_context", {}, runtime.session)
    assert "RAW_HIDDEN_ANSWER" in execute_tool("read_session_message", {"index": 0}, runtime.session)
    assert execute_tool("read_file", {"path": "review.md"}, runtime.session).startswith("error:")
    for ref in (
        "hidden-write", "hidden-write@4", "unknown", "post-read",
        "timeline:2", "timeline:4", "timeline:5", "duplicate", "duplicate@7",
    ):
        for key in ("id", "evidence_ref_id"):
            assert execute_tool("read_evidence", {key: ref}, runtime.session) == (
                "error: unknown evidence_ref_id"
            )
    assert "INITIAL_SOURCE" in execute_tool(
        "read_evidence", {"id": "initial-read"}, runtime.session
    )
    assert "initial-list" in execute_tool("list_evidence", {}, runtime.session)
    # 隐藏事件前后首次观察的 ID 均绑定到原事件及其正文，不因过滤重编号。
    for replayed in replay.files:
        evidence = json.loads(execute_tool(
            "read_evidence", {"id": replayed.first_observation_event_id}, runtime.session
        ))
        assert evidence["call_id"] == replayed.first_observation_event_id
        assert evidence["evidence_ref_id"] == replayed.first_observation_event_id
        assert evidence["result_text"] == replayed.content


def test_completion_prompt_omits_raw_session(tmp_path: Path) -> None:
    source = _source()
    runtime = ResultRuntime({"candidates": [_candidate()]})
    run_workspace_completion(
        task={"core_objective": "修复入口"},
        replay=ReplayResult((), (), (), ()),
        timeline=list(source["tool_timeline"]),
        source=source,
        agent=runtime,
        output_root=tmp_path,
    )
    prompt = json.loads(
        (tmp_path / "private/model_exchange.json").read_text(encoding="utf-8")
    )["request"]["prompt"]
    assert "FULL SESSION CONTEXT" not in prompt
    assert '"messages"' not in prompt
    assert "late unselected context" not in prompt
    assert "solvable, but NOT solved" in prompt


def test_completion_does_not_empty_workspace_when_session_replay_has_files(tmp_path: Path) -> None:
    source = _source()
    replay = replay_from_timeline(list(source["tool_timeline"]), tmp_path / "bE0")
    assert source["selected_span_has_file_ops"] is True
    assert replay.files
    # Selected span with no file ops, but session-level replay still has files.
    source["selected_span_has_file_ops"] = False
    runtime = ResultRuntime({"candidates": [], "open_questions": []})
    result = run_workspace_completion(
        task={"core_objective": "解释超时"},
        replay=replay,
        timeline=list(source["tool_timeline"]),
        source=source,
        agent=runtime,
        output_root=tmp_path / "completion",
    )
    assert result["status"] == "READY"
    assert result["agent"]["skip_reason"] is None
    assert result["completion_strategy"] == "from_replayed"
    workspace = Path(result["candidates"][0]["workspace"])
    assert (workspace / "foo.py").is_file()


@pytest.mark.parametrize("completed,errors", [(False, []), (True, ["TOOL_FAILURE"])])
def test_completion_cannot_materialize_incomplete_or_errored_agent(
    tmp_path: Path, completed: bool, errors: list[str]
) -> None:
    runtime = ResultRuntime({"candidates": [_candidate()]}, completed=completed, errors=errors)
    result = run_workspace_completion(task={"core_objective": "检查候选格式"}, replay=ReplayResult((), (), (), ()),
                                     timeline=[], agent=runtime, output_root=tmp_path)
    assert result["status"] == "REVIEW"
    assert not (tmp_path / "candidates").exists()
    assert "EMPTY_REPLAY_TREE" in result["errors"]


def test_completion_rejects_invalid_candidate_schema(tmp_path: Path) -> None:
    candidate = _candidate()
    candidate["dependencies"] = "pip install guessed-package"
    timeline = [
        {
            "call_id": "c1",
            "name": "read_file",
            "arguments": {"path": "foo.py", "offset": 1, "limit": 2},
            "result_text": "def main():\n",
        }
    ]
    replay = replay_from_timeline(timeline)
    runtime = ResultRuntime({"candidates": [candidate]})
    result = run_workspace_completion(
        task={"core_objective": "检查候选格式"},
        replay=replay,
        timeline=timeline,
        agent=runtime,
        output_root=tmp_path,
    )
    assert result["status"] == "REVIEW"
    assert "INVALID_DEPENDENCIES" in result["candidates"][0]["errors"]


@pytest.mark.parametrize("confidence", ["not-a-number", float("nan"), float("inf"), -0.1, 1.1])
def test_sufficiency_rejects_invalid_confidence(tmp_path: Path, confidence) -> None:
    runtime = ResultRuntime({"label": "SUFFICIENT", "decision": "READY", "reason": "looks fine",
                             "missing_context": [], "confidence": confidence})
    result = run_workspace_sufficiency(task={}, workspace_root=tmp_path / "ws", agent=runtime,
                                      output_root=tmp_path / "judge")
    assert result["status"] == "REVIEW"
    assert result["decision"] == "REVIEW"
    assert "INVALID_CONFIDENCE" in result["errors"]


def test_sufficiency_rejects_incomplete_agent_with_ready_payload(tmp_path: Path) -> None:
    runtime = ResultRuntime({"label": "SUFFICIENT", "decision": "READY", "reason": "looks fine",
                             "missing_context": [], "confidence": 0.9}, completed=False)
    result = run_workspace_sufficiency(task={}, workspace_root=tmp_path / "ws", agent=runtime,
                                      output_root=tmp_path / "judge")
    assert result["status"] == "REVIEW"
    assert "AGENT_INCOMPLETE" in result["errors"]


def test_captured_write_content_is_hidden_and_detected_as_solution_leakage() -> None:
    solution = "def unique_completed_answer():\n    return fully_solved_task\n"
    replay = replay_from_timeline([
        {"call_id": "read", "name": "read_file", "arguments": {"path": "foo.py"},
         "result_text": "def main():\n    pass\n"},
        {"call_id": "write", "name": "write_file",
         "arguments": {"path": "foo.py", "content": solution}},
    ])
    change = replay.withheld_changes[0]
    assert change.final_content == solution
    assert change.provenance == "TRAJECTORY_MUTATION"
    assert replay.files[0].content != solution
    public = json.dumps(replay.to_dict(), ensure_ascii=False)
    assert solution not in public
    assert replay.to_dict()["withheld_changes"][0]["final_content_sha256"]
    candidate = _candidate([{"path": "context.py", "content": solution,
                             "provenance": "MODEL_COMPLETED", "evidence_ref_ids": ["read"]}])
    valid, errors = validate_completion_candidate(candidate, replay, {"read", "write"})
    assert not valid
    assert "SOLUTION_LEAKAGE:context.py" in errors
