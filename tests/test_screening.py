from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from traceforge.cli import main
from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse
from traceforge.screening.contracts import ScreeningDecision, ScreeningRoute, TRIAGE_PROMPT_VERSION
from traceforge.screening.model_triage import _prompt
from traceforge.screening.observable import build_observable_evidence
from traceforge.screening.pipeline import run_reconstruction_screening
from traceforge.screening.rubric import admit_after_model, admit_after_tasks
from traceforge.screening.rules import decide_rule
from traceforge.screening.scan import scan_source_record
from traceforge.screening.task_labels import normalize_task_labels


def _line(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_scan_rejects_invalid_json() -> None:
    features = scan_source_record("{", line_number=1)
    decision = decide_rule(features)
    assert features["parse_ok"] is False
    assert decision["decision"] == "REJECT"
    assert decision["rule_pass"] is False


def test_rule_rejects_missing_user() -> None:
    features = scan_source_record(
        _line(
            {
                "messages": [{"role": "assistant", "content": "ok"}],
                "meta": {},
                "tools": [],
            }
        ),
        line_number=1,
    )
    decision = decide_rule(features)
    assert decision["decision"] == "REJECT"
    assert "NO_USER_TASK" in decision["blocking_reason_codes"]


def test_rule_rejects_missing_attempt() -> None:
    features = scan_source_record(
        _line(
            {
                "messages": [{"role": "user", "content": "帮我改这个文件"}],
                "meta": {},
                "tools": [],
            }
        ),
        line_number=1,
    )
    decision = decide_rule(features)
    assert decision["decision"] == "REJECT"
    assert "NO_AGENT_ATTEMPT" in decision["blocking_reason_codes"]


def test_rule_reviews_user_and_tool_attempt() -> None:
    features = scan_source_record(
        _line(
            {
                "messages": [
                    {"role": "user", "content": "帮我改这个文件"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "c1", "function": {"name": "read"}}],
                    },
                    {"role": "tool", "content": "print('x')"},
                ],
                "meta": {"leaf_response_status": "completed", "source_request_count": 1},
                "tools": [],
            }
        ),
        line_number=2,
    )
    decision = decide_rule(features)
    assert features["has_tool_activity"] is True
    assert decision["decision"] == "REVIEW"
    assert decision["rule_pass"] is True
    assert decision["route"] == "NEEDS_MODEL_TRIAGE"


def test_rule_defers_high_cost_session() -> None:
    messages = [{"role": "user", "content": "任务"}] + [
        {"role": "assistant", "content": "继续"} for _ in range(201)
    ]
    features = scan_source_record(
        _line({"messages": messages, "meta": {"source_request_count": 1}, "tools": []}),
        line_number=3,
    )
    decision = decide_rule(features)
    assert decision["decision"] == "DEFER"
    assert "ESTIMATED_COST_HIGH" in decision["blocking_reason_codes"]


def test_pipeline_writes_manifest_without_eligible(tmp_path: Path) -> None:
    source = tmp_path / "sessions.jsonl"
    source.write_text(
        "\n".join(
            [
                _line(
                    {
                        "messages": [{"role": "user", "content": "做任务"}],
                        "meta": {},
                        "tools": [],
                    }
                ),
                _line(
                    {
                        "messages": [
                            {"role": "user", "content": "做任务"},
                            {"role": "assistant", "content": "开始", "tool_calls": [{"id": "1"}]},
                        ],
                        "meta": {"source_request_count": 1},
                        "tools": [],
                    }
                ),
                "{",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    published = run_reconstruction_screening(input_path=source, output_root=tmp_path / "out")
    manifest = json.loads((published / "selection_manifest.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (published / "private/records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert manifest["decision_counts"]["ELIGIBLE"] == 0
    assert manifest["decision_counts"]["REJECT"] == 2
    assert manifest["decision_counts"]["REVIEW"] == 1
    assert manifest["eligible_source_refs"] == []
    assert manifest["policy"]["model_triage"] == "NOT_RUN"
    assert [row["decision"] for row in rows] == ["REJECT", "REVIEW", "REJECT"]


def test_screening_cli_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "in.jsonl"
    source.write_text(
        _line(
            {
                "messages": [
                    {"role": "user", "content": "任务"},
                    {"role": "assistant", "content": "好"},
                ],
                "meta": {},
                "tools": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    exit_code = main(
        [
            "screening",
            "run",
            "--input",
            str(source),
            "--output",
            str(tmp_path / "screen"),
            "--limit",
            "1",
            "--rules-only",
        ]
    )
    assert exit_code == 0
    published = Path(capsys.readouterr().out.strip())
    assert (published / "selection_manifest.json").is_file()


def _ready_rubric() -> dict[str, int]:
    return {
        "task_identifiability": 2,
        "failure_evidence": 2,
    }


def test_rubric_admits_code_file_failure() -> None:
    admitted = admit_after_model(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        outcome="FAILURE",
        needs_reconstruction=True,
        domain_route="code_file",
        rubric=_ready_rubric(),
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.ELIGIBLE.value
    assert admitted["route"] == ScreeningRoute.ELIGIBLE_CODE_FILE.value


def test_rubric_ignores_dropped_environment_and_privacy_scores() -> None:
    rubric = _ready_rubric()
    rubric["initial_environment_visibility"] = 0
    rubric["environment_completion_value"] = 0
    rubric["privacy_processability"] = 0
    admitted = admit_after_model(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        outcome="FAILURE",
        needs_reconstruction=True,
        domain_route="code_file",
        rubric=rubric,
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.ELIGIBLE.value


def test_rubric_ignores_dropped_cost_and_verifier_scores() -> None:
    rubric = _ready_rubric()
    rubric["estimated_cost"] = 0
    rubric["verifier_constructability"] = 0
    rubric["episode_boundary_confidence"] = 0
    admitted = admit_after_model(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        outcome="FAILURE",
        needs_reconstruction=True,
        domain_route="code_file",
        rubric=rubric,
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.ELIGIBLE.value


def test_rubric_does_not_admit_weak_task_or_missing_failure() -> None:
    weak_task = _ready_rubric()
    weak_task["task_identifiability"] = 1
    no_failure = _ready_rubric()
    no_failure["failure_evidence"] = 0
    weak = admit_after_model(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        outcome="FAILURE",
        needs_reconstruction=True,
        domain_route="code_file",
        rubric=weak_task,
        parse_errors=(),
    )
    missing = admit_after_model(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        outcome="INCOMPLETE",
        needs_reconstruction=True,
        domain_route="code_file",
        rubric=no_failure,
        parse_errors=(),
    )
    assert weak["decision"] == ScreeningDecision.REVIEW.value
    assert missing["decision"] == ScreeningDecision.REJECT.value


def test_observable_keeps_middle_failure_and_later_chat() -> None:
    raw = _line(
        {
            "messages": [
                {"role": "system", "content": "你是代码助手"},
                {"role": "user", "content": "修 README 安装步骤"},
                {
                    "role": "assistant",
                    "content": "先读文件",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {
                                "name": "exec",
                                "arguments": "{\"cmd\":\"cat README.md\"}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "c1",
                    "name": "exec",
                    "content": "cat: README.md: No such file or directory\nexit_code=1",
                },
                {"role": "assistant", "content": "README 不存在，安装步骤还没修好"},
                {"role": "user", "content": "猜我的人物画像"},
                {"role": "assistant", "content": "你像一位创造者"},
            ],
            "meta": {},
            "tools": [],
        }
    )
    features = scan_source_record(raw, line_number=1)
    evidence = build_observable_evidence(raw, features)
    dumped = json.dumps(evidence, ensure_ascii=False)
    assert len(evidence["spans"]) == 2
    assert "修 README 安装步骤" in dumped
    assert "No such file or directory" in dumped
    assert "exit_code=1" in dumped
    assert "猜我的人物画像" in dumped
    assert "你像一位创造者" in dumped
    assert evidence["shared_context"]
    assert evidence["shared_context"][0]["role"] == "system"


def test_observable_keeps_complete_huge_tool_body_and_marks_oversized() -> None:
    huge = "ERROR_LINE\n" + ("x" * 5000) + "\nTAIL_MARKER"
    raw = _line(
        {
            "messages": [
                {"role": "user", "content": "跑测试"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "t1", "function": {"name": "exec", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "t1", "name": "exec", "content": huge},
            ],
            "meta": {},
            "tools": [],
        }
    )
    features = scan_source_record(raw, line_number=1)
    evidence = build_observable_evidence(raw, features, max_input_chars=800)
    dumped = json.dumps(evidence, ensure_ascii=False)
    assert "ERROR_LINE" in dumped
    assert "TAIL_MARKER" in dumped
    assert evidence["serialization"]["truncated"] is False
    assert evidence["serialization"]["oversized"] is True
    assert "跑测试" in dumped
    assert "ERROR_LINE" in dumped
    assert "TAIL_MARKER" in dumped


def test_admit_after_tasks_selects_failed_span_among_success() -> None:
    admitted = admit_after_tasks(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        tasks=[
            {
                "span_ids": ["span_fail"],
                "outcome": "FAILURE",
                "needs_reconstruction": True,
                "domain_route": "code_file",
                "rubric": _ready_rubric(),
                "reason": "修文件失败",
            },
            {
                "span_ids": ["span_chat"],
                "outcome": "SUCCESS",
                "needs_reconstruction": False,
                "domain_route": "other",
                "rubric": _ready_rubric(),
                "reason": "闲聊完成",
            },
        ],
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.ELIGIBLE.value
    assert admitted["selected_span_ids"] == ("span_fail",)


def test_admit_after_tasks_rejects_all_success() -> None:
    admitted = admit_after_tasks(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        tasks=[
            {
                "span_ids": ["span_a"],
                "outcome": "SUCCESS",
                "needs_reconstruction": False,
                "domain_route": "code_file",
                "rubric": _ready_rubric(),
                "reason": "完成",
            },
            {
                "span_ids": ["span_b"],
                "outcome": "SUCCESS",
                "needs_reconstruction": False,
                "domain_route": "other",
                "rubric": _ready_rubric(),
                "reason": "完成",
            },
        ],
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.REJECT.value
    assert admitted["route"] == ScreeningRoute.SUCCESS_NOT_RECONSTRUCTED.value


def test_rubric_rejects_zero_task_score() -> None:
    rubric = _ready_rubric()
    rubric["task_identifiability"] = 0
    admitted = admit_after_model(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        outcome="INCOMPLETE",
        needs_reconstruction=True,
        domain_route="code_file",
        rubric=rubric,
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.REJECT.value


def test_rubric_cannot_override_rule_reject() -> None:
    admitted = admit_after_model(
        rule={
            "decision": "REJECT",
            "rule_pass": False,
            "blocking_reason_codes": ("NO_USER_TASK",),
        },
        outcome="FAILURE",
        needs_reconstruction=True,
        domain_route="code_file",
        rubric=_ready_rubric(),
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.REJECT.value
    assert admitted["route"] == ScreeningRoute.RULE_HARD_REJECT.value


def test_rubric_admits_valid_task_without_tools() -> None:
    admitted = admit_after_model(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        outcome="INCOMPLETE",
        needs_reconstruction=True,
        domain_route="other",
        rubric=_ready_rubric(),
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.ELIGIBLE.value
    assert admitted["route"] == ScreeningRoute.ELIGIBLE_TASK.value


def test_rubric_admits_retrieval_when_task_not_done() -> None:
    admitted = admit_after_model(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        outcome="FAILURE",
        needs_reconstruction=True,
        domain_route="retrieval",
        rubric=_ready_rubric(),
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.ELIGIBLE.value
    assert admitted["route"] == ScreeningRoute.ELIGIBLE_TASK.value


def test_admit_after_tasks_selects_incomplete_without_workspace_tools() -> None:
    admitted = admit_after_tasks(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        tasks=[
            {
                "span_ids": ["span_no_tool"],
                "outcome": "INCOMPLETE",
                "needs_reconstruction": True,
                "domain_route": "other",
                "rubric": _ready_rubric(),
                "reason": "有效任务但没有本该有的工具",
            }
        ],
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.ELIGIBLE.value
    assert admitted["route"] == ScreeningRoute.ELIGIBLE_TASK.value
    assert admitted["selected_span_ids"] == ("span_no_tool",)


class _FakeTriage:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def complete(self, request: ModelRequest) -> ModelResponse:
        assert request.response_schema == "traceforge.reconstruction-screening-triage.v2"
        return ModelResponse(
            request.request_id, request.model, "fake", json.dumps(self.payload), 1, 0.01
        )


def test_pipeline_model_can_mark_eligible(tmp_path: Path) -> None:
    source = tmp_path / "sessions.jsonl"
    payload = {
        "messages": [
            {"role": "user", "content": "修复 README 的安装步骤"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1", "function": {"name": "read"}}],
            },
            {"role": "tool", "name": "read", "content": "# 旧文档"},
        ],
        "meta": {"source_request_count": 1},
        "tools": [],
    }
    raw = _line(payload)
    source.write_text(raw + "\n", encoding="utf-8")
    features = scan_source_record(raw, line_number=1)
    evidence = build_observable_evidence(raw, features)
    span_id = evidence["span_ids"][0]
    published = run_reconstruction_screening(
        input_path=source,
        output_root=tmp_path / "out",
        model=_FakeTriage(
            {
                "tasks": [
                    {
                        "task_id": "task-1",
                        "span_ids": [span_id],
                        "is_actionable": True,
                        "outcome": "INCOMPLETE",
                        "needs_reconstruction": True,
                        "domain_route": "code_file",
                        "rubric": _ready_rubric(),
                        "reason": "用户要求改文档，尝试未完成",
                        "evidence_refs": {"message_indices": [0], "span_ids": [span_id]},
                    }
                ],
                "relations": [],
            }
        ),
        model_name="fake",
    )
    manifest = json.loads((published / "selection_manifest.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (published / "private/records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert manifest["decision_counts"]["ELIGIBLE"] == 1
    assert manifest["eligible_source_refs"]
    assert manifest["policy"]["model_triage"] == "RUN"
    assert rows[0]["triage"]["selected_span_ids"] == [span_id]


def _label_evidence() -> dict[str, object]:
    return {
        "source_ref": "capture-1",
        "span_ids": ["span_a", "span_b"],
        "span_audit": {"message_count": 6},
        "spans": [
            {"span_id": "span_a", "message_start": 0, "message_end": 3},
            {"span_id": "span_b", "message_start": 3, "message_end": 6},
        ],
    }


def _label_task(task_id: str, span_id: str, outcome: str, need: bool | None) -> dict[str, object]:
    return {
        "task_id": task_id,
        "span_ids": [span_id],
        "is_actionable": True,
        "outcome": outcome,
        "needs_reconstruction": need,
        "domain_route": "code_file",
        "rubric": _ready_rubric(),
        "reason": "有用户证据",
        "evidence_refs": {"message_indices": [0 if span_id == "span_a" else 3], "span_ids": [span_id]},
    }


def test_v10_labels_generate_stable_ids_and_validate_relations() -> None:
    payload = {
        "tasks": [
            _label_task("m-a", "span_a", "FAILURE", True),
            _label_task("m-b", "span_b", "INCOMPLETE", True),
        ],
        "relations": [
            {
                "from_task_id": "m-a",
                "to_task_id": "m-b",
                "type": "continuation",
                "evidence_refs": {"message_indices": [0, 3], "span_ids": ["span_a", "span_b"]},
                "reason": "后续继续同一交付物",
            }
        ],
    }
    first = normalize_task_labels(payload, evidence=_label_evidence(), source_ref="capture-1")
    second = normalize_task_labels(payload, evidence=_label_evidence(), source_ref="capture-1")
    tasks, relations, errors, status = first
    assert status == "COMPLETE"
    assert errors == ()
    assert tasks[0]["task_id"] == second[0][0]["task_id"]
    assert relations[0]["from_task_id"] == tasks[0]["task_id"]


def test_v10_rejects_overlapping_or_unanchored_relation() -> None:
    first = _label_task("m-a", "span_a", "FAILURE", True)
    second = _label_task("m-b", "span_b", "INCOMPLETE", True)
    second["span_ids"] = ["span_a", "span_b"]
    payload = {
        "tasks": [first, second],
        "relations": [
            {
                "from_task_id": "m-b",
                "to_task_id": "m-a",
                "type": "dependency",
                "evidence_refs": {"message_indices": [0], "span_ids": ["span_a"]},
                "reason": "逆序关系不应通过",
            }
        ],
    }
    _, _, errors, status = normalize_task_labels(
        payload, evidence=_label_evidence(), source_ref="capture-1"
    )
    assert status == "INVALID"
    assert any(error.startswith("TASK_SPAN_OVERLAP") for error in errors)
    assert any(error.startswith("RELATION_NOT_FORWARD") for error in errors)


def test_v9_labels_are_readable_but_fail_closed() -> None:
    legacy = {
        "tasks": [
            {
                "span_ids": ["span_a", "span_b"],
                "outcome": "FAILURE",
                "needs_reconstruction": True,
                "domain_route": "code_file",
                "rubric": _ready_rubric(),
                "reason": "旧记录没有详细证据标签",
            }
        ],
        "relations": [],
    }
    tasks, _, errors, status = normalize_task_labels(
        legacy, evidence=_label_evidence(), source_ref="capture-1"
    )
    assert status == "LEGACY_INCOMPLETE"
    assert tasks[0]["reconstruction_eligible"] is False
    assert "TASK_ID_REQUIRED:0" in errors
    admitted = admit_after_tasks(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        tasks=tasks,
        relations=[],
        label_status=status,
        parse_errors=errors,
    )
    assert admitted["decision"] == ScreeningDecision.REVIEW.value


def test_later_same_goal_success_suppresses_earlier_failure() -> None:
    tasks = [
        _label_task("m-a", "span_a", "FAILURE", True),
        _label_task("m-b", "span_b", "SUCCESS", False),
    ]
    admitted = admit_after_tasks(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        tasks=tasks,
        relations=[
            {
                "from_task_id": "m-a",
                "to_task_id": "m-b",
                "type": "correction",
            }
        ],
        label_status="COMPLETE",
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.REJECT.value
    assert tasks[0]["reconstruction_eligible"] is False
    assert "LATER_SAME_GOAL_SUCCESS" in tasks[0]["eligibility"]["blocking_reason_codes"]


def test_pipeline_concurrency_keeps_line_order(tmp_path: Path) -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "修文件"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "1", "function": {"name": "read"}}],
            },
            {"role": "tool", "name": "read", "content": "x"},
        ],
        "meta": {"source_request_count": 1},
        "tools": [],
    }
    source = tmp_path / "sessions.jsonl"
    source.write_text("\n".join(_line(payload) for _ in range(3)) + "\n", encoding="utf-8")

    class _SuccessTriage:
        def complete(self, request: ModelRequest) -> ModelResponse:
            found = re.findall(r"span_[0-9a-fA-F]+", request.prompt)
            evidence_span = found[0] if found else "span_missing"
            payload = {
                "tasks": [
                        {
                            "task_id": "task-1",
                            "span_ids": [evidence_span],
                            "is_actionable": True,
                            "outcome": "SUCCESS",
                        "needs_reconstruction": False,
                        "domain_route": "code_file",
                        "rubric": _ready_rubric(),
                            "reason": "完成",
                            "evidence_refs": {"message_indices": [0], "span_ids": [evidence_span]},
                        }
                    ],
                    "relations": [],
            }
            return ModelResponse(
                request.request_id,
                request.model,
                "fake",
                json.dumps(payload),
                1,
                0.01,
            )

    published = run_reconstruction_screening(
        input_path=source,
        output_root=tmp_path / "out",
        model=_SuccessTriage(),
        model_name="fake",
        concurrency=3,
    )
    rows = [
        json.loads(line)
        for line in (published / "private/records.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["line_number"] for row in rows] == [1, 2, 3]
    assert all(row["decision"] == "REJECT" for row in rows)


def test_triage_prompt_merges_by_final_completeness() -> None:
    text = _prompt({"span_ids": ["span_a"]})
    assert TRIAGE_PROMPT_VERSION == "reconstruction-screening-triage-v10"
    assert "按最终完成度合并" in text
    assert "中间 SUCCESS 后纠错仍要并" in text
