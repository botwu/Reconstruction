"""补全阻塞须复核原始恢复路径，不能被重复失败查询或评审文字驱动空转。"""

import json
from types import SimpleNamespace

import pytest

from traceforge.reconstruction.session_source import indexed_session


def scenario(tmp_path, monkeypatch, *, outcome="recover", failed_agent=False):
    from traceforge.reconstruction import search_environment as module

    raw = {"meta": {"reasoning": {"effort": "medium"}}, "messages": [
        {"role": "system", "content": "原 harness 与工具定义", "tools": [{"name": "lookup"}]},
        {"role": "user", "content": "分析该论文"},
        {"role": "tool", "tool_call_id": "lookup-1",
         "content": "原始来源 https://example.org/paper"},
    ]}
    source = {"line_sha256": "fixed-line", "raw_session": raw, "tool_timeline": [{
        "name": "lookup", "call_id": "lookup-1", "tool_message_index": 2,
        "result_text": raw["messages"][2]["content"],
    }]}
    task = {"task_id": "paper", "task_instruction": "分析该论文",
            "source_task": {"message_indices": [1], "user_texts": ["分析该论文"]},
            "acceptance_obligations": [{
                "id": "analysis", "kind": "NON_FILE", "description": "分析论文",
            }]}
    monkeypatch.setattr(module.SearchTools, "_search",
                        lambda *_: (_ for _ in ()).throw(RuntimeError("Not enough credits")))
    monkeypatch.setattr(module.SearchTools, "_fetch", lambda _, url: {
        "url": url, "success": True, "text": "原链接返回的完整论文正文", "raw_sha256": "paper-hash",
    })
    monkeypatch.setattr(module, "export_search_task", lambda _, path, **kwargs: path)
    roles, author_sessions, reviews = [], [], []
    author_turns = 0

    def run(*, role, instruction, session, output_root):
        nonlocal author_turns
        roles.append(role.name)
        request = json.loads(instruction)
        if role.name == "search_review":
            reviews.append(request)
            assert request["review_stage"] == "completion"
            assert "trials" not in request
            assert request["SOURCE_SESSION"] == indexed_session(raw)
            assert session.conversation is not author_sessions[0].conversation
            assert session.session_context == author_sessions[0].session_context
            assert request["actual_web_calls"][0]["success"] is False
            assert "Not enough credits" in request["actual_web_calls"][0]["error"]
            assert "web_search" not in role.tools and "web_open" not in role.tools
            decision = "BLOCKED" if outcome == "unrecoverable" else "REPAIR"
            payload = {"decision": decision, "requirements": [{
                "obligation_id": "analysis", "status": "ENVIRONMENT_GAP",
                "reason": "原tool消息仍提供与论文义务相关的来源，未尝试不等于不可访问",
                "repair": "读取原tool消息中的 https://example.org/paper，再核正文是否满足义务",
            }]}
        else:
            assert role.name == "search_completion"
            author_sessions.append(session)
            author_turns += 1
            if (author_turns == 1 or outcome == "no_progress"
                    or (outcome == "ready_gate" and author_turns == 2)):
                session.web_search_handler("原查询")
                payload = {"status": "READY" if outcome == "ready_gate" else "BLOCKED",
                           "requires_live_web": True, "retrieval_reason": "原查询额度不足",
                           "missing_inputs": [] if outcome == "ready_gate"
                           else [f"尚缺论文正文；第{author_turns}次说明"]}
            else:
                assert session is author_sessions[0]
                assert request["completion_feedback"]["decision"] == "REPAIR"
                session.web_open_handler("https://example.org/paper")
                payload = {"status": "READY", "requires_live_web": False,
                           "retrieval_reason": "原线索正文已实际读取，现有输入足够",
                           "missing_inputs": []}
            payload.update(context_references=[], excluded_events=[], limitations=[],
                           requirement_coverage=[{"obligation_id": "analysis",
                                                  "evidence_ref_ids": ["captured:0"],
                                                  "reason": "原始论文入口及已取得正文"}])
            session.conversation.messages.extend([
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)},
            ])
        return SimpleNamespace(completed=not failed_agent,
                               errors=["UPSTREAM_TRANSPORT_ERROR"] if failed_agent else [],
                               payload=payload, final_text=json.dumps(payload))

    result = module.run_search_task(source=source, task=task, agent=SimpleNamespace(run=run),
                                    output_root=tmp_path)
    return result, roles, reviews, author_sessions


def test_blocked_completion_reviews_original_routes_then_resumes_same_author(tmp_path, monkeypatch):
    result, roles, reviews, sessions = scenario(tmp_path, monkeypatch)
    assert roles == ["search_completion", "search_review", "search_completion"]
    assert result["status"] == "ENVIRONMENT_READY"
    assert result["errors"] == []
    assert result["researcher_rounds"][0]["stage"] == "completion"
    assert reviews[0]["environment"]["status"] == "BLOCKED"
    assert sessions[0] is sessions[1]
    revised = json.loads((tmp_path / "revisions/0001/environment.json").read_text())
    assert revised["requires_live_web"] is False
    assert revised["live_references"][0]["text"] == "原链接返回的完整论文正文"


def test_repeated_failed_request_and_changed_reason_are_not_progress(tmp_path, monkeypatch):
    result, roles, _, _ = scenario(tmp_path, monkeypatch, outcome="no_progress")
    assert roles == ["search_completion", "search_review", "search_completion"]
    assert result["status"] == "BLOCKED"
    assert "SEARCH_RECONSTRUCTION_NO_PROGRESS" in result["errors"]
    assert result["missing_inputs"] == ["尚缺论文正文；第2次说明"]


def test_agent_failure_does_not_trigger_semantic_recovery(tmp_path, monkeypatch):
    result, roles, reviews, _ = scenario(tmp_path, monkeypatch, failed_agent=True)
    assert roles == ["search_completion"]
    assert reviews == []
    assert result["status"] == "BLOCKED"
    assert "UPSTREAM_TRANSPORT_ERROR" in result["errors"]


def test_unrecoverable_review_keeps_blocked_state_and_evidence(tmp_path, monkeypatch):
    result, roles, reviews, _ = scenario(tmp_path, monkeypatch, outcome="unrecoverable")
    assert roles == ["search_completion", "search_review"]
    assert result["status"] == "BLOCKED"
    assert result["researcher_rounds"][0]["review"]["decision"] == "BLOCKED"
    assert reviews[0]["actual_web_calls"][0]["success"] is False


@pytest.mark.parametrize("invalid", [
    {"decision": "COMPLETE", "status": "SUPPORTED"},
    {"decision": "REPAIR", "status": "SOLVER_ERROR"},
])
def test_completion_review_cannot_accept_environment_or_invent_solver_results(tmp_path, invalid):
    from traceforge.reconstruction.agents.session import AgentConversation, AgentSession
    from traceforge.reconstruction.search_environment import SEARCH_REVIEW_ROLE, _run_search_review

    requests = []
    reviewer = AgentSession(conversation=AgentConversation())
    original_request = {
        "review_stage": "completion",
        "SOURCE_SESSION": {"messages": [{"message": {"role": "user", "content": "原要求"}}]},
        "environment": {"task": {"task_instruction": "原要求"}},
        "actual_web_calls": [{"tool": "web_open", "success": False, "error": "HTTP 402"}],
    }

    def run(**kwargs):
        assert kwargs["session"] is reviewer
        requests.append(json.loads(kwargs["instruction"]))
        reviewer.conversation.messages.append({"role": "user", "content": kwargs["instruction"]})
        choice = invalid if len(requests) == 1 else {
            "decision": "BLOCKED", "status": "ENVIRONMENT_GAP",
        }
        return SimpleNamespace(completed=True, errors=[], payload={
            "decision": choice["decision"], "requirements": [{
                "obligation_id": "analysis", "status": choice["status"],
                "reason": "具体原文与访问回执", "repair": "原文中的待尝试路线",
            }],
        })

    result = _run_search_review(
        request=original_request,
        task={"acceptance_obligations": [{"id": "analysis"}]},
        agent=SimpleNamespace(run=run), session=reviewer, role=SEARCH_REVIEW_ROLE,
        output_root=tmp_path, completion=True,
    )
    assert len(requests) == 2 and requests[1]["review_format_errors"]
    assert "SOURCE_SESSION" not in requests[1]
    assert "environment" not in requests[1] and "actual_web_calls" not in requests[1]
    assert json.loads(reviewer.conversation.messages[0]["content"]) == original_request
    assert sum("SOURCE_SESSION" in item["content"]
               for item in reviewer.conversation.messages) == 1
    assert result["decision"] == "BLOCKED"


def test_failed_open_pagination_does_not_count_as_material_progress():
    from traceforge.reconstruction.search_environment import _search_recovery_state

    first = {"tool": "web_open", "url": "https://example.org/paper",
             "offset": 0, "success": False, "error": "HTTP 402"}
    network = SimpleNamespace(calls=[first], pages={})
    before = _search_recovery_state(network)
    network.calls.append({**first, "offset": 8000, "error": "HTTP 402, retry"})
    assert _search_recovery_state(network) == before


def test_ready_rejected_by_real_query_gate_also_reviews_recovery(tmp_path, monkeypatch):
    result, roles, reviews, sessions = scenario(tmp_path, monkeypatch, outcome="ready_gate")
    assert roles == ["search_completion", "search_completion", "search_review", "search_completion"]
    assert result["status"] == "ENVIRONMENT_READY"
    assert result["errors"] == []
    assert reviews[0]["environment"]["author_result"]["status"] == "READY"
    assert "SEARCH_COMPLETION_NO_PROGRESS" in reviews[0]["environment"]["errors"]
    assert reviews[0]["actual_web_calls"][0]["success"] is False
    assert all(session is sessions[0] for session in sessions)


@pytest.mark.parametrize("text", ["", "正文"])
def test_only_nonempty_successful_pagination_counts_as_reading(text):
    from traceforge.reconstruction.search_environment import _search_recovery_state

    first = {"tool": "web_open", "url": "https://example.org/paper",
             "offset": 8000, "success": True, "raw_sha256": "fixed", "text": text}
    network = SimpleNamespace(calls=[first], pages={})
    before = _search_recovery_state(network)
    network.calls.append({**first, "offset": 16000})
    assert (_search_recovery_state(network) != before) is bool(text)
