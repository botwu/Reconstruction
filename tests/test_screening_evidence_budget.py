"""筛选模型必须收到完整证据，超预算时不得实际调用模型。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse
from traceforge.screening.model_triage import judge_reconstructability
from traceforge.screening.observable import (
    DEFAULT_MAX_INPUT_CHARS,
    build_observable_evidence,
    prepare_model_evidence,
)
from traceforge.screening.pipeline import run_reconstruction_screening
from traceforge.screening.rules import decide_rule
from traceforge.screening.scan import scan_source_record


class RecordingModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            request_id=request.request_id,
            model=request.model,
            provider="fixture",
            text='{"tasks": []}',
            attempts=1,
            latency_seconds=0,
        )


def evidence_for(tool_result: str) -> tuple[dict, dict]:
    raw = json.dumps(
        {
            "messages": [
                {"role": "system", "content": "SHARED_CONTEXT"},
                {"role": "user", "content": "修复 parser.py 的负数输入错误"},
                {
                    "role": "assistant",
                    "content": "FIRST_ATTEMPT",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {
                                "name": "exec_command",
                                "arguments": {"cmd": "python -m pytest"},
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": tool_result},
                {"role": "assistant", "content": "MIDDLE_FAILURE: 还未修复"},
                {"role": "user", "content": "请继续修复，保留正数行为"},
                {"role": "assistant", "content": "LATER_CORRECTION: 现在测试全部通过"},
            ],
            "meta": {},
            "tools": [],
        },
        ensure_ascii=False,
    )
    features = scan_source_record(raw, line_number=1)
    return build_observable_evidence(raw, features), decide_rule(features)


def test_small_evidence_preserves_all_events_in_actual_model_request() -> None:
    body = "ERROR_HEAD\n" + "middle-output\n" * 100 + "RESULT_TAIL"
    evidence, rule = evidence_for(body)
    original = copy.deepcopy(evidence)
    assert prepare_model_evidence(evidence) == original
    model = RecordingModel()
    judge_reconstructability(evidence=evidence, rule=rule, model=model, model_name="fixture")
    assert len(model.requests) == 1
    prompt = model.requests[0].prompt
    for marker in (
        "SHARED_CONTEXT", "FIRST_ATTEMPT", "MIDDLE_FAILURE",
        "LATER_CORRECTION", "ERROR_HEAD", "RESULT_TAIL", "保留正数行为",
    ):
        assert marker in prompt
    assert prompt.count("middle-output") == 100
    assert evidence == original


def test_oversized_evidence_returns_review_without_model_call_or_truncation() -> None:
    body = "FIRST_ERROR\n" + "x" * DEFAULT_MAX_INPUT_CHARS + "\nLATE_SUCCESS"
    evidence, rule = evidence_for(body)
    original = copy.deepcopy(evidence)
    model = RecordingModel()
    result = judge_reconstructability(
        evidence=evidence, rule=rule, model=model, model_name="fixture"
    )
    assert result["decision"] == "REVIEW"
    assert "OBSERVABLE_EVIDENCE_TOO_LARGE" in result["errors"]
    assert result["model_receipt"] is None
    assert model.requests == []
    assert evidence == original


@pytest.mark.parametrize("incomplete_flag", ["truncated", "model_input_compacted"])
def test_previously_shortened_evidence_cannot_be_admitted(incomplete_flag: str) -> None:
    evidence, rule = evidence_for("short result")
    evidence["serialization"][incomplete_flag] = True
    model = RecordingModel()
    result = judge_reconstructability(
        evidence=evidence, rule=rule, model=model, model_name="fixture"
    )
    assert result["decision"] == "REVIEW"
    assert "INCOMPLETE_OBSERVABLE_EVIDENCE" in result["errors"]
    assert model.requests == []


def test_pipeline_custom_budget_is_applied_before_model_call(tmp_path: Path) -> None:
    source = tmp_path / "sessions.jsonl"
    source.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "修复 parser.py"},
                    {
                        "role": "assistant",
                        "content": "尝试",
                        "tool_calls": [{"id": "c1", "function": {"name": "exec"}}],
                    },
                    {"role": "tool", "tool_call_id": "c1", "content": "x" * 2000},
                ],
                "meta": {},
                "tools": [],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    model = RecordingModel()
    published = run_reconstruction_screening(
        input_path=source,
        output_root=tmp_path / "out",
        model=model,
        model_name="fixture",
        concurrency=1,
        max_input_chars=100,
    )
    assert model.requests == []
    metrics = json.loads((published / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["max_input_chars"] == 100
    assert metrics["model_call_count"] == 1
