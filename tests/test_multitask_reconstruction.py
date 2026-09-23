from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.intent_recovery import IntentRecoveryError, run_intent_recovery
from traceforge.reconstruction.session_source import build_reconstruction_source
from traceforge.screening.observable import build_spans


def _record(payload: dict) -> dict:
    spans, _ = build_spans(payload["messages"])

    def task(span, indices, task_id):
        return {
            "task_id": task_id,
            "span_ids": [span.span_id],
            "evidence_refs": {"span_ids": [span.span_id], "message_indices": indices},
            "is_actionable": True,
            "outcome": "INCOMPLETE",
            "needs_reconstruction": True,
            "reconstruction_eligible": True,
            "rubric": {},
            "reason": "未完成",
            "tags": ["actionable", "incomplete", "needs_reconstruction", "rubric_pass", "selected_for_reconstruction"],
        }

    return {
        "decision": "ELIGIBLE",
        "source_ref": "session:test",
        "line_number": 1,
        "triage": {
            "tasks": [task(spans[0], [0, 1], "t1"), task(spans[1], [2, 3], "t2")],
            "relations": [],
            "label_status": "COMPLETE",
        },
    }


class _IntentAgent:
    model_name = "fake"

    def run(self, *, role, instruction, session, output_root):
        task_id = "t1" if "t1" in instruction else "t2"
        index = "0" if task_id == "t1" else "2"
        payload = {
            "task_id": task_id,
            "task_instruction": f"完成 {task_id}",
            "core_objective": f"完成 {task_id}",
            "acceptance_obligations": [
                {"id": "o1", "text": "完成任务", "evidence_ref_ids": [f"user:{index}"]}
            ],
            "success_criteria": ["完成任务"],
            "mandatory_constraints": [],
            "prohibitions": [],
        }
        return AgentResult(role=role.name, backend="fake", payload=payload, completed=True)


def test_source_and_intent_preserve_full_session_and_emit_multiple_tasks(tmp_path: Path):
    payload = {
        "messages": [
            {"role": "user", "content": "读取 foo.py"},
            {"role": "assistant", "content": "未完成"},
            {"role": "user", "content": "分析 bar.py"},
            {"role": "assistant", "content": "未完成"},
            {"role": "user", "content": "这句是闲聊"},
        ],
        "meta": {"capture": "immutable"},
    }
    source = build_reconstruction_source(raw_line=json.dumps(payload), record=_record(payload))
    assert len(source["raw_session"]["messages"]) == 5
    assert source["selected_task_ids"] == ["t1", "t2"]
    result = run_intent_recovery(source=source, agent=_IntentAgent(), output_root=tmp_path)
    assert result["status"] == "READY"
    assert [item["task"]["task_id"] for item in result["tasks"]] == ["t1", "t2"]
    assert all(item["task"]["evidence_refs"]["message_indices"] for item in result["tasks"])


def test_legacy_span_only_record_is_review_only():
    payload = {"messages": [{"role": "user", "content": "做事"}, {"role": "assistant", "content": "未完成"}]}
    spans, _ = build_spans(payload["messages"])
    record = {
        "decision": "ELIGIBLE",
        "source_ref": "legacy",
        "line_number": 1,
        "triage": {"selected_span_ids": [spans[0].span_id]},
    }
    source = build_reconstruction_source(raw_line=json.dumps(payload), record=record)
    assert source["label_status"] == "LEGACY_INCOMPLETE"
    assert source["selected_task_ids"] == []
    with pytest.raises(IntentRecoveryError, match="真实任务标签"):
        run_intent_recovery(source=source, agent=_IntentAgent(), output_root="/tmp/unused-intent")


def test_replay_does_not_downgrade_complete_file_after_later_write():
    result = replay_from_timeline(
        [
            {"name": "read", "call_id": "r", "arguments": {"path": ".env"}, "result_text": "x=1", "pending": False},
            {"name": "write", "call_id": "w", "arguments": {"path": ".env", "content": "x=2"}, "result_text": "ok", "pending": False},
            {"name": "read", "call_id": "e", "arguments": {"path": "new.py"}, "result_text": "error: timeout", "pending": False},
        ]
    )
    assert result.files[0].path == ".env"
    assert result.files[0].completeness == "COMPLETE"
    assert result.files[0].content == "x=1"
    assert result.withheld_changes[0].path == ".env"
    assert all(item.path != "new.py" for item in result.files)


def test_intent_to_environment_keeps_tagged_tool_evidence():
    from traceforge.reconstruction.eligible_reconstruction import _task_source

    tag = {"task_id": "t1", "span_ids": ["s1"]}
    source = {
        "raw_session": {"messages": [{"role": "user", "content": "修复入口"}]},
        "tasks": [tag],
        "tool_timeline": [
            {"call_id": "read1", "span_id": "s1", "name": "read", "arguments": {"path": "foo.py"}, "result_text": "pass\n", "pending": False},
            {"call_id": "read2", "span_id": "s2", "name": "read", "arguments": {"path": "unrelated.py"}, "result_text": "pass\n", "pending": False},
        ],
    }
    intent_task = {"task_id": "t1", "source_task": tag, "task_instruction": "修复入口"}
    result = _task_source(source, intent_task)
    assert result["selected_span_ids"] == ["s1"]
    assert [item["call_id"] for item in result["selected_tool_timeline"]] == ["read1"]
    assert result["selected_span_has_file_ops"] is True
    assert result["raw_session"] == source["raw_session"]
    assert len(result["tool_timeline"]) == 2
    assert result["session_timeline_scope"] == "FULL_SESSION"


def test_task_source_keeps_span_relations_for_intent() -> None:
    from traceforge.reconstruction.eligible_reconstruction import _task_source

    source = {
        "tasks": [{"task_id": "t1", "span_ids": ["s1"]}],
        "relations": [
            {"from_span_id": "s1", "to_span_id": "s2", "kind": "continuation"},
            {"from_span_id": "s3", "to_span_id": "s4", "kind": "unrelated"},
        ],
    }
    result = _task_source(
        source,
        {"task_id": "t1", "source_task": {"task_id": "t1", "span_ids": ["s1"]}},
    )
    assert result["relations"] == [source["relations"][0]]


def test_selected_span_file_ops_ignore_other_spans() -> None:
    from traceforge.reconstruction.eligible_reconstruction import _task_source

    tag = {"task_id": "t-ret", "span_ids": ["s-ret"], "domain_route": "retrieval"}
    source = {
        "raw_session": {"messages": [{"role": "user", "content": "查一下套餐"}]},
        "tasks": [tag],
        "tool_timeline": [
            {
                "call_id": "search",
                "span_id": "s-ret",
                "name": "browse_web",
                "arguments": {"url": "https://example.com"},
                "result_text": "ok",
                "pending": False,
            },
            {
                "call_id": "read1",
                "span_id": "s-file",
                "name": "read",
                "arguments": {"path": "foo.py"},
                "result_text": "pass\n",
                "pending": False,
            },
        ],
    }
    result = _task_source(source, {"task_id": "t-ret", "source_task": tag})
    assert [item["call_id"] for item in result["selected_tool_timeline"]] == ["search"]
    assert result["selected_span_has_file_ops"] is False
    assert len(result["tool_timeline"]) == 2


def test_replay_and_completion_use_full_session_tool_timeline(tmp_path: Path, monkeypatch) -> None:
    from traceforge.reconstruction import eligible_reconstruction as er
    from traceforge.reconstruction.terminal_universe_environment import ReplayResult, ReplayedFile

    tag = {"task_id": "t1", "span_ids": ["s1"]}
    source = {
        "raw_session": {"messages": [{"role": "user", "content": "修复入口"}]},
        "tasks": [tag],
        "relations": [],
        "session_tags": [],
        "tool_timeline": [
            {
                "call_id": "read1",
                "span_id": "s1",
                "name": "read",
                "arguments": {"path": "foo.py"},
                "result_text": "FOO\n",
                "pending": False,
            },
            {
                "call_id": "read2",
                "span_id": "s2",
                "name": "read",
                "arguments": {"path": "other.py"},
                "result_text": "OTHER\n",
                "pending": False,
            },
        ],
    }
    task = {"task_id": "t1", "source_task": tag, "task_instruction": "修复入口"}
    captured: dict[str, list[str]] = {}

    def fake_replay(timeline, destination=None, **kwargs):
        captured["replay_ids"] = [item.get("call_id") for item in timeline]
        return ReplayResult((ReplayedFile("foo.py", "FOO\n", "read1"), ReplayedFile("other.py", "OTHER\n", "read2")), (), (), ())

    def fake_completion(**kwargs):
        captured["completion_ids"] = [
            item.get("call_id") for item in (kwargs.get("timeline") or [])
        ]
        return {"status": "REVIEW", "errors": ["STOP_FOR_TEST"], "candidates": []}

    monkeypatch.setattr(er, "replay_from_timeline", fake_replay)
    monkeypatch.setattr(er, "write_replay_artifacts", lambda *args, **kwargs: None)
    monkeypatch.setattr(er, "complete_from_replayed", fake_completion)

    task_source, _, _, _ = er._replay_and_route(task=task, root=tmp_path, source=source)
    selected_ids = [item["call_id"] for item in task_source["selected_tool_timeline"]]
    assert captured["replay_ids"] == ["read1", "read2"]
    assert selected_ids == ["read1"]

    er._task_result(
        task=task,
        root=tmp_path,
        source=source,
        agent=object(),
        verification_model=None,
        verification_config=None,
        replay=ReplayResult((ReplayedFile("foo.py", "FOO\n", "read1"), ReplayedFile("other.py", "OTHER\n", "read2")), (), (), ()),
        support={"allow_completion": True, "reason_codes": []},
        task_source=task_source,
    )
    assert captured["completion_ids"] == ["read1", "read2"]
    assert captured["completion_ids"] != selected_ids
