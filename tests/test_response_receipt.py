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


def _summary_fixture(
    tmp_path, *, summary="APPROVED; Critical: 0; Important: 0; Minor: 1; review.md",
    report_text: str | None = None,
):
    import hashlib

    report_bytes = (report_text or "APPROVED\nCritical: 0\nImportant: 0\nMinor: 1\nMinor issue: source.py:4 naming.\n").encode("utf-8")
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


_SECTION_REVIEW = """# Code Review
## Verdict
CHANGES_REQUIRED

## Critical Findings
- None.

## Important
- file.py:12 - cancellation loses its owner.
  - Reproduction detail supporting the same finding.

## Minor
1. file.py:22 - misleading name.

## Strengths
- The successful path preserves ownership.
"""


def test_summary_counts_top_level_findings_without_requiring_numeric_report_fields(tmp_path) -> None:
    data, contract, _ = _summary_fixture(
        tmp_path, report_text=_SECTION_REVIEW,
        summary="CHANGES_REQUIRED; Critical: 0; Important: 1; Minor: 1; review.md",
    )
    outcome = evaluate_response_contract(data, contract, trial_root=tmp_path)
    assert outcome["verified_obligation_ids"] == ["obl-summary"]


def test_report_main_verdict_ignores_historical_verdict_mentions(tmp_path) -> None:
    report_text = _SECTION_REVIEW.replace(
        "## Critical Findings",
        "Earlier reviewers wrote APPROVED. The rubric explains CHANGES_REQUIRED.\n"
        "> Historical verdict: APPROVED\n\n## Critical Findings",
    )
    data, contract, _ = _summary_fixture(
        tmp_path, report_text=report_text,
        summary="CHANGES_REQUIRED; Critical: 0; Important: 1; Minor: 1; review.md",
    )
    assert evaluate_response_contract(data, contract, trial_root=tmp_path)["verified_obligation_ids"] == ["obl-summary"]


def test_summary_rejects_real_count_mismatch_against_section_findings(tmp_path) -> None:
    data, contract, _ = _summary_fixture(
        tmp_path, report_text=_SECTION_REVIEW,
        summary="CHANGES_REQUIRED; Critical: 0; Important: 0; Minor: 1; review.md",
    )
    outcome = evaluate_response_contract(data, contract, trial_root=tmp_path)
    assert outcome["verified_obligation_ids"] == []
    assert outcome["checks"][0]["errors"] == ["RESPONSE_SUMMARY_REPORT_MISMATCH"]


@pytest.mark.parametrize("replacement", [
    "",
    "- None.\n- file.py:9 - actual issue.",
    "There may be another problem.",
])
def test_uncertain_finding_section_is_not_guessed_as_zero(tmp_path, replacement) -> None:
    data, contract, _ = _summary_fixture(
        tmp_path, report_text=_SECTION_REVIEW.replace("- None.", replacement),
        summary="CHANGES_REQUIRED; Critical: 0; Important: 1; Minor: 1; review.md",
    )
    assert evaluate_response_contract(data, contract, trial_root=tmp_path)["verified_obligation_ids"] == []


def test_explicit_primary_verdict_takes_precedence_over_nested_history_heading(tmp_path) -> None:
    report_text = _SECTION_REVIEW + "\n## History\n### Verdict\nAPPROVED\n"
    data, contract, _ = _summary_fixture(
        tmp_path, report_text=report_text,
        summary="CHANGES_REQUIRED; Critical: 0; Important: 1; Minor: 1; review.md",
    )
    assert evaluate_response_contract(data, contract, trial_root=tmp_path)["verified_obligation_ids"] == ["obl-summary"]


def test_explicit_numeric_severity_headings_still_match_summary(tmp_path) -> None:
    report_text = (
        "# Review\n## Verdict: CHANGES_REQUIRED\n"
        "## Critical (0)\n- None.\n## Important (1)\n- file.py:12 - issue.\n"
        "## Minor Findings (1)\n- file.py:22 - naming.\n"
    )
    data, contract, _ = _summary_fixture(
        tmp_path, report_text=report_text,
        summary="CHANGES_REQUIRED; Critical: 0; Important: 1; Minor: 1; review.md",
    )
    assert evaluate_response_contract(data, contract, trial_root=tmp_path)["verified_obligation_ids"] == ["obl-summary"]


def test_conflicting_explicit_section_count_is_not_ignored(tmp_path) -> None:
    report_text = _SECTION_REVIEW.replace("## Important", "## Important (2)")
    data, contract, _ = _summary_fixture(
        tmp_path, report_text=report_text,
        summary="CHANGES_REQUIRED; Critical: 0; Important: 1; Minor: 1; review.md",
    )
    assert evaluate_response_contract(data, contract, trial_root=tmp_path)["verified_obligation_ids"] == []


def test_explicit_report_contract_does_not_require_unrequested_legacy_fields() -> None:
    value = {"criteriaSatisfied": report()["criteriaSatisfied"], "summary": "finished"}
    contract = acceptance_contract()
    contract["checks"][0]["required_fields"] = {"criteriaSatisfied": "array", "summary": "string"}
    fence = chr(96) * 3
    data = trajectory(fence + "acceptance-report\n" + json.dumps(value) + "\n" + fence)
    assert evaluate_response_contract(data, contract)["verified_obligation_ids"] == ["obl-report"]
    with pytest.raises(ResponseReceiptError, match="FIELD_REQUIRED"):
        build_response_receipt(data)


def test_explicit_report_without_criteria_uses_only_declared_fields() -> None:
    contract = acceptance_contract()
    contract["checks"][0].update(criterion_ids=[], required_fields={"summary": "string"})
    fence = chr(96) * 3
    data = trajectory(fence + 'acceptance-report\n{"summary":"finished"}\n' + fence)
    assert evaluate_response_contract(data, contract)["verified_obligation_ids"] == ["obl-report"]


@pytest.mark.parametrize("value", [
    {"criteriaSatisfied": [{"id": "criterion-1", "status": "passed", "evidence": "specific"}]},
    {"criteriaSatisfied": [{"id": "criterion-1", "status": "satisfied", "evidence": ""}]},
])
def test_explicit_contract_keeps_supported_nested_criteria_validation(value) -> None:
    contract = acceptance_contract()
    contract["checks"][0]["required_fields"] = {"criteriaSatisfied": "array"}
    fence = chr(96) * 3
    data = trajectory(fence + "acceptance-report\n" + json.dumps(value) + "\n" + fence)
    assert evaluate_response_contract(data, contract)["verified_obligation_ids"] == []


def test_contract_binding_only_receipt_does_not_claim_schema_validation() -> None:
    fence = chr(96) * 3
    data = trajectory(fence + 'acceptance-report\n{"summary":"finished"}\n' + fence)
    receipt = build_response_receipt(data, validate_report_schema=False)
    assert receipt["verification_scope"] == "REPORT_BINDING_ONLY"
    assert verify_response_receipt(receipt, data)["verification_scope"] == "REPORT_BINDING_ONLY"


@pytest.mark.parametrize("field,kind,value", [
    ("commandsRun", "array", [{"command": "pytest", "result": "unknown"}]),
    ("changedFiles", "array", [{"path": "file.py"}]),
    ("custom", "unsupported-type", "value"),
])
def test_explicit_contract_does_not_pass_unsupported_shapes(field, kind, value) -> None:
    contract = acceptance_contract()
    contract["checks"][0].update(criterion_ids=[], required_fields={field: kind})
    fence = chr(96) * 3
    data = trajectory(fence + "acceptance-report\n" + json.dumps({field: value}) + "\n" + fence)
    assert evaluate_response_contract(data, contract)["verified_obligation_ids"] == []


@pytest.mark.parametrize("summary", [None, 7, {"value": "passed"}])
def test_explicit_item_schema_rejects_missing_or_wrong_summary(summary) -> None:
    value = report()
    if summary is None:
        value["commandsRun"][0].pop("summary")
    else:
        value["commandsRun"][0]["summary"] = summary
    contract = acceptance_contract()
    check = contract["checks"][0]
    check["required_fields"]["commandsRun"] = "array"
    check["required_item_fields"] = {
        "commandsRun": {"command": "string", "result": "string", "summary": "string"},
    }
    fence = chr(96) * 3
    data = trajectory(fence + "acceptance-report\n" + json.dumps(value) + "\n" + fence)
    outcome = evaluate_response_contract(data, contract)
    assert outcome["verified_obligation_ids"] == []


def test_explicit_item_schema_validates_criterion_fields_and_keeps_structure_scope() -> None:
    contract = acceptance_contract()
    contract["checks"][0]["required_item_fields"] = {
        "criteriaSatisfied": {"id": "string", "status": "string", "evidence": "string"},
    }
    outcome = evaluate_response_contract(trajectory(), contract)
    assert outcome["verified_obligation_ids"] == ["obl-report"]
    assert outcome["semantic_verified"] is False
    assert outcome["checks"][0]["verification_scope"] == "REPORT_STRUCTURE_ONLY"


def test_undeclared_command_summary_is_not_required_by_explicit_or_legacy_parser() -> None:
    value = report()
    value["commandsRun"][0].pop("summary")
    fence = chr(96) * 3
    response = fence + "acceptance-report\n" + json.dumps(value) + "\n" + fence
    assert parse_acceptance_report(response)[0] == value
    contract = acceptance_contract()
    check = contract["checks"][0]
    check["required_fields"]["commandsRun"] = "array"
    check["required_item_fields"] = {"commandsRun": {"command": "string", "result": "string"}}
    assert evaluate_response_contract(trajectory(response), contract)["verified_obligation_ids"] == ["obl-report"]


@pytest.mark.parametrize("item_fields", [
    {"commandsRun": {"summary": "unsupported"}},
    {"commandsRun": ["summary"]},
    {"commandsRun": {"summary": "object"}},
    {"unknown": {"summary": "string"}},
])
def test_invalid_or_unsupported_item_contract_cannot_clear_obligation(item_fields) -> None:
    contract = acceptance_contract()
    contract["checks"][0]["required_fields"]["commandsRun"] = "array"
    contract["checks"][0]["required_item_fields"] = item_fields
    assert evaluate_response_contract(trajectory(), contract)["verified_obligation_ids"] == []
