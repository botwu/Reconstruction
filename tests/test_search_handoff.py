"""搜索交接必须保留来源，并区分历史任务输入与本次答案。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceforge.reconstruction.search_environment import run_search_task


def run_fixture(root, **changes):
    messages = [
        {"role": "assistant", "content": "历史完整方案：先恢复数据；再比较两个实现；保留失败重试与版本约束。"},
        {"role": "user", "content": "按上述方案比较两个实现。"},
        {"role": "assistant", "content": "本次比较的最终答案：A 优于 B。"},
    ]
    source = {"raw_session": {"messages": messages}, "tool_timeline": [
        {"name": "Read", "arguments": {"path": "a.py"}, "result_text": "A 的完整源码\r\n",
         "session_parse": {"reason": "模型已经认定 A 优于 B", "effect": "read_only",
                           "file_ops": [{"kind": "read", "path": "a.py", "content": "A 的完整源码\r\n",
                                         "line_numbers": [1]}]}},
        {"name": "Read", "arguments": {"path": "b.py"}, "result_text": "B 的源码及调用链"},
        {"name": "Read", "pending": True},
    ]}
    payload = {
        "status": "READY", "requires_live_web": False, "retrieval_reason": "本地源码比较",
        # 旧白名单只含 A：回归必须证明 B 的原文仍被交付。
        "reference_event_indices": [0], "excluded_events": [], "context_references": [],
        "context_note": "自由改写的提前结论，不允许进入交付。",
        "requirement_coverage": [{"obligation_id": "compare", "evidence_ref_ids": ["captured:0", "captured:1"],
                                  "reason": "两个实现及调用资料均有原始捕获"}],
        "limitations": [], "missing_inputs": [], **changes,
    }

    def run(**kwargs):
        for index in range(2):
            kwargs["session"].tool_events.append(
                {"name": "read_evidence", "arguments": {"id": f"captured:{index}"}, "ok": True})
        return SimpleNamespace(completed=True, errors=[], payload=payload)

    outcome = run_search_task(
        source=source, task={"task_id": "q", "task_instruction": "比较 A 和 B",
                             "source_task": {"message_indices": [1]},
                             "acceptance_obligations": [{"id": "compare", "text": "比较两个实现"}]},
        agent=SimpleNamespace(run=run), output_root=root,
    )
    return outcome, json.loads((root / "environment.json").read_text()), messages


def test_unselected_raw_source_is_not_lost_or_replaced_by_parser_opinion(tmp_path):
    outcome, env, _ = run_fixture(tmp_path)
    assert outcome["status"] == "ENVIRONMENT_READY"
    assert [r["event_index"] for r in env["captures"]] == [0, 1]
    assert env["captures"][0]["result_text"] == "A 的完整源码\r\n"
    assert env["captures"][1]["result_text"] == "B 的源码及调用链"
    assert "reason" not in env["captures"][0]["session_parse"]
    assert "自由改写的提前结论" not in json.dumps(env, ensure_ascii=False)
    package = json.loads((Path(outcome["harbor_task"]) / "workspace/evidence.json").read_text())
    assert package["captures"] == env["captures"]


def test_prior_plan_is_copied_from_source_in_full(tmp_path):
    outcome, env, messages = run_fixture(tmp_path, context_references=[
        {"message_index": 0, "used_by_user_message_index": 1},
    ])
    assert outcome["status"] == "ENVIRONMENT_READY"
    assert env["context_messages"][0]["content"] == messages[0]["content"]
    assert env["context_messages"][0]["message_index"] == 0


@pytest.mark.parametrize("reference", [
    {"message_index": 2, "used_by_user_message_index": 1},
    {"message_index": 0, "used_by_user_message_index": 2},
    {"message_index": 0, "used_by_user_message_index": 1, "quote": "伪造的历史方案"},
])
def test_future_answer_or_invented_history_cannot_become_input(tmp_path, reference):
    outcome, _, _ = run_fixture(tmp_path, context_references=[reference])
    assert outcome["status"] == "BLOCKED"


@pytest.mark.parametrize("excluded", [
    [{"event_index": 3, "reason": "源中不存在的索引"}],
    [{"event_index": 1, "reason": ""}],
    [{"event_index": 1, "reason": "本次答案"}, {"event_index": 1, "reason": "重复"}],
])
def test_exclusions_require_existing_unique_source_and_reason(tmp_path, excluded):
    outcome, _, _ = run_fixture(tmp_path, excluded_events=excluded)
    assert outcome["status"] == "BLOCKED"


def test_explicit_exclusion_is_audited_and_does_not_leave_false_support(tmp_path):
    outcome, env, _ = run_fixture(
        tmp_path, excluded_events=[{"event_index": 1, "reason": "该返回是本次生成的答案，不能作为初态"}])
    assert outcome["status"] == "BLOCKED"
    assert any("captured:1" in error for error in outcome["errors"])
    assert env["evidence_handoff"]["excluded_events"][0]["event_index"] == 1


def test_access_to_one_source_does_not_cover_unaccounted_requirement(tmp_path):
    outcome, _, _ = run_fixture(tmp_path, requirement_coverage=[])
    assert outcome["status"] == "BLOCKED"


@pytest.mark.parametrize("repair_changes_input", [True, False])
def test_actual_rollout_gap_returns_to_same_researcher_and_stops_without_change(
    tmp_path, repair_changes_input,
):
    from traceforge.reconstruction.agents.session import execute_tool

    author_sessions, solver_inputs = [], []
    completions = []
    task = {"task_id": "loop", "task_instruction": "比较 A 和 B",
            "acceptance_obligations": [{"id": "compare", "text": "比较 A 和 B"}]}
    source = {"raw_session": {"messages": []}, "tool_timeline": [
        {"name": "Read", "result_text": "A 的原始源码"},
        {"name": "Read", "result_text": "B 的原始源码"},
    ]}

    def author_run(**kwargs):
        author_sessions.append(kwargs["session"])
        session = kwargs["session"]
        session.conversation.messages.append({"role": "assistant", "content": "已核对原始材料"})
        if kwargs["role"].name == "search_review":
            prompt = json.loads(kwargs["instruction"])
            assert prompt["trials"][0]["tool_events"]
            assert prompt["available_evidence_ref_ids"] == [
                item["evidence_ref_id"] for item in solver_inputs[-1]]
            assert prompt["trials"][0]["read_evidence_calls"][0]["id"] == "captured:1"
            assert "requirement_coverage" not in prompt
            missing = len(solver_inputs[-1]) == 1
            return SimpleNamespace(completed=True, errors=[], payload={
                "decision": "REPAIR" if missing else "COMPLETE",
                "requirements": [{"obligation_id": "compare",
                                  "status": "ENVIRONMENT_GAP" if missing else "SUPPORTED",
                                  "reason": "实际 read_evidence 不能读取 B" if missing else "A、B 均可读取",
                                  "repair": "恢复 captured:1" if missing else ""}],
            })
        completions.append(kwargs)
        if len(completions) > 1:
            assert "rollout_feedback" in json.loads(kwargs["instruction"])
        session.tool_events.extend(
            {"name": "read_evidence", "arguments": {"id": f"captured:{i}"}, "ok": True}
            for i in range(2))
        repaired = repair_changes_input and len(completions) > 1
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "READY", "requires_live_web": False, "retrieval_reason": "比较本地源码",
            "excluded_events": [] if repaired else [{"event_index": 1, "reason": "被误判为无关"}],
            "context_references": [], "missing_inputs": [],
            "requirement_coverage": [{
                "obligation_id": "compare",
                "evidence_ref_ids": ["captured:0", "captured:1"] if repaired else ["captured:0"],
                "reason": "核对比较所需源码",
            }],
        })

    def solve(**kwargs):
        session = kwargs["session"]
        solver_inputs.append(session.evidence)
        args = {"id": "captured:1"}
        result = execute_tool("read_evidence", args, session)
        session.tool_events.append({"name": "read_evidence", "arguments": args,
                                    "ok": not result.startswith("error:"), "result": result})
        return SimpleNamespace(completed=True, errors=[], turns=[],
                               final_text="缺少 B 的源码" if result.startswith("error:") else "已完成比较")

    outcome = run_search_task(
        source=source, task=task, agent=SimpleNamespace(run=author_run),
        rollout_agent=SimpleNamespace(run=solve, model_name="fixture"), rollout_trials=1,
        output_root=tmp_path,
    )
    assert len(completions) == 2
    assert all(session is author_sessions[0] for session in author_sessions)
    assert len(solver_inputs) == (2 if repair_changes_input else 1)
    assert outcome["acceptance"] == "NOT_ASSESSED"
    if repair_changes_input:
        assert outcome["status"] == "ROLLOUT_COMPLETED"
        assert outcome["environment_review"] == "COMPLETE"
        assert len(outcome["researcher_rounds"]) == 2
    else:
        assert outcome["status"] == "BLOCKED"
        assert outcome["errors"] == ["SEARCH_RECONSTRUCTION_NO_PROGRESS"]


def test_old_environment_cannot_bypass_source_boundary_by_resuming(tmp_path):
    from traceforge.reconstruction.search_environment import run_search_rollouts

    with pytest.raises(ValueError, match="重新补全"):
        run_search_rollouts(
            environment={"status": "READY", "schema_version": "traceforge.search-environment.v2"},
            rollout_agent=SimpleNamespace(), output_root=tmp_path)


def test_current_user_original_is_legal_input_even_when_referenced_again(tmp_path):
    outcome, env, messages = run_fixture(tmp_path, context_references=[
        {"message_index": 1, "used_by_user_message_index": 1},
    ])
    assert outcome["status"] == "ENVIRONMENT_READY"
    assert env["context_messages"][0]["content"] == messages[1]["content"]


def test_explicit_exclusion_of_known_pending_call_is_harmless(tmp_path):
    outcome, env, _ = run_fixture(tmp_path, excluded_events=[
        {"event_index": 2, "reason": "原会话没有此调用的返回"},
    ])
    assert outcome["status"] == "ENVIRONMENT_READY"
    assert [record["event_index"] for record in env["captures"]] == [0, 1]


@pytest.mark.parametrize("errors,answer", [(["UPSTREAM_TRANSPORT_ERROR"], "截断的报告"), ([], "")])
def test_execution_errors_and_empty_answers_are_not_completed_rollouts(tmp_path, errors, answer):
    from traceforge.reconstruction.search_environment import run_search_rollouts

    result = run_search_rollouts(
        environment={"schema_version": "traceforge.search-environment.v3", "status": "READY",
                     "task": {"task_id": "q", "task_instruction": "比较源码"},
                     "captures": [], "context_messages": [], "limitations": []},
        rollout_agent=SimpleNamespace(model_name="fixture", run=lambda **kwargs: SimpleNamespace(
            completed=True, errors=errors, final_text=answer, turns=[])),
        output_root=tmp_path, rollout_trials=1,
    )
    assert result["status"] == "ROLLOUT_INCOMPLETE"
    assert result["errors"]
    assert result["environment_review"] == "NOT_REVIEWED"
    assert result["acceptance"] == "NOT_ASSESSED"


def test_verbatim_history_quote_keeps_original_whitespace():
    from traceforge.reconstruction.search_handoff import restore_context

    text = "  方案及约束。\r\n"
    result = restore_context(
        [{"role": "assistant", "content": text}, {"role": "user", "content": "执行该方案"}],
        {"source_task": {"message_indices": [1]}},
        [{"message_index": 0, "used_by_user_message_index": 1, "quote": text}],
    )
    assert result[0]["content"] == text
