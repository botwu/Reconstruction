"""复核必须绑定真实工具返回，不能以预览或来源可访问性代替实际读取。"""

import hashlib
import json
from pathlib import Path

import pytest

from traceforge.reconstruction.agents.runtime import load_tool_results
from traceforge.reconstruction.agents.session import AgentSession, execute_tool


def recorded_read(root: Path) -> tuple[list[dict], str]:
    session = AgentSession(evidence=[{
        "evidence_ref_id": "captured:0", "result_text": "源码证据" * 4000,
    }])
    arguments = {"id": "captured:0", "offset": 7000, "limit": 1000}
    result = execute_tool("read_evidence", arguments, session)
    event = {
        "tool_call_id": "read-1", "name": "read_evidence", "arguments": arguments,
        "ok": True, "result_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
        "result_preview": result[:512],
    }
    path = root / "private/tool_events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in [
        {"status": "STARTED", "tool_call_id": "read-1", "name": "read_evidence",
         "arguments": arguments},
        {"status": "FINISHED", **event, "result": result},
    ]) + "\n")
    return [event], result


def rewrite_finished(root: Path, **changes) -> None:
    path = root / "private/tool_events.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[-1].update(changes)
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n")


def test_actual_partial_read_is_bound_without_claiming_full_text(tmp_path):
    events, result = recorded_read(tmp_path)
    bound = load_tool_results(tmp_path, events)
    assert bound == [{**events[0], "result": result}]
    assert len(bound[0]["result"]) > 512
    assert "continued; use offset=8000" in bound[0]["result"]
    assert "result" not in events[0]


@pytest.mark.parametrize("field,value", [
    ("tool_call_id", "another-call"), ("name", "web_open"),
    ("arguments", {"id": "captured:1"}), ("result_sha256", "0" * 64),
    ("ok", False),
])
def test_trace_must_match_current_execution_record(tmp_path, field, value):
    events, _ = recorded_read(tmp_path)
    rewrite_finished(tmp_path, **{field: value})
    with pytest.raises(ValueError, match="工具完整回执"):
        load_tool_results(tmp_path, events)


def test_body_tampering_is_detected_even_when_declared_hash_is_unchanged(tmp_path):
    events, result = recorded_read(tmp_path)
    rewrite_finished(tmp_path, result=result + "伪造尾部")
    with pytest.raises(ValueError, match="正文哈希"):
        load_tool_results(tmp_path, events)


@pytest.mark.parametrize("change", ["missing", "duplicate", "truncated"])
def test_missing_or_ambiguous_trace_cannot_be_guessed(tmp_path, change):
    events, _ = recorded_read(tmp_path)
    path = tmp_path / "private/tool_events.jsonl"
    if change == "missing":
        path.unlink()
    elif change == "duplicate":
        with path.open("a") as stream:
            stream.write(path.read_text().splitlines()[-1] + "\n")
    else:
        path.write_text(path.read_text() + '{"status":')
    with pytest.raises(ValueError, match="工具完整回执"):
        load_tool_results(tmp_path, events)


def test_no_tool_execution_does_not_require_a_trace_file(tmp_path):
    assert load_tool_results(tmp_path, []) == []


def test_nested_web_failure_is_preserved_for_review(tmp_path):
    result = execute_tool("web_open", {"url": "https://example.org/paper"}, AgentSession(
        web_open_handler=lambda *args, **kwargs: {"success": False, "error": "HTTP 500"},
    ))
    event = {"tool_call_id": "web-1", "name": "web_open",
             "arguments": {"url": "https://example.org/paper"}, "ok": True,
             "result_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
             "result_preview": result[:512]}
    path = tmp_path / "private/tool_events.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"status": "FINISHED", **event, "result": result}) + "\n")
    bound = load_tool_results(tmp_path, [event])
    assert bound[0]["ok"] is True
    assert json.loads(bound[0]["result"])["success"] is False


def review_fixture(root: Path) -> tuple[list[dict], str]:
    trial = root / "rollouts/trial-01"
    trial.mkdir(parents=True)
    events, result = recorded_read(trial)
    (trial / "execution.json").write_text(json.dumps({"tool_events": events}))
    (trial / "input.json").write_text(json.dumps({"evidence": [{
        "evidence_ref_id": "captured:0", "content_kind": "source_file",
    }]}))
    (trial / "receipt.json").write_text(json.dumps({"completed": True}))
    (trial / "answer.md").write_text("真实回答保持不变")
    return events, result


def test_researcher_receives_actual_return_instead_of_preview(tmp_path):
    from types import SimpleNamespace

    from traceforge.reconstruction.search_environment import _review_search_rollouts

    events, actual = review_fixture(tmp_path)

    def review(**kwargs):
        trial = json.loads(kwargs["instruction"])["trials"][0]
        assert trial["tool_events"] == [{**events[0], "result": actual}]
        assert trial["read_evidence_calls"][0]["source"]["evidence_ref_id"] == "captured:0"
        return SimpleNamespace(completed=True, errors=[], payload={
            "decision": "COMPLETE", "requirements": [{
                "obligation_id": "inspect", "status": "SUPPORTED",
                "reason": "已根据真实局部返回核查", "repair": "",
            }],
        })

    result = _review_search_rollouts(
        task={"acceptance_obligations": [{"id": "inspect"}]},
        environment={"evidence_handoff": {}, "captures": [], "requirement_coverage": [],
                     "context_messages": []},
        agent=SimpleNamespace(run=review), session=AgentSession(), output_root=tmp_path,
    )
    assert result["decision"] == "COMPLETE"


@pytest.mark.parametrize("missing_trace", [True, False])
def test_unbound_tool_return_stops_review_without_inventing_environment_gap(
    tmp_path, missing_trace,
):
    from types import SimpleNamespace

    from traceforge.reconstruction.search_environment import _review_search_rollouts

    review_fixture(tmp_path)
    trial = tmp_path / "rollouts/trial-01"
    if missing_trace:
        (trial / "private/tool_events.jsonl").unlink()
    else:
        rewrite_finished(trial, result="篡改后的返回")

    def review(**kwargs):
        pytest.fail("工具完整回执不可信时不能发起模型复核")

    result = _review_search_rollouts(
        task={"acceptance_obligations": [{"id": "inspect"}]},
        environment={"evidence_handoff": {}, "captures": [], "requirement_coverage": [],
                     "context_messages": []},
        agent=SimpleNamespace(run=review), session=AgentSession(), output_root=tmp_path,
    )
    assert result["failure_kind"] == "TRACE_UNAVAILABLE"
    assert result["decision"] == "BLOCKED"
    assert "工具完整回执" in result["error_detail"]
    assert "requirements" not in result
    assert json.loads((tmp_path / "researcher-review.json").read_text()) == result
    assert (trial / "answer.md").read_text() == "真实回答保持不变"


def test_trace_failure_is_reported_as_review_incomplete_by_pipeline(tmp_path):
    from types import SimpleNamespace

    from traceforge.reconstruction.search_environment import run_search_task

    def author(**kwargs):
        assert kwargs["role"].name == "search_completion"
        arguments = {"id": "captured:0"}
        actual = execute_tool("read_evidence", arguments, kwargs["session"])
        kwargs["session"].tool_events.append({
            "name": "read_evidence", "arguments": arguments, "ok": not actual.startswith("error:"),
        })
        return SimpleNamespace(completed=True, errors=[], payload={
            "status": "READY", "requires_live_web": False, "retrieval_reason": "原始源码比较",
            "excluded_events": [], "context_references": [], "missing_inputs": [],
            "requirement_coverage": [{
                "obligation_id": "inspect", "evidence_ref_ids": ["captured:0"],
                "reason": "已提供原始正文",
            }],
        })

    def solve(**kwargs):
        kwargs["session"].tool_events.append({
            "name": "read_evidence", "arguments": {"id": "captured:0"}, "ok": True,
        })
        return SimpleNamespace(completed=True, errors=[], turns=[], final_text="已有真实回答")

    source = {"raw_session": {"messages": [{"role": "tool", "content": "原始源码"}]},
              "tool_timeline": [{
                  "name": "Read", "result_text": "原始源码", "tool_message_index": 0,
              }]}
    result = run_search_task(
        source=source,
        task={"task_id": "inspect", "task_instruction": "比较源码",
              "acceptance_obligations": [{"id": "inspect"}]},
        agent=SimpleNamespace(run=author),
        rollout_agent=SimpleNamespace(run=solve, model_name="fixture"),
        rollout_trials=1, output_root=tmp_path,
    )
    assert result["environment_review"] == "REVIEW_INCOMPLETE"
    assert result["stopped_at"] == "researcher_review"
    assert result["errors"] == ["TOOL_TRACE_UNAVAILABLE"]
    assert json.loads((tmp_path / "environment.json").read_text())["status"] == "READY"
