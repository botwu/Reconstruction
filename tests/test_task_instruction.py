"""公开指令不能丢失验收格式，也不能把缺少示例变成上游硬门禁。"""

import json

from traceforge.task_instruction import grounded_response_contract, render_task_instruction


def _task():
    example = {"criteriaSatisfied": [{"id": "criterion-1", "status": "satisfied"}],
               "changedFiles": ["src/example.py"], "noStagedFiles": True,
               "manualNotes": "optional sample"}
    source = ("Write review.md, then return APPROVED or CHANGES_REQUIRED and Critical/Important/Minor counts.\n"
              "## Acceptance Contract\n`manualNotes` is optional.\n```acceptance-report\n"
              + json.dumps(example) + "\n```")
    return {"task_instruction": "Review input.py and write review.md. Follow the response schema.",
            "mandatory_constraints": ["Do not modify input.py."],
            "source_task": {"user_texts": [source]},
            "environment_bindings": [
                {"obligation_id": "file", "verifier_kind": "FILE", "output_paths": ["review.md"]},
                {"obligation_id": "format", "verifier_kind": "NON_FILE"},
                {"obligation_id": "summary", "verifier_kind": "NON_FILE"}],
            "response_contract": {"schema_version": "traceforge.response-contract.v1", "checks": [
                {"kind": "acceptance_report", "obligation_id": "format", "criterion_ids": ["invented"]},
                {"kind": "basic_summary", "obligation_id": "summary", "verdicts": ["APPROVED", "CHANGES_REQUIRED"],
                 "finding_levels": ["Critical", "Important", "Minor"], "report_path": "review.md", "match_report": True},
            ]}}


def test_instruction_retains_original_schema_and_constraints_without_duplication():
    task = _task()
    rendered = render_task_instruction(task)
    assert "src/example.py" in rendered
    assert "Do not modify input.py." in rendered
    assert rendered.count("```acceptance-report") == 1
    assert render_task_instruction({**task, "task_instruction": rendered}) == rendered


def test_missing_or_invalid_schema_does_not_block_instruction_delivery():
    assert render_task_instruction({"task_instruction": "Return acceptance-report."}) == "Return acceptance-report."
    task = _task()
    task["source_task"]["user_texts"] = ["```acceptance-report\n{invalid\n```"]
    assert "{invalid" in render_task_instruction(task)
    assert grounded_response_contract(task) is None


def test_response_contract_uses_source_fields_and_ids_not_model_invention():
    contract = grounded_response_contract(_task())
    report, summary = contract["checks"]
    assert report["criterion_ids"] == ["criterion-1"]
    assert report["required_fields"] == {"criteriaSatisfied": "array", "changedFiles": "array", "noStagedFiles": "boolean"}
    assert summary["report_path"] == "review.md"
    assert contract["source_sha256"]


def test_response_contract_cannot_claim_file_semantics_or_unknown_report():
    task = _task()
    task["response_contract"]["checks"][0]["obligation_id"] = "file"
    task["response_contract"]["checks"][1]["report_path"] = "private-answer.md"
    assert grounded_response_contract(task) is None
