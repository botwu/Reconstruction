from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_fakes import tagged_record
from p1_fixtures import ToolCitingIntentRuntime, load_jsonl_row
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.sandbox import staging_user_texts
from traceforge.reconstruction.agents.session import AgentSession, execute_tool
from traceforge.reconstruction.intent_recovery import run_intent_recovery
from traceforge.reconstruction.session_source import (
    build_reconstruction_source,
    load_raw_line,
)

_AUDIT_RECORDS = Path(
    "/tmp/traceforge-screen-live10/ab2e5edb3b6889e47b49b2d9b71e239b85cc615a695bee8a976ec41796d89a40/private/records.jsonl"
)
_AUDIT_SESSIONS = Path(
    "/mnt/afs_toolcall/wujian1/Projects/data_back_workspace/R01/opus-4.8/sessions.jsonl"
)


class _CaptureRuntime:
    backend = "hermes-sandbox"
    model_name = "test-model"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.session: AgentSession | None = None
        self.instruction = ""

    def run(self, *, role, instruction, session, output_root):
        del role, output_root
        self.instruction = instruction
        self.session = session
        return AgentResult(
            role="intent",
            backend=self.backend,
            payload=self.payload,
            final_text=json.dumps(self.payload),
            completed=True,
        )


def _padded_source(*, user_index: int, text: str, extra_task: bool = False) -> dict:
    messages: list[dict] = [{"role": "assistant", "content": f"pad-{i}"} for i in range(user_index)]
    messages.append({"role": "user", "content": text})
    messages.append({"role": "assistant", "content": "还没做完"})
    if extra_task:
        messages.extend(
            [
                {"role": "user", "content": "另一个任务：查云服务套餐"},
                {"role": "assistant", "content": "还没查完"},
            ]
        )
    raw_line = json.dumps({"messages": messages, "meta": {}, "tools": []}, ensure_ascii=False)
    selected = [0, 1] if extra_task else [0]
    return build_reconstruction_source(raw_line=raw_line, record=tagged_record(raw_line, selected=selected))


def test_read_session_message_returns_one_turn() -> None:
    session = AgentSession(
        session_context=json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "第一句"},
                    {"role": "assistant", "content": "第二句"},
                ]
            },
            ensure_ascii=False,
        )
    )
    text = execute_tool("read_session_message", {"index": 0}, session)
    assert "第一句" in text
    assert "第二句" not in text
    assert execute_tool("read_session_message", {"index": 9}, session).startswith("error:")


def test_list_and_read_user_text_use_original_message_index() -> None:
    session = AgentSession(
        user_records=[
            {"id": "user:129", "message_index": 129, "text": "请修复 parser 第 40 行"}
        ]
    )
    listed = json.loads(execute_tool("list_user_texts", {}, session))
    assert listed == [
        {
            "id": "user:129",
            "message_index": 129,
            "index": 129,
            "preview": "请修复 parser 第 40 行",
            "chars": len("请修复 parser 第 40 行"),
        }
    ]
    assert execute_tool("read_user_text", {"id": "user:129"}, session) == "请修复 parser 第 40 行"
    assert execute_tool("read_user_text", {"index": 129}, session) == "请修复 parser 第 40 行"
    assert execute_tool("read_user_text", {"index": 0}, session) == "error: unknown user text id"
    assert execute_tool("read_user_text", {"id": "user:0"}, session) == "error: unknown user text id"
    assert execute_tool("read_user_text", {"id": "user:999"}, session) == "error: unknown user text id"


def test_two_tasks_keep_distinct_original_indexes() -> None:
    first = AgentSession(
        user_records=[{"id": "user:3", "message_index": 3, "text": "修复 foo"}]
    )
    second = AgentSession(
        user_records=[{"id": "user:10", "message_index": 10, "text": "查院校专业"}]
    )
    assert json.loads(execute_tool("list_user_texts", {}, first))[0]["id"] == "user:3"
    assert json.loads(execute_tool("list_user_texts", {}, second))[0]["id"] == "user:10"
    assert execute_tool("read_user_text", {"id": "user:10"}, first) == "error: unknown user text id"
    assert execute_tool("read_user_text", {"index": 3}, second) == "error: unknown user text id"


def test_staging_user_texts_aligns_filename_with_id(tmp_path: Path) -> None:
    dest = staging_user_texts(
        [{"id": "user:129", "message_index": 129, "text": "请修复 parser"}],
        tmp_path,
    )
    assert (dest / "user_129.txt").read_text(encoding="utf-8") == "请修复 parser"
    index = json.loads((dest / "index.json").read_text(encoding="utf-8"))
    assert index == [{"id": "user:129", "message_index": 129, "file": "user_129.txt"}]


def test_intent_session_and_gate_share_user_129(tmp_path: Path) -> None:
    source = _padded_source(user_index=129, text="请修复 parser 第 40 行")
    task = source["tasks"][0]
    runtime = _CaptureRuntime(
        {
            "task_id": task["task_id"],
            "task_instruction": "请修复 parser 第 40 行",
            "core_objective": "修复 parser",
            "acceptance_obligations": [
                {
                    "id": "obl-001",
                    "text": "修复 parser",
                    "evidence_ref_ids": ["user:129"],
                }
            ],
            "success_criteria": ["修复 parser"],
        }
    )
    result = run_intent_recovery(source=source, agent=runtime, output_root=tmp_path)
    assert result["status"] == "READY"
    assert runtime.session is not None
    assert runtime.session.user_records[0]["id"] == "user:129"
    assert runtime.session.user_records[0]["message_index"] == 129
    listed = json.loads(execute_tool("list_user_texts", {}, runtime.session))
    assert listed[0]["id"] == "user:129"
    assert '"id": "user:129"' in runtime.instruction
    assert result["task"]["acceptance_obligations"][0]["evidence_ref_ids"] == ["user:129"]


def test_intent_rejects_cross_task_and_unknown_ids(tmp_path: Path) -> None:
    source = _padded_source(user_index=3, text="修复 foo.py", extra_task=True)
    first = source["tasks"][0]
    second = source["tasks"][1]
    foreign = _CaptureRuntime(
        {
            "task_id": first["task_id"],
            "task_instruction": "修复 foo.py",
            "core_objective": "修复 foo",
            "acceptance_obligations": [
                {
                    "id": "obl-001",
                    "text": "修复 foo",
                    "evidence_ref_ids": [f"user:{second['message_indices'][0]}"],
                }
            ],
            "success_criteria": ["修复 foo"],
        }
    )
    gated = run_intent_recovery(source=source, agent=foreign, output_root=tmp_path / "cross")
    assert gated["status"] == "REVIEW"
    assert any(code.startswith("OBLIGATION_EVIDENCE_REQUIRED") for code in gated["errors"])
    unknown = _CaptureRuntime(
        {
            "task_id": first["task_id"],
            "task_instruction": "修复 foo.py",
            "core_objective": "修复 foo",
            "acceptance_obligations": [
                {
                    "id": "obl-001",
                    "text": "修复 foo",
                    "evidence_ref_ids": ["user:999"],
                }
            ],
            "success_criteria": ["修复 foo"],
        }
    )
    rejected = run_intent_recovery(source=source, agent=unknown, output_root=tmp_path / "unknown")
    assert rejected["status"] == "REVIEW"
    assert any(code.startswith("OBLIGATION_EVIDENCE_REQUIRED") for code in rejected["errors"])


@pytest.mark.parametrize("line_number", [3, 10])
def test_audit_line_intent_ids_match_tools(tmp_path: Path, line_number: int) -> None:
    record = load_jsonl_row(_AUDIT_RECORDS, line_number)
    if record is None or not _AUDIT_SESSIONS.is_file():
        pytest.skip("audit live10 records or opus-4.8 sessions.jsonl not available")
    raw_line = load_raw_line(
        _AUDIT_SESSIONS,
        line_number=line_number,
        line_sha256=str(record.get("line_sha256") or "") or None,
    )
    source = build_reconstruction_source(raw_line=raw_line, record=record)
    runtime = ToolCitingIntentRuntime()
    result = run_intent_recovery(
        source=source, agent=runtime, output_root=tmp_path / f"L{line_number}"
    )
    assert runtime.listed
    assert all(item["id"] == f"user:{item['message_index']}" for item in runtime.listed)
    assert all(not str(item["by_id"]).startswith("error:") for item in runtime.reads)
    assert all(item["by_id"] == item["by_index"] for item in runtime.reads)
    if any(item["message_index"] == 129 for item in runtime.listed):
        assert execute_tool("read_user_text", {"index": 129}, runtime.session)
        assert not execute_tool("read_user_text", {"index": 129}, runtime.session).startswith("error:")
        assert execute_tool("read_user_text", {"index": 0}, runtime.session).startswith("error:")
    cited = result["tasks"][0]["task"]["acceptance_obligations"][0]["evidence_ref_ids"]
    assert cited == [item["id"] for item in runtime.listed]
    assert result["status"] == "READY"



def test_intent_can_read_multiple_original_context_messages():
    from traceforge.reconstruction.agents import INTENT_ROLE
    assert {"read_session_message", "read_session_context"}.issubset(INTENT_ROLE.tools)
    session = AgentSession(session_context=json.dumps({"messages": [
        {"role": "assistant", "content": "部署采集工作流，尚未发布"},
        {"role": "user", "content": "先发布，再补采"},
        {"role": "assistant", "content": "需要先更新工作流版本"},
    ]}, ensure_ascii=False))
    first = execute_tool("read_session_message", {"index": 0}, session)
    session.tool_events.append({"name": "read_session_message", "ok": True})
    second = execute_tool("read_session_message", {"index": 2}, session)
    assert "部署采集工作流" in first
    assert "更新工作流版本" in second
    assert execute_tool("read_session_message", {"index": 3}, session).startswith("error:")



def test_intent_prompt_previews_context_without_changing_raw_session():
    from traceforge.reconstruction.intent_recovery import _prompt
    source = {"raw_session": {"messages": [
        {"role": "assistant", "content": "采集工作流尚未发布到生产"},
        {"role": "user", "content": "发布并补采"},
        {"role": "assistant", "content": "部署工作流并补采数据"},
        {"role": "user", "content": "另一个任务"},
    ]}}
    before = json.dumps(source, ensure_ascii=False)
    prompt = _prompt(source, {"task_id": "t"}, [{"id": "user:1", "message_index": 1, "text": "发布并补采"}], [])
    assert "采集工作流尚未发布到生产" in prompt
    assert "另一个任务" not in prompt
    assert json.dumps(source, ensure_ascii=False) == before


def test_intent_preserves_contract_and_original_review_gate(tmp_path: Path) -> None:
    contract = (
        "## Acceptance Contract\n"
        "criterion-1 和 criterion-2 均须提供独立证据。\n"
        "```acceptance-report\n"
        '{"criteriaSatisfied":[{"id":"criterion-1","status":"satisfied"},'
        '{"id":"criterion-2","status":"not-applicable"}],"customEvidence":[]}\n'
        "```"
    )
    source = _padded_source(
        user_index=2, text="只读审查，零个严重问题才允许通过。\n\n" + contract
    )
    task = source["tasks"][0]
    runtime = _CaptureRuntime({
        "task_id": task["task_id"],
        "task_instruction": "只读审查，然后按指定 schema 返回 acceptance-report。",
        "core_objective": "审查变更",
        "acceptance_obligations": [{
            "id": "obl-001", "text": "提供审查结论", "evidence_ref_ids": ["user:2"],
        }],
        "mandatory_constraints": ["只读，不执行 Git 命令。"],
    })
    result = run_intent_recovery(source=source, agent=runtime, output_root=tmp_path)
    assert result["status"] == "READY", result["errors"]
    instruction = result["task"]["task_instruction"]
    assert contract in instruction
    assert "零个严重问题才允许通过" in instruction
    assert "只读，不执行 Git 命令。" in instruction
    assert "customEvidence" in instruction
    assert instruction.count("```acceptance-report") == 1


def test_intent_reviews_missing_acceptance_schema(tmp_path: Path) -> None:
    source = _padded_source(user_index=2, text="返回指定格式的 acceptance-report。")
    task = source["tasks"][0]
    runtime = _CaptureRuntime({
        "task_id": task["task_id"],
        "task_instruction": "返回指定格式的 acceptance-report。",
        "core_objective": "审查变更",
        "acceptance_obligations": [{
            "id": "obl-001", "text": "返回 acceptance-report。", "evidence_ref_ids": ["user:2"],
        }],
    })
    result = run_intent_recovery(source=source, agent=runtime, output_root=tmp_path)
    assert result["status"] == "REVIEW"
    assert any(code.startswith("ACCEPTANCE_REPORT_SCHEMA_MISSING") for code in result["errors"])
    assert "criteriaSatisfied" not in result["task"]["task_instruction"]
