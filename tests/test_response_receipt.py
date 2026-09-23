from __future__ import annotations

import copy
import json

import pytest

from traceforge.harbor_ags.response_receipt import (
    RESPONSE_RECEIPT_SCHEMA,
    ResponseReceiptError,
    build_response_receipt,
    parse_acceptance_report,
    verify_response_receipt,
)


def report() -> dict:
    return {
        "criteriaSatisfied": [{"id": "criterion-1", "status": "satisfied", "evidence": "ok"}],
        "changedFiles": [],
        "testsAddedOrUpdated": [],
        "commandsRun": [{"command": "pytest", "result": "passed", "summary": "ok"}],
        "validationOutput": ["passed"],
        "residualRisks": [],
        "noStagedFiles": True,
        "diffSummary": "review only",
        "reviewFindings": ["no blockers"],
    }


def trajectory(text: str | None = None) -> bytes:
    response = text or "done\n```acceptance-report\n" + json.dumps(report()) + "\n```"
    return json.dumps(
        {
            "schema_version": "traceforge-lossless-trajectory-v1",
            "messages": [
                {"role": "user", "content": "review"},
                {"role": "assistant", "content": response},
            ],
        },
        ensure_ascii=False,
    ).encode()


def test_receipt_accepts_terminal_fenced_report_and_binds_hashes() -> None:
    data = trajectory()
    receipt = build_response_receipt(data)
    assert receipt["schema_version"] == RESPONSE_RECEIPT_SCHEMA
    assert receipt["assistant_message_index"] == 1
    assert verify_response_receipt(receipt, data)["report"] == report()


def test_receipt_rejects_trailing_text() -> None:
    response = "done\n```acceptance-report\n" + json.dumps(report()) + "\n```\ntrailing"
    with pytest.raises(ResponseReceiptError, match="ACCEPTANCE_REPORT_NOT_TERMINAL"):
        build_response_receipt(trajectory(response))


def test_receipt_rejects_invalid_status() -> None:
    invalid = copy.deepcopy(report())
    invalid["criteriaSatisfied"][0]["status"] = "passed"
    text = "```acceptance-report\n" + json.dumps(invalid) + "\n```"
    with pytest.raises(ResponseReceiptError, match="INVALID_CRITERION_STATUS"):
        parse_acceptance_report(text)


def test_receipt_rejects_hash_or_report_tampering() -> None:
    data = trajectory()
    receipt = build_response_receipt(data)
    tampered = dict(receipt)
    tampered["response_sha256"] = "0" * 64
    with pytest.raises(ResponseReceiptError, match="RECEIPT_BINDING_MISMATCH:response_sha256"):
        verify_response_receipt(tampered, data)
    tampered = dict(receipt)
    tampered["report"] = {**receipt["report"], "noStagedFiles": False}
    with pytest.raises(ResponseReceiptError, match="RECEIPT_REPORT_MISMATCH"):
        verify_response_receipt(tampered, data)


def test_receipt_rejects_non_terminal_tool_message() -> None:
    data = json.loads(trajectory().decode())
    data["messages"].append({"role": "tool", "content": "late"})
    with pytest.raises(ResponseReceiptError, match="NOT_TERMINAL"):
        build_response_receipt(json.dumps(data).encode())


def test_receipt_rejects_duplicate_or_empty_criteria() -> None:
    fence = chr(96) * 3 + "acceptance-report\n"
    end = "\n" + chr(96) * 3
    invalid = copy.deepcopy(report())
    invalid["criteriaSatisfied"].append(dict(invalid["criteriaSatisfied"][0]))
    with pytest.raises(ResponseReceiptError, match="DUPLICATE_CRITERION_ID"):
        parse_acceptance_report(fence + json.dumps(invalid) + end)
    invalid = copy.deepcopy(report())
    invalid["criteriaSatisfied"] = []
    with pytest.raises(ResponseReceiptError, match="criteriaSatisfied"):
        parse_acceptance_report(fence + json.dumps(invalid) + end)
