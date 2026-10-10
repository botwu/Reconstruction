"""合同错误须回到原用户意图，不能在错误合同下继续补全或求解。"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceforge.reconstruction import search_environment as module
from traceforge.reconstruction.agents import INTENT_ROLE, AgentSession
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.intent_recovery import IntentRecoveryError, run_intent_recovery
from traceforge.reconstruction.session_source import indexed_session


def source():
    return {
        "line_sha256": "original-row",
        "domain_route": "retrieval",
        "input_domain": "search",
        "tasks": [
            {
                "task_id": "plan",
                "span_ids": ["span-one"],
                "message_indices": [1],
                "user_texts": ["一次性列出已确定的全部后续任务"],
            }
        ],
        "selected_task_ids": ["plan"],
        "tool_timeline": [],
        "raw_session": {
            "tools": [{"name": "lookup", "schema": {"all": "fields"}}],
            "messages": [
                {"role": "system", "content": "之前一阶段已完成，二阶段还有两项。"},
                {"role": "user", "content": "一次性列出已确定的全部后续任务"},
                {"role": "assistant", "content": "原轨迹的回答，仅作历史材料"},
                {"role": "user", "content": "另一个任务，不能作为当前义务锚点"},
            ],
        },
    }


def payload(*, corrected=False, renumber=False):
    texts = (
        ["列出全部既定后续任务", "保留已完成和未完成的时序依据"]
        if corrected
        else ["给条件性任务模板，完整范围留给执行者以后核对"]
    )
    ids = [f"new-{i}" if corrected or renumber else f"old-{i}" for i in range(len(texts))]
    return {
        "task_id": "plan",
        "task_instruction": texts[0],
        "core_objective": texts[0],
        "acceptance_obligations": [
            {"id": oid, "text": text, "evidence_ref_ids": ["user:1"]}
            for oid, text in zip(ids, texts, strict=True)
        ],
        "environment_bindings": [
            {
                "obligation_id": oid,
                "verifier_kind": "NON_FILE",
                "required_paths": [],
                "initial_required_paths": [],
                "output_paths": [],
                "observable": text,
            }
            for oid, text in zip(ids, texts, strict=True)
        ],
    }


def issue():
    return {
        "source_message_indices": [1],
        "reason": "用户要求当次给出完整范围，合同却推迟给后续执行者核对",
        "repair": "按原用户请求恢复完整范围义务，材料不足交给补全判断",
    }


def task():
    raw = source()
    return {**payload(), "source_task": {**raw["tasks"][0], "domain_route": "retrieval"}}


def review(contract_issue=None, decision="REPAIR"):
    return {
        "decision": decision,
        "task_contract_issue": contract_issue,
        "requirements": [
            {
                "obligation_id": "old-0",
                "status": "SUPPORTED",
                "reason": "目前交付只支持旧合同",
                "repair": "",
            }
        ],
    }


def result(role, value):
    return AgentResult(
        role=role.name,
        backend="fixture",
        completed=True,
        errors=[],
        payload=value,
        final_text=json.dumps(value, ensure_ascii=False),
        turns=[],
    )


@pytest.mark.parametrize("completion", [False, True])
def test_contract_issue_does_not_require_an_old_obligation_gap(tmp_path, completion):
    value = review(issue())
    outcome = module._run_search_review(
        source=source(),
        request={"SOURCE_SESSION": indexed_session(source()["raw_session"])},
        task=task(),
        agent=SimpleNamespace(run=lambda **kw: result(kw["role"], value)),
        session=AgentSession(),
        role=module.SEARCH_REVIEW_ROLE,
        output_root=tmp_path,
        completion=completion,
    )
    assert outcome == value


@pytest.mark.parametrize("indices", [[0], [2], [3], [99], [-1], [True], [], [1, 1]])
def test_contract_feedback_cannot_cite_nonuser_or_other_task(tmp_path, indices):
    bad = {**issue(), "source_message_indices": indices}
    outcome = module._run_search_review(
        source=source(),
        request={},
        task=task(),
        agent=SimpleNamespace(run=lambda **kw: result(kw["role"], review(bad))),
        session=AgentSession(),
        role=module.SEARCH_REVIEW_ROLE,
        output_root=tmp_path,
    )
    assert outcome["decision"] == "BLOCKED"
    assert any("TASK_CONTRACT" in error for error in outcome["errors"])


@pytest.mark.parametrize("decision", ["COMPLETE", "BLOCKED"])
def test_contract_issue_must_return_to_intent(tmp_path, decision):
    value = review(issue(), decision)
    outcome = module._run_search_review(
        source=source(),
        request={},
        task=task(),
        agent=SimpleNamespace(run=lambda **kw: result(kw["role"], value)),
        session=AgentSession(),
        role=module.SEARCH_REVIEW_ROLE,
        output_root=tmp_path,
    )
    assert outcome["decision"] == "BLOCKED"
    assert outcome["errors"]


@pytest.mark.parametrize("mode", ["fix", "renumber", "completion"])
def test_search_contract_repair_restarts_intent_and_author_only_when_goal_changes(
    tmp_path,
    monkeypatch,
    mode,
):
    raw_source = source()
    untouched = copy.deepcopy(raw_source)
    intents, authors, native, post = [], [], [], []
    monkeypatch.setattr(module, "export_search_task", lambda environment, root, **kw: root)

    def rollout(**kwargs):
        native.append(kwargs)
        return (
            {"status": "ROLLOUT_COMPLETED"},
            [
                {"trial": str(i), "tool_events": [], "answer": "SOLVER_ANSWER_NOT_INTENT_FEEDBACK"}
                for i in range(2)
            ],
        )

    monkeypatch.setattr(module, "run_native_unassessed_rollouts", rollout)

    def run(*, role, instruction, session, output_root):
        if role.name == INTENT_ROLE.name:
            intents.append((instruction, session, output_root))
            value = payload(
                corrected=len(intents) > 1 and mode != "renumber",
                renumber=len(intents) > 1 and mode == "renumber",
            )
        else:
            request = json.loads(instruction)
            if role.name == "search_completion":
                authors.append((request, session, output_root))
                value = {
                    "status": "READY",
                    "requires_live_web": False,
                    "missing_inputs": [],
                    "retrieval_reason": "原输入足够",
                    "excluded_events": [],
                    "context_references": [],
                    "limitations": [],
                    "requirement_coverage": [
                        {
                            "obligation_id": row["id"],
                            "evidence_ref_ids": ["message:1"],
                            "reason": "原用户请求",
                        }
                        for row in request["task"]["acceptance_obligations"]
                    ],
                }
                if mode == "completion" and len(intents) == 1:
                    value.update(
                        status="BLOCKED", missing_inputs=["旧合同把完整范围推迟给未来核对"]
                    )
            else:
                assert role.name == "search_review"
                post.append((request, session))
                value = {
                    "decision": "REPAIR" if len(intents) == 1 else "COMPLETE",
                    "task_contract_issue": issue() if len(intents) == 1 else None,
                    "requirements": [
                        {
                            "obligation_id": row["id"],
                            "status": "SUPPORTED",
                            "reason": "按原用户请求核对",
                            "repair": "",
                        }
                        for row in request.get("environment", {"task": task()})["task"][
                            "acceptance_obligations"
                        ]
                    ],
                }
        return result(role, value)

    manifest_path = module.run_search_reconstruction(
        source=raw_source,
        agent=SimpleNamespace(run=run),
        output_root=tmp_path,
        verification_config=SimpleNamespace(execute_rollout=True, rollout_trials=2),
    )
    manifest = json.loads(manifest_path.read_text())
    final = manifest["tasks"][0]
    assert len(intents) == 2
    assert intents[0][1] is not intents[1][1]
    assert json.loads(intents[1][1].session_context) == raw_source["raw_session"]
    assert "TASK_CONTRACT_FEEDBACK=" in intents[1][0]
    assert "SOLVER_ANSWER_NOT_INTENT_FEEDBACK" not in intents[1][0]
    assert raw_source == untouched
    assert (tmp_path / "tasks/plan/result.json").is_file()
    old_result = json.loads((tmp_path / "tasks/plan/result.json").read_text())
    assert old_result["task_contract_issue"] == issue()
    if mode != "renumber":
        assert manifest["status"] == "COMPLETED"
        assert len(authors) == len(post) == 2
        assert len(native) == (1 if mode == "completion" else 2)
        assert authors[0][1] is not authors[1][1]
        assert authors[0][2] != authors[1][2]
        assert all(
            len(row["requirements"]) == count
            for row, count in zip(
                [
                    old_result["researcher_rounds"][0]["review"],
                    final["researcher_rounds"][0]["review"],
                ],
                [1, 2],
                strict=True,
            )
        )
        selected = json.loads(Path(final["intent_path"]).read_text())
        assert selected["task"]["source_task"] == task()["source_task"]
        assert len(selected["task"]["acceptance_obligations"]) == 2
        assert final["result_path"] != str(tmp_path / "tasks/plan/result.json")
        assert json.loads(Path(final["result_path"]).read_text())["status"] == "ROLLOUT_COMPLETED"
        assert len(final["task_contract_attempts"]) == 2
    else:
        assert len(authors) == len(native) == 1
        assert final["stopped_at"] == "intent"
        assert "TASK_CONTRACT_NO_PROGRESS" in final["errors"]


def test_fresh_intent_can_replace_bad_obligation_set_but_not_source_anchor(tmp_path):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return result(kwargs["role"], payload(corrected=True))

    old = task()
    feedback = {"previous_task": old, "task_contract_issue": issue()}
    output = run_intent_recovery(
        source=source(),
        agent=SimpleNamespace(run=run),
        output_root=tmp_path,
        correction_feedback=feedback,
    )
    assert output["status"] == "READY"
    assert len(calls) == 1
    assert len(output["task"]["acceptance_obligations"]) == 2
    assert output["task"]["source_task"] == old["source_task"]
    assert "TASK_CONTRACT_FEEDBACK=" in calls[0]["instruction"]


@pytest.mark.parametrize("change", ["other_anchor", "solver_field", "other_user"])
def test_intent_rejects_foreign_or_unbounded_correction_before_model(tmp_path, change):
    feedback = {"previous_task": task(), "task_contract_issue": issue()}
    if change == "other_anchor":
        feedback["previous_task"]["source_task"]["message_indices"] = [3]
    elif change == "solver_field":
        feedback["solver_answer"] = "不能夹带答卷"
    else:
        feedback["task_contract_issue"]["source_message_indices"] = [3]
    with pytest.raises(IntentRecoveryError, match=r"合同|锚点|TASK_CONTRACT"):
        run_intent_recovery(
            source=source(),
            agent=SimpleNamespace(run=lambda **_: pytest.fail("不能创建模型请求")),
            output_root=tmp_path,
            correction_feedback=feedback,
        )


def test_response_contract_renumbering_preserves_actual_obligation_mapping(tmp_path):
    raw = source()
    text = (
        "一次性列出已确定的全部后续任务。响应格式如下：\n"
        "\x60\x60\x60acceptance-report\n"
        '{"criteriaSatisfied":[{"id":"complete","status":"done","evidence":"说明"}]}'
        "\n\x60\x60\x60"
    )
    raw["raw_session"]["messages"][1]["content"] = text
    raw["tasks"][0]["user_texts"] = [text]
    normalized = []
    for index, suffix in enumerate(["old", "new", "different-binding"]):
        value = payload(corrected=True)
        ids = [f"{suffix}-{i}" for i in range(2)]
        for row, binding, oid in zip(
            value["acceptance_obligations"],
            value["environment_bindings"],
            ids,
            strict=True,
        ):
            row["id"] = binding["obligation_id"] = oid
        value["response_contract"] = {
            "schema_version": "traceforge.response-contract.v1",
            "checks": [{"kind": "acceptance_report", "obligation_id": ids[index == 2]}],
        }
        recovered = run_intent_recovery(
            source=raw,
            agent=SimpleNamespace(run=lambda value=value, **kw: result(kw["role"], value)),
            output_root=tmp_path / suffix,
        )
        assert recovered["status"] == "READY"
        current = recovered["task"]
        assert current["response_contract"]["checks"][0]["obligation_id"] == ids[index == 2]
        normalized.append(current)
    before = copy.deepcopy(normalized)
    assert module._task_contract_state(normalized[0]) == module._task_contract_state(normalized[1])
    assert module._task_contract_state(normalized[0]) != module._task_contract_state(normalized[2])
    assert normalized == before
