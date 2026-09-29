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


def test_derived_observation_does_not_become_a_new_user_requirement():
    task = {
        "task_instruction": "表头包含店铺关键字即可识别。",
        "acceptance_obligations": [{
            "text": "其他表头同样适用。",
            "observable": "修改 tests/test_excel.py 并运行 pytest。",
        }],
    }
    assert render_task_instruction(task) == "表头包含店铺关键字即可识别。\n\n其他表头同样适用。"
    task["acceptance_obligations"][0]["text"] = "修改 tests/test_excel.py 并运行 pytest。"
    assert "修改 tests/test_excel.py 并运行 pytest。" in render_task_instruction(task)


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


def test_public_instruction_and_response_check_share_workspace_coordinates():
    task = _task()
    task["environment_bindings"][0]["output_paths"] = ["project/review.md"]
    task["environment_path_aliases"] = {"review.md": "project/review.md", "unused.py": "other/unused.py"}
    rendered = render_task_instruction(task)
    assert "review.md → project/review.md" in rendered
    assert "unused.py" not in rendered
    assert render_task_instruction({**task, "task_instruction": rendered}) == rendered
    assert grounded_response_contract(task)["checks"][1]["report_path"] == "project/review.md"


def test_summary_levels_are_recovered_as_complete_source_group_not_model_subset():
    task = _task()
    task["response_contract"]["checks"][1]["finding_levels"] = ["Critical"]
    contract = grounded_response_contract(task)
    summary = next(check for check in contract["checks"] if check["kind"] == "basic_summary")
    assert summary["finding_levels"] == ["Critical", "Important", "Minor"]


def test_summary_levels_keep_complete_comma_delimited_source_group():
    task = _task()
    task["source_task"]["user_texts"][0] = task["source_task"]["user_texts"][0].replace(
        "Critical/Important/Minor", "Critical, Important, and Minor"
    )
    task["response_contract"]["checks"][1]["finding_levels"] = ["Important"]
    contract = grounded_response_contract(task)
    assert contract["checks"][1]["finding_levels"] == ["Critical", "Important", "Minor"]


def test_conflicting_source_level_groups_leave_summary_unverified():
    task = _task()
    task["source_task"]["user_texts"][0] += "\nAn alternative list is Critical/Minor."
    task["response_contract"]["checks"][1]["finding_levels"] = ["Critical"]
    contract = grounded_response_contract(task)
    assert [check["kind"] for check in contract["checks"]] == ["acceptance_report"]


def _extend_source_example(task, extra):
    source = task["source_task"]["user_texts"][0]
    start, end = source.index("{"), source.rindex("}") + 1
    example = json.loads(source[start:end])
    example.update(extra)
    task["source_task"]["user_texts"][0] = source[:start] + json.dumps(example) + source[end:]


def test_unknown_nested_object_is_not_flattened_into_certified_top_level_type():
    task = _task()
    _extend_source_example(task, {"proof": {"file": "source.py", "line": 12}})
    contract = grounded_response_contract(task)
    assert [check["kind"] for check in contract["checks"]] == ["basic_summary"]
    assert "proof" in render_task_instruction(task)
    assert task["environment_bindings"][1]["obligation_id"] == "format"


def test_unknown_nested_array_shape_stays_unverified():
    task = _task()
    _extend_source_example(task, {"attachments": [{"file": "source.py"}]})
    contract = grounded_response_contract(task)
    assert [check["kind"] for check in contract["checks"]] == ["basic_summary"]


def test_unchecked_extra_criterion_field_stays_unverified():
    task = _task()
    _extend_source_example(task, {"criteriaSatisfied": [{
        "id": "criterion-1", "status": "satisfied", "evidence": "source.py:12",
        "ticket": "required-issue-id",
    }]})
    contract = grounded_response_contract(task)
    assert [check["kind"] for check in contract["checks"]] == ["basic_summary"]


def test_supported_criterion_and_command_entry_shapes_remain_available():
    task = _task()
    _extend_source_example(task, {
        "criteriaSatisfied": [{"id": "criterion-1", "status": "satisfied", "evidence": "source.py:12"}],
        "commandsRun": [{"command": "pytest", "result": "passed", "summary": "2 passed"}],
    })
    contract = grounded_response_contract(task)
    assert contract["checks"][0]["kind"] == "acceptance_report"
    assert contract["checks"][0]["required_fields"]["commandsRun"] == "array"


def test_nested_contract_fields_follow_only_source_example_keys():
    task = _task()
    _extend_source_example(task, {
        "criteriaSatisfied": [{"id": "criterion-1", "status": "satisfied", "evidence": "source.py:12"}],
        "commandsRun": [{"command": "pytest", "result": "passed", "summary": "2 passed"}],
    })
    check = grounded_response_contract(task)["checks"][0]
    assert check["required_item_fields"] == {
        "criteriaSatisfied": {"id": "string", "status": "string", "evidence": "string"},
        "commandsRun": {"command": "string", "result": "string", "summary": "string"},
    }

    _extend_source_example(task, {"commandsRun": [{"command": "pytest", "result": "passed"}]})
    check = grounded_response_contract(task)["checks"][0]
    assert check["required_item_fields"]["commandsRun"] == {"command": "string", "result": "string"}


def test_grounded_contract_preserves_both_checks_for_merged_response_obligation():
    task = _task()
    task["response_contract"]["checks"][1]["obligation_id"] = "format"
    contract = grounded_response_contract(task)
    assert [(check["obligation_id"], check["kind"]) for check in contract["checks"]] == [
        ("format", "acceptance_report"), ("format", "basic_summary"),
    ]


def test_ungrounded_half_cannot_leave_merged_response_obligation_covered():
    task = _task()
    task["response_contract"]["checks"][1]["obligation_id"] = "format"
    task["response_contract"]["checks"][1]["report_path"] = "unobserved.md"
    assert grounded_response_contract(task) is None


def test_duplicate_same_kind_cannot_silently_narrow_response_obligation():
    task = _task()
    task["response_contract"]["checks"] = [
        task["response_contract"]["checks"][0], task["response_contract"]["checks"][0],
    ]
    assert grounded_response_contract(task) is None
