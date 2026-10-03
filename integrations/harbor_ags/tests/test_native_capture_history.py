"""原生捕获链：完整 HTTP 重试不能绕过逐轮历史核验。"""

import copy

from harbor_ags import evidence
from harbor_ags.evidence import reconcile_evidence
from harbor_ags.validator import _call_transcript_prefix_issues, _context_compaction_detected

SUMMARY = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted.\n"
    "Earlier work is preserved in the capture.\n"
    "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
)


def _message(role, text):
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _call(history, text):
    return {
        "request": {"messages": copy.deepcopy(history), "tools": [{"name": "read"}]},
        "response": {
            "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        },
        "status": 200, "complete": True, "error_code": None,
    }


def _fixture(compact=False):
    history = [_message("user", "task")]
    calls = []
    for i in range(4):
        if compact and i == 3:
            history = [
                history[0], history[1], _message("user", SUMMARY),
                _message("assistant", "answer2"), _message("user", "turn3"),
            ]
        call = _call(history, f"answer{i}")
        calls.append(call)
        history += [_message("assistant", f"answer{i}")]
        if i < 3:
            history += [_message("user", f"turn{i + 1}")]
    messages = [
        {"role": m["role"], "content": m["content"][0]["text"]}
        for m in history
    ]
    return calls, messages


def _with_retry(calls):
    failed = copy.deepcopy(calls[2])
    failed.update(status=500, response={"type": "error", "error": {"message": "upstream"}})
    return [*calls[:2], failed, *calls[2:]]


def test_http_error_retry_is_not_compaction_and_history_is_checked():
    calls, messages = _fixture()
    calls = _with_retry(calls)
    assert not _context_compaction_detected(messages, calls)
    assert _call_transcript_prefix_issues(calls, messages) == []
    calls[2]["request"]["messages"][1]["content"][0]["text"] = "lost response"
    assert any(x.get("call_index") == 2 for x in _call_transcript_prefix_issues(calls, messages))


def test_real_summary_replaces_only_proven_history_window():
    calls, messages = _fixture(compact=True)
    assert evidence.compaction_windows(calls) == {3: {"retained": [0, 2], "compacted": [1]}}
    assert _context_compaction_detected(messages, calls)
    assert _call_transcript_prefix_issues(calls, messages) == []
    calls[1]["request"]["messages"][1]["content"][0]["text"] = "lost before compression"
    assert any(x.get("call_index") == 1 for x in _call_transcript_prefix_issues(calls, messages))


def test_summary_does_not_hide_new_missing_reply_after_boundary():
    calls, messages = _fixture(compact=True)
    call = _call(calls[-1]["request"]["messages"], "answer4")
    calls.append(call)  # 故意遗漏刚成功的 answer3；不能被既有摘要豁免。
    assert any(x.get("call_index") == 4 for x in _call_transcript_prefix_issues(calls, messages))


def test_marker_in_tool_content_or_initial_user_is_not_compaction():
    calls, messages = _fixture()
    messages[0]["content"] = SUMMARY
    assert not _context_compaction_detected(messages, calls)
    calls[-1]["request"]["messages"][2] = {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": SUMMARY}]
    }
    assert evidence.compaction_windows(calls) == {}


def test_forged_summary_with_unbound_retained_reply_is_not_compaction():
    calls, _ = _fixture(compact=True)
    calls[-1]["request"]["messages"][3]["content"][0]["text"] = "invented"
    assert evidence.compaction_windows(calls) == {}


def test_missing_successful_response_is_not_counted_as_compaction():
    calls, messages = _fixture()
    assert not _context_compaction_detected(messages[:-1], calls)
    assert _call_transcript_prefix_issues(calls, messages[:-1])


def test_http_200_error_payload_cannot_be_a_successful_reply():
    calls, messages = _fixture()
    calls[2]["response"] = {"type": "error", "error": {"message": "protocol failure"}}
    assert _call_transcript_prefix_issues(calls, messages)


def test_incomplete_exchange_remains_failure_despite_real_compaction():
    calls, messages = _fixture(compact=True)
    calls[1]["complete"] = False
    full = {"anthropic_calls": calls, "messages": messages}
    report = reconcile_evidence(full, {"steps": []})
    assert any(x["code"] == "INCOMPLETE_MODEL_EXCHANGE" for x in report["issues"])


def _raw_exchange(call, index):
    import base64
    import hashlib
    import json

    def body(raw, value=None):
        result = {
            "raw_base64": base64.b64encode(raw).decode(),
            "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
        }
        if value is not None:
            result["json"] = value
        return result

    request = call["request"]
    raw_request = json.dumps(request).encode()
    raw_response = b"" if not call["complete"] else json.dumps(call["response"]).encode()
    return {
        "exchange_id": f"x{index}", "request": body(raw_request, request),
        "request_headers": {"content-type": "application/json"},
        "method": "POST", "path": "/v1/messages", "upstream_url": "https://test.invalid",
        "started_at": f"2026-10-03T00:00:{index * 2:02d}+00:00",
        "finished_at": f"2026-10-03T00:00:{index * 2 + 1:02d}+00:00",
        "response_status": call["status"], "complete": call["complete"],
        "error_code": call["error_code"], "streaming": True, "sse_event_count": 0,
        "response": {"message": call["response"], "body": body(raw_response)},
    }


def test_empty_transport_retry_requires_original_raw_proof():
    from harbor_ags.capture import assemble_anthropic_sse

    calls, messages = _fixture()
    failed = copy.deepcopy(calls[2])
    failed.update(
        complete=False, error_code="UPSTREAM_TRANSPORT_ERROR",
        response=assemble_anthropic_sse([]),
    )
    calls.insert(2, failed)
    raw = [_raw_exchange(call, i) for i, call in enumerate(calls)]
    full = evidence.build_full_trajectory(raw, {"messages": messages})
    atif = evidence.project_atif_v17(full)
    assert _call_transcript_prefix_issues(full["anthropic_calls"], full["messages"]) == []
    without_raw = reconcile_evidence(full, atif)
    assert any(x["code"] == "INCOMPLETE_MODEL_EXCHANGE" for x in without_raw["issues"])
    report = reconcile_evidence(full, atif, exchanges=raw)
    assert report["ok"], report["issues"]
    assert report["compaction"]["detected"] is False
    assert any(x["code"] == "RETRIED_EMPTY_TRANSPORT_CAPTURE" for x in report["warnings"])

    changed = copy.deepcopy(raw)
    changed[2]["request_headers"]["extra"] = "different transport"
    assert any(
        x["code"] == "INCOMPLETE_MODEL_EXCHANGE"
        for x in reconcile_evidence(full, atif, exchanges=changed)["issues"]
    )
    changed_full = evidence.build_full_trajectory(changed, {"messages": messages})
    assert any(
        x["code"] == "INCOMPLETE_MODEL_EXCHANGE"
        for x in reconcile_evidence(changed_full, atif, exchanges=changed)["issues"]
    )


def test_received_response_body_is_never_empty_transport_retry():
    import base64
    import hashlib

    from harbor_ags.capture import assemble_anthropic_sse

    calls, messages = _fixture()
    failed = copy.deepcopy(calls[2])
    failed.update(
        complete=False, error_code="UPSTREAM_TRANSPORT_ERROR",
        response=assemble_anthropic_sse([]),
    )
    calls.insert(2, failed)
    raw = [_raw_exchange(call, i) for i, call in enumerate(calls)]
    received = b"data: partial"
    raw[2]["response"]["body"] = {
        "raw_base64": base64.b64encode(received).decode(),
        "sha256": hashlib.sha256(received).hexdigest(), "size_bytes": len(received),
    }
    full = evidence.build_full_trajectory(raw, {"messages": messages})
    atif = evidence.project_atif_v17(full)
    assert any(
        x["code"] == "INCOMPLETE_MODEL_EXCHANGE"
        for x in reconcile_evidence(full, atif, exchanges=raw)["issues"]
    )
