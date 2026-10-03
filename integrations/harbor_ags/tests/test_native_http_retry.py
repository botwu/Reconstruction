"""只恢复由同原始请求后续完整成功覆盖的 HTTP 错误。"""

import base64
import copy
import hashlib
import json

import pytest
from harbor_ags.validator import _validate_capture_records


def _body(value):
    raw = json.dumps(value, sort_keys=True).encode()
    return {
        "raw_base64": base64.b64encode(raw).decode(), "json": value,
        "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _exchanges(status=500):
    request = _body({"model": "fixture", "messages": [{"role": "user", "content": "task"}]})
    base = {
        "method": "POST", "path": "/v1/messages", "upstream_url": "https://fixture.invalid",
        "request": request, "request_headers": {"content-type": "application/json"},
        "response_headers": {"content-type": "application/json"}, "duration_ms": 100,
        "streaming": False, "complete": True, "error_code": None, "sse_event_count": 0,
    }
    failed = dict(
        copy.deepcopy(base), exchange_id="failed", response_status=status,
        started_at="2026-10-03T00:00:00+00:00", finished_at="2026-10-03T00:00:01+00:00",
        response=_body({"type": "error", "error": {"type": "api_error", "message": "upstream"}}),
    )
    success = dict(
        copy.deepcopy(base), exchange_id="success", response_status=200,
        started_at="2026-10-03T00:00:02+00:00", finished_at="2026-10-03T00:00:03+00:00",
        response=_body({
            "id": "answer", "type": "message", "role": "assistant", "model": "fixture",
            "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }),
    )
    return [failed, success]


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_complete_error_retry_keeps_fact_without_rejecting_recovered_chain(status):
    exchanges = _exchanges(status)
    assert _validate_capture_records(exchanges, []) == []
    warnings = []
    assert _validate_capture_records(exchanges, [], warnings=warnings) == []
    assert warnings == [{
        "code": "RETRIED_COMPLETE_HTTP_ERROR", "exchange_id": "failed", "status": status,
    }]
    assert exchanges[0]["response_status"] == status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
def test_nonretryable_client_error_stays_failure(status):
    assert any(
        x["code"] == "MODEL_EXCHANGE_HTTP_FAILURE"
        for x in _validate_capture_records(_exchanges(status), [])
    )


@pytest.mark.parametrize("change", [
    "unrecovered", "headers", "raw_request", "time", "missing_raw", "partial",
    "streaming", "assistant_content", "tool_content", "thinking_content",
    "invalid_success", "malformed_success", "bad_success_raw",
])
def test_error_retry_does_not_hide_other_lost_evidence(change):
    exchanges = _exchanges()
    failed, success = exchanges
    if change == "unrecovered":
        exchanges.pop()
    elif change == "headers":
        success["request_headers"]["extra"] = "different identity"
    elif change == "raw_request":
        success["request"] = _body({"model": "fixture", "messages": []})
    elif change == "time":
        success["started_at"] = "2026-10-02T23:59:59+00:00"
    elif change == "missing_raw":
        failed["response"].pop("raw_base64")
    elif change == "partial":
        failed["complete"] = False
    elif change == "streaming":
        failed.update(streaming=True, sse_event_count=1)
    elif change in {"assistant_content", "tool_content", "thinking_content"}:
        block = {
            "assistant_content": {"type": "text", "text": "received answer"},
            "tool_content": {"type": "tool_use", "id": "t", "name": "x", "input": {}},
            "thinking_content": {"type": "thinking", "thinking": "received reasoning"},
        }[change]
        error = failed["response"]["json"]
        error["content"] = [block]
        failed["response"] = _body(error)
    elif change == "invalid_success":
        success["response"] = _body({"type": "error", "error": {"message": "still failed"}})
    elif change == "malformed_success":
        value = success["response"]["json"]
        value.pop("usage")
        success["response"] = _body(value)
    elif change == "bad_success_raw":
        success["response"]["sha256"] = "0" * 64
    assert any(
        x["code"] == "MODEL_EXCHANGE_HTTP_FAILURE"
        for x in _validate_capture_records(exchanges, [])
    )


def test_partial_sse_success_cannot_recover_previous_http_error():
    from harbor_ags.evidence import _superseded_transport_exchange_ids

    exchanges = _exchanges()
    success = exchanges[1]
    message = copy.deepcopy(success["response"]["json"])
    message.update(stop_reason=None, complete=False)
    message["content"] = [{"type": "text", "text": "only received part"}]
    frame = (
        "event: message_start\ndata: "
        + json.dumps({"type": "message_start", "message": message})
        + "\n\n"
    ).encode()
    success.update(
        streaming=True, complete=False, error_code="UPSTREAM_TRANSPORT_ERROR",
        sse_event_count=1, response={
            "message": message,
            "body": {
                "raw_base64": base64.b64encode(frame).decode(),
                "sha256": hashlib.sha256(frame).hexdigest(), "size_bytes": len(frame),
            },
        },
    )
    assert "failed" not in _superseded_transport_exchange_ids(exchanges, include_http_errors=True)
    assert any(
        x["code"] == "MODEL_EXCHANGE_HTTP_FAILURE"
        for x in _validate_capture_records(exchanges, [])
    )
