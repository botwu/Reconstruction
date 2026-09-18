from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from traceforge.cli import main
from traceforge.reconstruction.session_source import (
    ReconstructionSourceError,
    build_reconstruction_source,
    load_eligible_record,
)
from traceforge.screening.observable import build_spans


def _session() -> dict[str, object]:
    return {
        "messages": [
            {"role": "user", "content": "把 foo.py 里的入口函数读出来"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "exec",
                            "arguments": {"command": "cat foo.py"},
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "def main():\n    return 1\n"},
            {"role": "assistant", "content": "还没改完"},
        ],
        "meta": {},
        "tools": [],
        "domain_meta": {},
    }


def _record(raw_line: str, *, decision: str = "ELIGIBLE") -> dict[str, object]:
    payload = json.loads(raw_line)
    spans, _ = build_spans(payload["messages"])
    return {
        "decision": decision,
        "route": "ELIGIBLE_CODE_FILE",
        "source_ref": "jsonl:4:abcd",
        "line_number": 4,
        "line_sha256": "unused",
        "triage": {"selected_span_ids": [spans[0].span_id]},
    }


def test_source_omits_private_reasoning() -> None:
    session = _session()
    session["messages"][1]["reasoning"] = "secret chain of thought"
    session["messages"][1]["reasoning_content"] = "also secret"
    raw_line = json.dumps(session, ensure_ascii=False)
    source = build_reconstruction_source(raw_line=raw_line, record=_record(raw_line))
    dumped = json.dumps(source, ensure_ascii=False)
    assert "secret chain of thought" not in dumped
    assert "also secret" not in dumped
    assert source["privacy"]["private_thinking_reasoning"] == "omitted"
    assert source["raw_session"]["messages"][1]["tool_calls"][0]["function"]["name"] == "exec"


def test_source_reads_raw_session_without_compile() -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    source = build_reconstruction_source(raw_line=raw_line, record=_record(raw_line))
    assert source["policy"]["compile"] is False
    assert source["policy"]["evidence_join"] is False
    assert source["tasks"][0]["user_texts"] == ["把 foo.py 里的入口函数读出来"]
    assert source["tool_timeline"][0]["name"] == "exec"
    assert source["tool_timeline"][0]["arguments"]["command"] == "cat foo.py"
    assert source["tool_timeline"][0]["result_text"].startswith("def main")
    assert source["tool_timeline"][0]["pending"] is False


def test_source_rejects_non_eligible() -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    record = _record(raw_line, decision="REVIEW")
    with pytest.raises(ReconstructionSourceError, match="ELIGIBLE"):
        build_reconstruction_source(raw_line=raw_line, record=record)


def test_source_rejects_unknown_span() -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    record = _record(raw_line)
    record["triage"] = {"selected_span_ids": ["span_deadbeefdeadbeef"]}
    with pytest.raises(ReconstructionSourceError, match="对不上"):
        build_reconstruction_source(raw_line=raw_line, record=record)


def test_cli_source_writes_json(tmp_path: Path) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False) + "\n"
    input_path = tmp_path / "sessions.jsonl"
    input_path.write_text("ignored\n" * 3 + raw_line, encoding="utf-8")
    record = _current_record(raw_line)
    record["line_sha256"] = hashlib.sha256(raw_line.encode("utf-8")).hexdigest()
    records_path = tmp_path / "records.jsonl"
    records_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    output = tmp_path / "source"
    assert (
        main(
            [
                "reconstruct",
                "source",
                "--input",
                str(input_path),
                "--records",
                str(records_path),
                "--line-number",
                "4",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads((output / "reconstruction_source.json").read_text(encoding="utf-8"))
    assert payload["tasks"][0]["user_texts"][0].startswith("把 foo.py")
    assert (output / "initial_workspace/foo.py").read_text(encoding="utf-8").startswith("def main")
    replay = json.loads((output / "replay.json").read_text(encoding="utf-8"))
    assert replay["policy"]["trajectory_replay"] is False
    assert load_eligible_record(records_path, line_number=4)["decision"] == "ELIGIBLE"



def _current_record(raw_line: str) -> dict[str, object]:
    from traceforge.screening.contracts import TRIAGE_PROMPT_VERSION

    record = _record(raw_line)
    span_id = record["triage"]["selected_span_ids"][0]
    record["triage"] = {
        "prompt_version": TRIAGE_PROMPT_VERSION,
        "label_status": "COMPLETE",
        "tasks": [{
            "task_id": "task_incomplete",
            "span_ids": [span_id],
            "is_actionable": True,
            "outcome": "INCOMPLETE",
            "reconstruction_eligible": True,
            "eligibility": {"decision": "ELIGIBLE", "route": "ELIGIBLE_TASK"},
            "evidence_refs": {"span_ids": [span_id], "message_indices": [0, 3]},
        }],
        "relations": [],
    }
    return record


def test_v10_eligibility_derives_and_uses_selected_tag() -> None:
    from traceforge.reconstruction.intent_recovery import selected_task_views

    raw_line = json.dumps(_session(), ensure_ascii=False)
    record = _current_record(raw_line)
    source = build_reconstruction_source(raw_line=raw_line, record=record)
    assert source["selected_task_ids"] == ["task_incomplete"]
    assert "selected_for_reconstruction" in source["tasks"][0]["tags"]
    assert "screening_eligible" in source["session_tags"]
    assert len(selected_task_views(source)) == 1


def test_success_task_is_not_reenabled_by_source() -> None:
    from traceforge.reconstruction.intent_recovery import selected_task_views

    raw_line = json.dumps(_session(), ensure_ascii=False)
    record = _current_record(raw_line)
    task = record["triage"]["tasks"][0]
    task.update(outcome="SUCCESS", reconstruction_eligible=False,
                eligibility={"decision": "REJECT", "route": "SUCCESS_NOT_RECONSTRUCTED"})
    source = build_reconstruction_source(raw_line=raw_line, record=record)
    assert source["selected_task_ids"] == []
    assert selected_task_views(source) == []


@pytest.mark.parametrize("malformation", ["old_version", "missing_task_id", "missing_evidence"])
def test_load_rejects_incompatible_labels(tmp_path: Path, malformation: str) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    record = _current_record(raw_line)
    if malformation == "old_version":
        record["triage"]["prompt_version"] = "reconstruction-screening-triage-v9"
    elif malformation == "missing_task_id":
        record["triage"]["tasks"][0].pop("task_id")
    else:
        record["triage"]["tasks"][0].pop("evidence_refs")
    path = tmp_path / "records.jsonl"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ReconstructionSourceError):
        load_eligible_record(path, line_number=4)
