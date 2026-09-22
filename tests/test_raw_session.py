from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from traceforge.reconstruction.raw_session import RawSessionSourceError, build_raw_session_source
from traceforge.screening.observable import build_spans


@dataclass
class FakeResult:
    payload: dict
    completed: bool = True
    errors: list[str] | None = None
    final_text: str = "{}"
    backend: str = "fake"
    turns: list[dict] | None = None


class FakeAgent:
    def __init__(self, payload: dict, completed: bool = True):
        self.payload = payload
        self.completed = completed
        self.role_name = None

    def run(self, *, role, instruction, session, output_root):
        self.role_name = role.name
        assert session.workspace is None
        assert "SPAN_CATALOG=" in instruction
        return FakeResult(
            self.payload,
            completed=self.completed,
            errors=[],
            final_text=json.dumps(self.payload),
            turns=[],
        )


def raw_session() -> str:
    return json.dumps(
        {
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "构建机器人环境"},
                {"role": "assistant", "content": "先检查环境"},
                {"role": "user", "content": "继续这个环境并保留约束"},
                {"role": "assistant", "content": "已经检查"},
                {"role": "user", "content": "另一个独立任务：写报告"},
                {"role": "assistant", "content": "收到"},
                {"role": "user", "content": "只是补充背景上下文"},
                {"role": "assistant", "content": "好的"},
            ]
        }
    )


def segmentation_payload(line: str) -> dict:
    messages = json.loads(line)["messages"]
    spans, _ = build_spans(messages)
    return {
        "tasks": [
            {
                "span_ids": [spans[0].span_id, spans[1].span_id],
                "evidence_refs": {"message_indices": [1, 3]},
                "task_kind": "implementation",
            },
            {
                "span_ids": [spans[2].span_id],
                "evidence_refs": {"message_indices": [5]},
                "task_kind": "report",
            },
        ],
        "context_span_ids": [spans[3].span_id],
        "relations": [
            {
                "from_span_id": spans[0].span_id,
                "to_span_id": spans[1].span_id,
                "kind": "continuation",
            }
        ],
        "label_status": "COMPLETE",
    }


def test_raw_session_groups_continuation_and_preserves_all_spans(tmp_path: Path) -> None:
    line = raw_session()
    agent = FakeAgent(segmentation_payload(line))
    source = build_raw_session_source(
        raw_line=line, line_number=7, source_ref="R04.jsonl", agent=agent, output_root=tmp_path
    )

    assert agent.role_name == "session_tasks"
    assert source["entry_mode"] == "RAW_SESSION"
    assert source["line_number"] == 7
    assert len(source["tasks"]) == 2
    assert len(source["tasks"][0]["span_ids"]) == 2
    assert source["tasks"][0]["intake_selected"] is True
    assert source["context_span_ids"]
    assert source["selected_task_ids"] == [task["task_id"] for task in source["tasks"]]
    assert "decision" not in source
    assert "rubric_pass" not in source
    assert all("reconstruction_eligible" not in task for task in source["tasks"])
    receipt = json.loads((tmp_path / "session_task_segmentation.json").read_text())
    assert receipt["status"] == "READY"
    assert receipt["assigned_span_count"] == receipt["span_count"]


def test_raw_session_rejects_overlap_and_persists_review_receipt(tmp_path: Path) -> None:
    line = raw_session()
    messages = json.loads(line)["messages"]
    spans, _ = build_spans(messages)
    payload = {
        "tasks": [
            {"span_ids": [spans[0].span_id], "evidence_refs": {"message_indices": [1]}},
            {"span_ids": [spans[0].span_id], "evidence_refs": {"message_indices": [1]}},
        ],
        "context_span_ids": [span.span_id for span in spans[1:]],
    }
    with pytest.raises(RawSessionSourceError, match="任务边界"):
        build_raw_session_source(
            raw_line=line,
            line_number=1,
            source_ref="R04",
            agent=FakeAgent(payload),
            output_root=tmp_path,
        )
    receipt = json.loads((tmp_path / "session_task_segmentation.json").read_text())
    assert receipt["status"] == "SESSION_TASK_REVIEW"
    assert any(error.startswith("SPAN_OVERLAP") for error in receipt["errors"])


def test_raw_session_never_drops_success_or_failure_span(tmp_path: Path) -> None:
    line = raw_session()
    messages = json.loads(line)["messages"]
    spans, _ = build_spans(messages)
    # 即使某个 span 看起来像上下文，遗漏它也必须进入 review。
    payload = {
        "tasks": [{"span_ids": [spans[0].span_id][:1], "evidence_refs": {"message_indices": [1]}}],
        "context_span_ids": [],
    }
    with pytest.raises(RawSessionSourceError):
        build_raw_session_source(
            raw_line=line,
            line_number=1,
            source_ref="R04",
            agent=FakeAgent(payload),
            output_root=tmp_path,
        )
    receipt = json.loads((tmp_path / "session_task_segmentation.json").read_text())
    assert any(error.startswith("UNASSIGNED_SPANS") for error in receipt["errors"])


def test_raw_session_invalid_input_persists_input_invalid(tmp_path: Path) -> None:
    with pytest.raises(RawSessionSourceError, match="合法 JSON"):
        build_raw_session_source(
            raw_line="{bad",
            line_number=3,
            source_ref="R05",
            agent=FakeAgent({}),
            output_root=tmp_path,
        )
    receipt = json.loads((tmp_path / "session_task_segmentation.json").read_text())
    assert receipt["status"] == "INPUT_INVALID"

def test_raw_session_evidence_subset_preserves_complete_task_scope(tmp_path: Path) -> None:
    line = raw_session()
    payload = segmentation_payload(line)
    payload["tasks"][0]["evidence_refs"]["message_indices"] = [1]
    source = build_raw_session_source(
        raw_line=line,
        line_number=1,
        source_ref="R04",
        agent=FakeAgent(payload),
        output_root=tmp_path,
    )

    task = source["tasks"][0]
    assert task["message_indices"] == [1, 3]
    assert task["user_texts"] == ["构建机器人环境", "继续这个环境并保留约束"]
    assert task["evidence_refs"]["message_indices"] == [1]


@pytest.mark.parametrize("indices", [[1, 5], [1, 2], [1, 99], [1, "3"], [True]])
def test_raw_session_rejects_evidence_outside_task_users(
    tmp_path: Path, indices: list[object]
) -> None:
    line = raw_session()
    payload = segmentation_payload(line)
    payload["tasks"][0]["evidence_refs"]["message_indices"] = indices
    with pytest.raises(RawSessionSourceError, match="任务边界"):
        build_raw_session_source(
            raw_line=line,
            line_number=1,
            source_ref="R04",
            agent=FakeAgent(payload),
            output_root=tmp_path,
        )

    receipt = json.loads((tmp_path / "session_task_segmentation.json").read_text())
    assert receipt["status"] == "SESSION_TASK_REVIEW"
    assert "TASK_0_EVIDENCE_SCOPE_MISMATCH" in receipt["errors"]
