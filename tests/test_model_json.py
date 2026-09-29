"""模型调用失败、输出截断和结构修正必须留下不同的回执。"""

import json

import pytest

from traceforge.reconstruction.model_gateway import ModelGatewayError, ModelRequest, ModelResponse
from traceforge.reconstruction.model_json import ModelOutputError, complete_checked_json


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return ModelResponse(request.request_id, request.model, "fixture", response,
                             1, 0.1, finish_reason="stop")


def run(model, output):
    def validate(value):
        if value.get("value") != 1:
            raise ModelOutputError("value 必须为 1")

    return complete_checked_json(
        model=model, request=ModelRequest("fixture", "fixture", "解析", '{"source":"原文"}',
                                         "fixture.v1"), output_root=output, validate=validate,
    )


def test_feedback_keeps_original_context_and_every_response(tmp_path):
    model = Model(['{"value":2}', '{"value":1}'])
    result, attempt = run(model, tmp_path)
    assert result == {"value": 1}
    corrected = json.loads(model.requests[1].prompt)
    assert corrected["source"] == "原文"
    assert corrected["validation_feedback"]["error"] == "value 必须为 1"
    assert (tmp_path / "attempt-0001/response.txt").read_text() == '{"value":2}'
    assert (attempt / "response.txt").read_text() == '{"value":1}'


def test_transport_failure_does_not_enter_correction_loop(tmp_path):
    model = Model([ModelGatewayError("连接中断", code="UPSTREAM_TRANSPORT_ERROR")])
    with pytest.raises(ModelGatewayError):
        run(model, tmp_path)
    assert len(model.requests) == 1
    receipt = json.loads((tmp_path / "attempt-0001/receipt.json").read_text())
    assert receipt["status"] == "ERROR"
    assert receipt["error_code"] == "UPSTREAM_TRANSPORT_ERROR"


def test_repeated_invalid_output_stops_without_claiming_source_rejection(tmp_path):
    with pytest.raises(ModelOutputError, match="无进展"):
        run(Model(['{"value":2}', '{"value":2}']), tmp_path)
    receipt = json.loads((tmp_path / "attempt-0002/receipt.json").read_text())
    assert receipt["status"] == "ERROR"
    assert receipt["error_code"] == "NO_PROGRESS"


def test_truncated_json_is_saved_but_never_accepted(tmp_path):
    class TruncatedModel:
        def complete(self, request):
            return ModelResponse(request.request_id, request.model, "fixture", '{"value":1}',
                                 1, 0.1, finish_reason="length")

    with pytest.raises(ModelGatewayError, match="截断"):
        run(TruncatedModel(), tmp_path)
    attempt = tmp_path / "attempt-0001"
    assert (attempt / "response.txt").read_text() == '{"value":1}'
    assert not (attempt / "result.json").exists()


def test_resume_after_transport_error_keeps_real_response_and_feedback(tmp_path):
    first = Model(['{"value":2}', ModelGatewayError("连接中断", code="UPSTREAM_TRANSPORT_ERROR")])
    with pytest.raises(ModelGatewayError):
        run(first, tmp_path)
    resumed = Model(['{"value":1}'])
    value, attempt = run(resumed, tmp_path)
    assert value == {"value": 1}
    assert attempt.name == "attempt-0003"
    assert len(resumed.requests) == 1
    assert json.loads(resumed.requests[0].prompt)["validation_feedback"]["previous_output"] == '{"value":2}'
    assert (tmp_path / "attempt-0001/response.txt").read_text() == '{"value":2}'


def test_resume_rejects_other_source_without_calling_model(tmp_path):
    run(Model(['{"value":1}']), tmp_path)
    model = Model([])
    with pytest.raises(ModelOutputError, match="原始输入"):
        complete_checked_json(model=model, request=ModelRequest(
            "r", "fixture", "解析", '{"source":"另一条会话"}', "fixture.v1"),
            output_root=tmp_path, validate=lambda value: None)
    assert model.requests == []
