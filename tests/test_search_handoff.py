"""搜索交接必须保留来源，并区分历史任务输入与本次答案。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceforge.reconstruction.search_environment import run_search_task


def run_fixture(root, *, second_result="B 的源码及调用链", **changes):
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
        {"name": "Read", "arguments": {"path": "b.py"}, "result_text": second_result},
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
                             "source_task": {"message_indices": [1], "user_texts": [messages[1]["content"]]},
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
        tmp_path, second_result="本次比较的最终答案：A 优于 B。",
        excluded_events=[{"event_index": 1, "kind": "task_answer",
                          "quote": "本次比较的最终答案：A 优于 B。",
                          "reason": "该返回是本次生成的答案，不能作为初态"}])
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
            "excluded_events": [] if repaired else [{
                "event_index": 1, "kind": "post_task_state", "quote": "B 的原始源码",
                "reason": "模型误把原始观察当成解题后的状态，语义复核仍需纠正"}],
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
        event = {"tool_call_id": "fixture-read-b", "name": "read_evidence", "arguments": args,
                 "ok": not result.startswith("error:"),
                 "result_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
                 "result_preview": result[:512]}
        session.tool_events.append(event)
        trace = kwargs["output_root"] / "private/tool_events.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text(json.dumps({"status": "FINISHED", **event, "result": result}) + "\n")
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


@pytest.mark.parametrize("version", ["v2", "v3"])
def test_old_environment_cannot_bypass_source_boundary_by_resuming(tmp_path, version):
    from traceforge.reconstruction.search_environment import run_search_rollouts

    with pytest.raises(ValueError, match="重新补全"):
        run_search_rollouts(
            environment={"status": "READY", "schema_version": f"traceforge.search-environment.{version}"},
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
        environment={"schema_version": "traceforge.search-environment.v5", "status": "READY",
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


@pytest.mark.parametrize("message,expected", [(1, "ENVIRONMENT_READY"), (2, "BLOCKED")])
def test_direct_task_input_is_available_without_repeated_history(tmp_path, message, expected):
    outcome, _, _ = run_fixture(tmp_path, requirement_coverage=[{
        "obligation_id": "compare", "evidence_ref_ids": ["captured:0", f"message:{message}"],
        "reason": "原用户要求和比较所需源码",
    }])
    assert outcome["status"] == expected


def test_missing_live_query_feedback_reports_actual_calls(tmp_path, monkeypatch):
    from traceforge.reconstruction.search_tools import SearchTools

    url = "https://example.org/paper"
    monkeypatch.setattr(SearchTools, "_search", lambda self, query: (
        [{"title": "原始论文", "link": url}], "query-hash"))
    monkeypatch.setattr(SearchTools, "_fetch", lambda self, address: {
        "success": True, "url": address, "text": "论文原文", "source_mode": "live_page",
        "raw_sha256": "page-hash",
    })
    instructions = []

    def author(**kwargs):
        instructions.append(kwargs["instruction"])
        session = kwargs["session"]
        if len(instructions) == 1:
            session.web_open_handler(url)
        else:
            feedback = json.loads(kwargs["instruction"])
            assert feedback["live_access"]["web_search_calls"] == []
            assert feedback["live_access"]["opened_urls"] == [url]
            session.web_search_handler("原始论文")
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "READY", "requires_live_web": True, "retrieval_reason": "补充公开文献",
            "context_references": [], "excluded_events": [], "missing_inputs": [],
            "requirement_coverage": [{"obligation_id": "research", "evidence_ref_ids": [url],
                                      "reason": "研究所需论文原文"}],
        })

    outcome = run_search_task(
        source={"raw_session": {"messages": []}, "tool_timeline": []},
        task={"task_id": "query", "task_instruction": "检索论文",
              "acceptance_obligations": [{"id": "research"}]},
        agent=SimpleNamespace(run=author), output_root=tmp_path,
    )
    assert outcome["status"] == "ENVIRONMENT_READY"
    assert len(instructions) == 2


@pytest.mark.parametrize("original_inline", [True, False])
def test_inline_original_read_is_evidence_but_parser_opinion_is_not(tmp_path, original_inline):
    observation = {"kind": "read", "path": "a.py", "content_ref": {
        "block_index": 0, "start_line": 1, "end_line": 2},
        "content": "def existing():\n    return 1"}
    parsed = {"reason": "模型声称读过 a.py", "file_ops": [observation] if original_inline else []}
    source = {"raw_session": {"messages": [
        {"role": "tool", "content": "def existing():\n    return 1"},
    ] if original_inline else []}, "tool_timeline": [
        {"name": "Read", "result_text": "def existing():\n    return 1", "session_parse": parsed,
         "tool_message_index": 0}]}

    def author(**kwargs):
        prompt = json.loads(kwargs["instruction"])
        if "events" in prompt:
            interpretation = prompt["events"][0]["interpretation"]
            assert interpretation["reason"] == parsed["reason"]
            expected_ops = [
                {key: value for key, value in observation.items() if key != "content"}
            ] if original_inline else []
            assert interpretation["file_ops"] == expected_ops
            assert prompt["events"][0]["evidence_ref_id"] == "captured:0"
            if original_inline:
                assert (prompt["SOURCE_SESSION"]["messages"][0]["message"]
                        == source["raw_session"]["messages"][0])
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "READY", "requires_live_web": False, "retrieval_reason": "原始源码调查",
            "excluded_events": [], "context_references": [], "missing_inputs": [],
            "requirement_coverage": [{"obligation_id": "inspect", "evidence_ref_ids": ["captured:0"],
                                      "reason": ["已有函数正文和读取坐标", "尚未作出分析结论"]}],
        })

    outcome = run_search_task(
        source=source, task={"task_id": "inline", "task_instruction": "分析源码",
                             "acceptance_obligations": [{"id": "inspect"}]},
        agent=SimpleNamespace(run=author), output_root=tmp_path,
    )
    assert outcome["status"] == ("ENVIRONMENT_READY" if original_inline else "BLOCKED")
    env = json.loads((tmp_path / "environment.json").read_text())
    if original_inline:
        access = env["author_evidence_access"]
        assert access["inline_observations"][0]["evidence_ref_id"] == "captured:0"
        assert access["inline_observations"][0]["content_ref"] == observation["content_ref"]
        assert access["read_calls"] == []


@pytest.mark.parametrize("reason,valid", [
    ("源码和调用处", True), (["源码", "调用处"], True),
    ([], False), (["源码", 1], False), (["  "], False), ({"text": "源码"}, False),
])
def test_coverage_explanation_accepts_text_or_text_items_without_hiding_missing_reads(reason, valid):
    from traceforge.reconstruction.search_handoff import validate_requirement_coverage

    coverage = [{"obligation_id": "inspect", "evidence_ref_ids": ["captured:0"], "reason": reason}]
    task = {"acceptance_obligations": [{"id": "inspect"}]}
    assert (not validate_requirement_coverage(task, coverage, {"captured:0"}, {"captured:0"})) == valid
    assert validate_requirement_coverage(task, coverage, {"captured:0"}, set())


@pytest.mark.parametrize("extra", [
    {},
    {"kind": "retrieval_preview", "quote": "旧的搜索片段仍有来源线索"},
    {"kind": "task_answer", "quote": "不在原始返回中的结论"},
])
def test_history_search_cannot_be_discarded_as_replaced_or_with_invented_proof(extra):
    from traceforge.reconstruction.search_handoff import deliver_captures

    records = [{"event_index": 0, "evidence_ref_id": "captured:0",
                "result_text": "旧的搜索片段仍有来源线索"}]
    with pytest.raises(ValueError):
        deliver_captures(records, [{
            "event_index": 0, "reason": "历史检索预览，已用新的期刊页面替代。",
            **extra,
        }], event_count=1)
    assert records[0]["result_text"] == "旧的搜索片段仍有来源线索"


def test_task_answer_exclusion_requires_original_return_not_parser_opinion():
    from traceforge.reconstruction.search_handoff import deliver_captures

    records = [{"event_index": 0, "evidence_ref_id": "captured:0",
                "result_blocks": [{"text": "子 agent 已完成本题，最终结论 A 优于 B。"}]}]
    exclusion = {"event_index": 0, "kind": "task_answer", "quote": "最终结论 A 优于 B",
                 "reason": "原解题过程产生的回答不进入初态。"}
    delivered, handoff = deliver_captures(records, [exclusion], event_count=1)
    assert delivered == []
    assert handoff["excluded_events"] == [exclusion]


@pytest.mark.parametrize("corrects_response", [True, False])
def test_review_phase_retries_wrong_completion_shape_without_rebuilding_environment(tmp_path, corrects_response):
    from traceforge.reconstruction.agents import AgentSession
    from traceforge.reconstruction.agents.session import AgentConversation
    from traceforge.reconstruction.search_environment import _review_search_rollouts

    trial = tmp_path / "rollouts/trial-01"
    trial.mkdir(parents=True)
    (trial / "answer.md").write_text("基于原文完成的回答")
    (trial / "execution.json").write_text(json.dumps({"tool_events": []}))
    (trial / "receipt.json").write_text(json.dumps({"completed": True}))
    (trial / "input.json").write_text(json.dumps({"evidence": []}))
    calls = []
    session = AgentSession(conversation=AgentConversation())

    def review(**kwargs):
        calls.append(kwargs)
        payload = {"status": "READY", "requirement_coverage": []}
        if corrects_response and len(calls) == 2:
            payload = {"decision": "COMPLETE", "requirements": [{
                "obligation_id": "compare", "status": "SUPPORTED",
                "reason": "实际回答使用了已交付的原始比较资料", "repair": "",
            }]}
        return SimpleNamespace(completed=True, errors=[], payload=payload)

    result = _review_search_rollouts(
        task={"acceptance_obligations": [{"id": "compare"}]},
        environment={"evidence_handoff": {}, "captures": [], "requirement_coverage": [],
                     "context_messages": []},
        agent=SimpleNamespace(run=review), session=session, output_root=tmp_path,
    )
    assert len(calls) == 2
    assert all(call["role"].name == "search_review" and call["session"] is session for call in calls)
    assert (trial / "answer.md").read_text() == "基于原文完成的回答"
    assert result["decision"] == ("COMPLETE" if corrects_response else "BLOCKED")

@pytest.mark.parametrize("corrects_references", [True, False])
def test_coverage_retry_supplies_exact_delivered_ids_without_guessing(tmp_path, corrects_references):
    calls = []

    def author(**kwargs):
        calls.append(json.loads(kwargs["instruction"]))
        kwargs["session"].tool_events.append(
            {"name": "read_evidence", "arguments": {"id": "captured:0"}, "ok": True})
        refs = ["captured:0:offset0..2000", "user:1"]
        if len(calls) == 2:
            assert calls[-1]["available_evidence_ref_ids"] == ["captured:0", "message:1"]
            assert calls[-1]["read_evidence_ref_ids"] == ["captured:0", "message:1"]
            if corrects_references:
                refs = calls[-1]["available_evidence_ref_ids"]
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "READY", "requires_live_web": False, "retrieval_reason": "检索已捕获源码",
            "excluded_events": [], "context_references": [], "missing_inputs": [],
            "requirement_coverage": [{"obligation_id": "inspect", "evidence_ref_ids": refs,
                                      "reason": "原始源码和用户要求已经读取"}],
        })

    outcome = run_search_task(
        source={"raw_session": {"messages": [
            {"role": "assistant", "content": "前文"},
            {"role": "user", "content": "分析这份代码"},
        ]}, "tool_timeline": [{"name": "Read", "result_text": "原始源码"}]},
        task={"task_id": "q", "task_instruction": "分析这份代码",
              "source_task": {"message_indices": [1], "user_texts": ["分析这份代码"]},
              "acceptance_obligations": [{"id": "inspect", "text": "分析代码"}]},
        agent=SimpleNamespace(run=author), output_root=tmp_path,
    )
    assert len(calls) == 2
    assert outcome["status"] == ("ENVIRONMENT_READY" if corrects_references else "BLOCKED")


def test_review_resolves_solver_reads_from_its_own_input_not_author_inventory(tmp_path):
    from traceforge.reconstruction.agents import AgentSession
    from traceforge.reconstruction.agents.session import AgentConversation
    from traceforge.reconstruction.search_environment import _review_search_rollouts

    trial = tmp_path / "rollouts" / "trial-01"
    trial.mkdir(parents=True)
    (trial / "answer.md").write_text("依据实际读取的 A 作答")
    (trial / "receipt.json").write_text(json.dumps({"completed": True}))
    (trial / "input.json").write_text(json.dumps({"evidence": [
        {"evidence_ref_id": "live:0", "url": "https://example.org/a", "name": "web_open"},
        {"evidence_ref_id": "live:1", "url": "https://example.org/b", "name": "web_open"},
    ]}))
    (trial / "execution.json").write_text(json.dumps({"tool_events": [
        {"name": "read_evidence", "arguments": {"id": "live:0", "limit": 1000}, "ok": True},
    ]}))

    def review(**kwargs):
        prompt = json.loads(kwargs["instruction"])
        reads = prompt["trials"][0]["read_evidence_calls"]
        assert reads == [{"id": "live:0", "limit": 1000, "ok": True,
                          "source": {"evidence_ref_id": "live:0", "url": "https://example.org/a"}}]
        return SimpleNamespace(completed=True, errors=[], payload={
            "decision": "COMPLETE", "requirements": []})

    _review_search_rollouts(
        task={"acceptance_obligations": []},
        environment={"evidence_handoff": {}, "captures": [], "context_messages": [],
                     "requirement_coverage": [{"obligation_id": "q",
                         "evidence_ref_ids": ["https://example.org/a", "https://example.org/b"]}]},
        agent=SimpleNamespace(run=review),
        session=AgentSession(conversation=AgentConversation()), output_root=tmp_path)

@pytest.mark.parametrize("independent_revision", [False, True])
def test_intermediate_answer_is_input_only_for_a_new_task(independent_revision):
    from traceforge.reconstruction.search_handoff import restore_context

    messages = [
        {"role": "system", "content": "环境说明"},
        {"role": "user", "content": "分析两个方向的核心科学问题"},
        {"role": "assistant", "content": "旧分析答案：共同问题是 X，方向 A 与 B 最接近。"},
        {"role": "user", "content": "使用综述式段落，不要罗列"},
    ]
    task = {"source_task": {"message_indices": [3] if independent_revision else [1, 3]}}
    references = [{"message_index": 2, "used_by_user_message_index": 3}]
    if independent_revision:
        restored = restore_context(messages, task, references)
        assert restored[0]["content"] == messages[2]["content"]
    else:
        with pytest.raises(ValueError, match="任务初态"):
            restore_context(messages, task, references)


def test_review_api_failure_does_not_become_format_or_material_gap(tmp_path):
    from traceforge.reconstruction.agents import AgentSession
    from traceforge.reconstruction.search_environment import _review_search_rollouts

    trial = tmp_path / "rollouts/trial-01"
    trial.mkdir(parents=True)
    (trial / "answer.md").write_text("已有真实回答")
    (trial / "execution.json").write_text(json.dumps({"tool_events": []}))
    (trial / "receipt.json").write_text(json.dumps({"completed": True}))
    (trial / "input.json").write_text(json.dumps({"evidence": []}))
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completed=False, errors=["MODEL_RATE_LIMIT"], payload={},
                               final_text="Your requests have exceeded token rate limit.")

    result = _review_search_rollouts(
        task={"acceptance_obligations": [{"id": "compare"}]},
        environment={"evidence_handoff": {}, "captures": [], "requirement_coverage": [],
                     "context_messages": []},
        agent=SimpleNamespace(run=review), session=AgentSession(), output_root=tmp_path,
    )
    assert len(calls) == 1
    assert result["decision"] == "BLOCKED"
    assert result["failure_kind"] == "AGENT_FAILURE"
    assert result["errors"] == ["MODEL_RATE_LIMIT"]
    assert "exceeded token rate limit" in result["error_detail"]
    validation = json.loads((tmp_path / "researcher-review/validation.json").read_text())
    assert validation["status"] == "AGENT_FAILED"
    assert validation["errors"] == ["MODEL_RATE_LIMIT"]
    assert (trial / "answer.md").read_text() == "已有真实回答"


@pytest.mark.parametrize("role", ["system", "developer"])
def test_task_preferences_in_harness_prompt_are_quoted_as_source_data(role):
    from traceforge.reconstruction.search_handoff import restore_context

    quote = "  用户长期偏好：正文使用学术段落。\r\n"
    messages = [
        {"role": role, "content": quote + "旧工具协议：调用 old_search。"},
        {"role": "user", "content": "继续扩展综述"},
    ]
    result = restore_context(
        messages, {"source_task": {"message_indices": [1]}},
        [{"message_index": 0, "used_by_user_message_index": 1, "quote": quote}],
    )
    assert result == [{"message_index": 0, "used_by_user_message_index": 1,
                       "role": role, "content": quote}]
    assert messages[0]["content"].endswith("旧工具协议：调用 old_search。")


@pytest.mark.parametrize("role", ["system", "developer"])
@pytest.mark.parametrize("quote", [None, "", "并不存在的用户偏好"])
def test_harness_prompt_requires_a_verbatim_task_context_quote(role, quote):
    from traceforge.reconstruction.search_handoff import restore_context

    reference = {"message_index": 0, "used_by_user_message_index": 1}
    if quote is not None:
        reference["quote"] = quote
    with pytest.raises(ValueError):
        restore_context(
            [{"role": role, "content": "偏好与旧工具协议"}, {"role": "user", "content": "综述"}],
            {"source_task": {"message_indices": [1]}}, [reference],
        )


@pytest.mark.parametrize("role", ["system", "developer"])
def test_later_harness_context_cannot_become_initial_input(role):
    from traceforge.reconstruction.search_handoff import restore_context

    with pytest.raises(ValueError, match="本任务开始后"):
        restore_context(
            [{"role": "user", "content": "分析代码"},
             {"role": role, "content": "已完成后的状态"},
             {"role": "user", "content": "继续"}],
            {"source_task": {"message_indices": [0, 2]}},
            [{"message_index": 1, "used_by_user_message_index": 2, "quote": "已完成后的状态"}],
        )


@pytest.mark.parametrize("pending", [False, True])
def test_original_user_input_without_returned_tools_is_delivered(tmp_path, pending):
    original = "只整理并翻译可见商品资料：title=Brass Sink Drain；bullet_points=[原文截断]"
    source = {"raw_session": {"messages": [{"role": "user", "content": original}]},
              "tool_timeline": [{"name": "history", "pending": True}] if pending else []}
    task = {"task_id": "user-only", "task_instruction": "整理并翻译用户原文",
            "source_task": {"message_indices": [0], "user_texts": [original]},
            "acceptance_obligations": [{"id": "translate", "text": "只翻译实际可见资料"}]}
    calls = []

    def author(**kwargs):
        calls.append(kwargs["role"].name)
        assert kwargs["session"].evidence == []
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "READY", "requires_live_web": False,
            "retrieval_reason": "任务仅需要完整交付的用户原文，不补全截断内容",
            "excluded_events": [], "context_references": [], "missing_inputs": [],
            "requirement_coverage": [{
                "obligation_id": "translate", "evidence_ref_ids": ["message:0"],
                "reason": "原始标题及截断边界已经交付，不借用后续助手回答",
            }],
        })

    outcome = run_search_task(source=source, task=task, agent=SimpleNamespace(run=author),
                              output_root=tmp_path)
    assert outcome["status"] == "ENVIRONMENT_READY"
    assert calls == ["search_completion"]
    environment = json.loads((tmp_path / "environment.json").read_text())
    assert environment["captures"] == []
    assert environment["evidence_handoff"]["pending_event_indices"] == ([0] if pending else [])
    assert original in (Path(outcome["harbor_task"]) / "instruction.md").read_text()


@pytest.mark.parametrize("gap", ["pending_return", "unread_return", "private_history"])
def test_user_input_does_not_erase_required_evidence_gap(tmp_path, gap):
    original = "依据先前的私有方案比较两个实现，不用新编方案替代。"
    event = ({"name": "history", "result_text": "两个实现的原始源码"}
             if gap == "unread_return" else {"name": "history", "pending": True})
    source = {"raw_session": {"messages": [{"role": "user", "content": original}]},
              "tool_timeline": [event]}
    task = {"task_id": "required-evidence", "task_instruction": original,
            "source_task": {"message_indices": [0], "user_texts": [original]},
            "acceptance_obligations": [{"id": "compare", "text": "按先前方案比较"}]}
    missing = ["先前私有方案正文未交付"] if gap == "private_history" else []

    def author(**kwargs):
        if kwargs["role"].name == "search_review":
            return SimpleNamespace(completed=True, errors=[], payload={
                "decision": "BLOCKED", "requirements": [{
                    "obligation_id": "compare", "status": "ENVIRONMENT_GAP",
                    "reason": "用户要求不含私有方案正文，当前工具没有恢复该正文的入口",
                    "repair": "",
                }],
            })
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "BLOCKED" if missing else "READY", "requires_live_web": False,
            "retrieval_reason": "私有方案必须来自原始输入，公网不能替代",
            "excluded_events": [], "context_references": [], "missing_inputs": missing,
            "requirement_coverage": [{
                "obligation_id": "compare",
                "evidence_ref_ids": ["message:0"] if missing else ["message:0", "captured:0"],
                "reason": "原请求已交付，仍须核对对应私有材料正文",
            }],
        })

    outcome = run_search_task(source=source, task=task, agent=SimpleNamespace(run=author),
                              output_root=tmp_path)
    assert outcome["status"] == "BLOCKED"
    assert "harbor_task" not in outcome
    if missing:
        assert outcome["missing_inputs"] == missing
        assert outcome["researcher_rounds"][0]["review"]["decision"] == "BLOCKED"
    else:
        expected = "来源尚未实际读取" if gap == "unread_return" else "来源未交付"
        assert any(expected in error and "captured:0" in error for error in outcome["errors"])
