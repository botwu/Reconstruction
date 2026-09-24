from __future__ import annotations

import copy
import json

import pytest

from traceforge.harbor_ags.response_receipt import (
    RESPONSE_RECEIPT_SCHEMA,
    ResponseReceiptError,
    build_response_receipt,
    build_response_receipt_from_path,
    parse_acceptance_report,
    verify_response_receipt,
)
from traceforge.task_instruction import acceptance_report_criterion_ids


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


def _task_with_original_criteria() -> dict:
    shape = report()
    shape["criteriaSatisfied"].append({
        "id": "criterion-2", "status": "not-applicable", "evidence": "只读审查不改源码",
    })
    return {"source_task": {"user_texts": [
        "只读审查。\n\n## Acceptance Contract\n"
        "```acceptance-report\n" + json.dumps(shape) + "\n```"
    ]}}


def test_receipt_binds_all_original_criterion_ids(tmp_path) -> None:
    ids = acceptance_report_criterion_ids(_task_with_original_criteria())
    assert ids == ["criterion-1", "criterion-2"]
    complete = report()
    complete["criteriaSatisfied"].append({
        "id": "criterion-2", "status": "satisfied", "evidence": "独立审查证据",
    })
    data = trajectory("```acceptance-report\n" + json.dumps(complete) + "\n```")
    path = tmp_path / "trajectory.full.json"
    path.write_bytes(data)
    receipt = build_response_receipt_from_path(path, expected_criterion_ids=ids)
    assert verify_response_receipt(receipt, data, expected_criterion_ids=ids) == receipt


@pytest.mark.parametrize("ids", [
    ["criterion-1", "criterion-2"],
    ["other-criterion"],
    [],
])
def test_receipt_rejects_missing_unexpected_or_empty_expected_criteria(ids) -> None:
    with pytest.raises(ResponseReceiptError, match=r"CRITERIA_(MISMATCH|INVALID)"):
        build_response_receipt(trajectory(), expected_criterion_ids=ids)


def test_criterion_ids_cannot_be_invented_by_reconstructed_instruction() -> None:
    task = {"task_instruction": (
        "```acceptance-report\n" + json.dumps(report()) + "\n```"
    )}
    with pytest.raises(ValueError, match="ACCEPTANCE_REPORT_SCHEMA_MISSING"):
        acceptance_report_criterion_ids(task)


def test_criterion_ids_reject_duplicate_original_ids() -> None:
    task = _task_with_original_criteria()
    task["source_task"]["user_texts"][0] = task["source_task"]["user_texts"][0].replace(
        "criterion-2", "criterion-1"
    )
    with pytest.raises(ValueError, match="ACCEPTANCE_REPORT_SCHEMA_INVALID_CRITERIA"):
        acceptance_report_criterion_ids(task)


@pytest.mark.parametrize("field, value", [
    ("manualNotes", None),
    ("notes", ["不能用数组代替字符串"]),
])
def test_receipt_checks_optional_note_types(field, value) -> None:
    invalid = report()
    invalid[field] = value
    text = "```acceptance-report\n" + json.dumps(invalid) + "\n```"
    with pytest.raises(ResponseReceiptError, match="INVALID_REPORT_FIELD:" + field):
        parse_acceptance_report(text)


@pytest.mark.parametrize("command", [
    {"command": "pytest", "result": "passed"},
    {"command": "pytest", "result": "passed", "summary": 1},
])
def test_receipt_requires_command_summary_string(command) -> None:
    invalid = report()
    invalid["commandsRun"] = [command]
    text = "```acceptance-report\n" + json.dumps(invalid) + "\n```"
    with pytest.raises(ResponseReceiptError, match=r"INVALID_REPORT_FIELD:commandsRun.summary"):
        parse_acceptance_report(text)


def test_receipt_accepts_empty_optional_notes() -> None:
    complete = report()
    complete.update(manualNotes="", notes="")
    text = "```acceptance-report\n" + json.dumps(complete) + "\n```"
    assert parse_acceptance_report(text)[0] == complete
