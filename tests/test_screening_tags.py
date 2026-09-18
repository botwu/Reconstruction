from traceforge.screening.rubric import admit_after_tasks
from traceforge.screening.task_labels import build_session_tags


def _task(task_id, outcome, actionable=True, need=True):
    return {
        "task_id": task_id,
        "span_ids": ["s-" + task_id],
        "is_actionable": actionable,
        "outcome": outcome,
        "needs_reconstruction": need,
        "domain_route": "other",
        "rubric": {"task_identifiability": 3, "failure_evidence": 3},
        "tags": [],
    }


def test_rubric_pass_adds_explicit_reconstruction_tags_and_session_tags():
    tasks = [_task("a", "INCOMPLETE"), _task("b", "SUCCESS", need=False)]
    result = admit_after_tasks(
        rule={"decision": "REVIEW", "blocking_reason_codes": []},
        tasks=tasks,
        relations=[
            {
                "from_task_id": "a",
                "to_task_id": "b",
                "type": "dependency",
            }
        ],
        parse_errors=(),
        label_status="COMPLETE",
    )
    assert result["decision"] == "ELIGIBLE"
    assert "selected_for_reconstruction" in tasks[0]["tags"]
    assert "rubric_pass" in tasks[0]["tags"]
    assert "rubric_reject" in tasks[1]["tags"]
    tags = build_session_tags(tasks=tasks, relations=[{"type": "dependency"}], decision=result["decision"])
    assert {"contains_reconstruction_candidate", "contains_unfinished_task", "has_dependency", "screening_eligible"} <= set(tags)


def test_parse_errors_still_tag_tasks_but_cannot_select():
    tasks = [_task("a", "INCOMPLETE"), _task("b", "SUCCESS", need=False)]
    result = admit_after_tasks(
        rule={"decision": "REVIEW", "blocking_reason_codes": []},
        tasks=tasks,
        relations=[],
        parse_errors=("UNCERTAIN_NEEDS_RECONSTRUCTION_MUST_BE_NULL:2",),
        label_status="COMPLETE",
    )
    assert result["decision"] == "REVIEW"
    assert result["route"] == "MODEL_TRIAGE_FAILED"
    assert tasks[0]["reconstruction_eligible"] is False
    assert "rubric_review" in tasks[0]["tags"]
    assert "selected_for_reconstruction" not in tasks[0]["tags"]
    assert "rubric_review" in tasks[1]["tags"] or "rubric_reject" in tasks[1]["tags"]
    session = build_session_tags(tasks=tasks, relations=[], decision=result["decision"])
    assert "screening_review" in session
    assert "contains_reconstruction_candidate" not in session


def test_non_actionable_is_tagged_and_cannot_pass_rubric():
    tasks = [_task("chat", "SUCCESS", actionable=False, need=False)]
    result = admit_after_tasks(
        rule={"decision": "REVIEW", "blocking_reason_codes": []},
        tasks=tasks,
        relations=[],
        parse_errors=(),
        label_status="COMPLETE",
    )
    assert result["decision"] == "REJECT"
    assert "rubric_reject" in tasks[0]["tags"]
    assert "non_actionable" in tasks[0]["tags"]
    assert "contains_reconstruction_candidate" not in build_session_tags(tasks=tasks, relations=[], decision=result["decision"])
