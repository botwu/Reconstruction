from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.cli import main
from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse
from traceforge.screening.contracts import ScreeningDecision, ScreeningRoute
from traceforge.screening.observable import build_observable_evidence
from traceforge.screening.pipeline import run_reconstruction_screening
from traceforge.screening.rubric import admit_after_model, admit_after_tasks
from traceforge.screening.rules import decide_rule
from traceforge.screening.scan import scan_source_record


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


def test_observable_bounds_huge_tool_body_keeps_user() -> None:
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
    assert "omitted_chars=" in dumped
    assert "hash=" in dumped
    assert evidence["serialization"]["truncated"] is True
    assert huge not in dumped
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


def test_rubric_reviews_when_no_environment_handle() -> None:
    admitted = admit_after_model(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        outcome="FAILURE",
        needs_reconstruction=True,
        domain_route="other",
        rubric=_ready_rubric(),
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.REVIEW.value


def test_rubric_defers_retrieval() -> None:
    admitted = admit_after_model(
        rule={"decision": "REVIEW", "rule_pass": True, "blocking_reason_codes": ()},
        outcome="FAILURE",
        needs_reconstruction=True,
        domain_route="retrieval",
        rubric=_ready_rubric(),
        parse_errors=(),
    )
    assert admitted["decision"] == ScreeningDecision.DEFER.value
    assert admitted["route"] == ScreeningRoute.RETRIEVAL_BACKEND_NOT_READY.value


class _FakeTriage:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def complete(self, request: ModelRequest) -> ModelResponse:
        assert request.response_schema == "traceforge.reconstruction-screening-triage.v1"
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
                        "span_ids": [span_id],
                        "outcome": "INCOMPLETE",
                        "needs_reconstruction": True,
                        "domain_route": "code_file",
                        "rubric": _ready_rubric(),
                        "reason": "用户要求改文档，尝试未完成",
                    }
                ]
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
