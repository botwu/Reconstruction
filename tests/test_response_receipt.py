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
    evaluate_response_contract,
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


def test_receipt_scope_does_not_promote_self_reported_success_to_semantic_verification() -> None:
    receipt = build_response_receipt(trajectory())
    assert receipt["verification_scope"] == "REPORT_STRUCTURE_ONLY"
    assert receipt["semantic_verified"] is False


def acceptance_contract() -> dict:
    return {
        "schema_version": "traceforge.response-contract.v1",
        "checks": [{
            "kind": "acceptance_report", "obligation_id": "obl-report",
            "criterion_ids": ["criterion-1"],
            "required_fields": {"criteriaSatisfied": "array", "changedFiles": "array",
                                "noStagedFiles": "boolean", "diffSummary": "string"},
        }],
    }


def test_machine_contract_accepts_structure_without_proving_self_reported_facts() -> None:
    outcome = evaluate_response_contract(trajectory(), acceptance_contract())
    assert outcome["verified_obligation_ids"] == ["obl-report"]
    assert outcome["checks"][0]["verification_scope"] == "REPORT_STRUCTURE_ONLY"
    assert outcome["semantic_verified"] is False


@pytest.mark.parametrize("field,value", [
    ("criterion_ids", ["criterion-1", "criterion-2"]),
    ("required_fields", {"manualNotes": "string"}),
    ("required_fields", {"noStagedFiles": "string"}),
])
def test_machine_contract_rejects_missing_criteria_fields_and_type_mismatches(field, value) -> None:
    contract = acceptance_contract()
    contract["checks"][0][field] = value
    assert evaluate_response_contract(trajectory(), contract)["verified_obligation_ids"] == []


def test_unknown_machine_check_does_not_clear_obligation() -> None:
    contract = acceptance_contract()
    contract["checks"][0]["kind"] = "semantic_correctness"
    assert evaluate_response_contract(trajectory(), contract)["verified_obligation_ids"] == []


def _summary_fixture(tmp_path, *, summary="APPROVED; Critical: 0; Important: 0; Minor: 1; review.md"):
    import hashlib

    report_bytes = b"APPROVED\nCritical: 0\nImportant: 0\nMinor: 1\nMinor issue: source.py:4 naming.\n"
    relative = "logs/artifacts/traceforge/workspace/review.md"
    target = tmp_path / "artifacts" / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(report_bytes)
    (tmp_path / "artifacts/manifest.json").write_text(json.dumps({
        "schema_version": "traceforge-harbor-artifacts/v1",
        "files": [{"path": relative, "size_bytes": len(report_bytes),
                   "sha256": hashlib.sha256(report_bytes).hexdigest()}],
    }))
    contract = {"schema_version": "traceforge.response-contract.v1", "checks": [{
        "kind": "basic_summary", "obligation_id": "obl-summary",
        "verdicts": ["APPROVED", "CHANGES_REQUIRED"],
        "finding_levels": ["Critical", "Important", "Minor"],
        "report_path": "review.md", "match_report": True,
    }]}
    return trajectory(summary), contract, target


def test_basic_summary_compares_real_manifest_bound_trial_report(tmp_path) -> None:
    data, contract, target = _summary_fixture(tmp_path)
    outcome = evaluate_response_contract(data, contract, trial_root=tmp_path)
    assert outcome["verified_obligation_ids"] == ["obl-summary"]
    check = outcome["checks"][0]
    assert check["verification_scope"] == "REPORT_CONSISTENCY_ONLY"
    assert check["report_sha256"]
    assert outcome["semantic_verified"] is False


@pytest.mark.parametrize("summary", [
    "APPROVED; Critical: 1; Important: 0; Minor: 1; review.md",
    "CHANGES_REQUIRED; Critical: 0; Important: 0; Minor: 1; review.md",
    "APPROVED; Critical: 0; Important: 0; Minor: 1; other.md",
    "APPROVED; Critical: 0; Important: 0; Minor: 1; review.md; everything is perfect",
])
def test_basic_summary_rejects_mismatch_or_extra_claims(tmp_path, summary) -> None:
    data, contract, _ = _summary_fixture(tmp_path, summary=summary)
    assert evaluate_response_contract(data, contract, trial_root=tmp_path)["verified_obligation_ids"] == []


def test_basic_summary_refuses_tampered_or_missing_report(tmp_path) -> None:
    data, contract, target = _summary_fixture(tmp_path)
    target.write_text("APPROVED\nCritical: 0\nImportant: 0\nMinor: 1\n")
    assert evaluate_response_contract(data, contract, trial_root=tmp_path)["verified_obligation_ids"] == []
    target.unlink()
    assert evaluate_response_contract(data, contract, trial_root=tmp_path)["verified_obligation_ids"] == []


def test_basic_summary_cannot_read_reference_or_escape_trial(tmp_path) -> None:
    data, contract, target = _summary_fixture(tmp_path)
    contract["checks"][0]["report_path"] = "../../solution/review.md"
    assert evaluate_response_contract(data, contract, trial_root=tmp_path)["verified_obligation_ids"] == []


def test_duplicate_contract_obligation_cannot_be_cleared_by_one_passing_check() -> None:
    contract = acceptance_contract()
    contract["checks"].append({**contract["checks"][0], "kind": "unsupported"})
    with pytest.raises(ResponseReceiptError, match="DUPLICATE"):
        evaluate_response_contract(trajectory(), contract)


def test_report_contract_status_claims_are_not_semantic_acceptance() -> None:
    value = report()
    value["criteriaSatisfied"][0]["status"] = "not-satisfied"
    value["noStagedFiles"] = False
    fence = chr(96) * 3
    data = trajectory(fence + "acceptance-report\n" + json.dumps(value) + "\n" + fence)
    outcome = evaluate_response_contract(data, acceptance_contract())
    assert outcome["verified_obligation_ids"] == ["obl-report"]
    assert outcome["semantic_verified"] is False


def test_basic_summary_refuses_symlink_into_reference_files(tmp_path) -> None:
    data, contract, target = _summary_fixture(tmp_path)
    reference = tmp_path / "reference.md"
    reference.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(reference)
    outcome = evaluate_response_contract(data, contract, trial_root=tmp_path)
    assert outcome["verified_obligation_ids"] == []
    assert "RESPONSE_REPORT_PATH_UNSAFE" in outcome["checks"][0]["errors"]


def test_malformed_contract_cannot_be_treated_as_empty_success() -> None:
    with pytest.raises(ResponseReceiptError, match="CHECKS_REQUIRED"):
        evaluate_response_contract(trajectory(), {"schema_version": "traceforge.response-contract.v1", "checks": 1})


def test_binding_only_receipt_round_trips_without_claiming_acceptance_report() -> None:
    data = trajectory("APPROVED; Critical: 0; Important: 0; Minor: 0; review.md")
    receipt = build_response_receipt(data, require_acceptance_report=False)
    assert receipt["report"] is None
    assert receipt["verification_scope"] == "FINAL_RESPONSE_BINDING_ONLY"
    assert verify_response_receipt(receipt, data)["semantic_verified"] is False
