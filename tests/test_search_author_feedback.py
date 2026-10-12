"""作者只接收环境恢复动作，完整后审记录仍独立保存。"""

import copy
import json
from types import SimpleNamespace

import pytest

from traceforge.reconstruction import search_environment as module
from traceforge.reconstruction.agents.session import AgentConversation


def review():
    return {"decision": "REPAIR", "task_contract_issue": None, "requirements": [
        {"obligation_id": "a", "status": "SOLVER_ERROR",
         "reason": "待求结论：平台不能导出目标格式", "repair": "不得移交的答案改写"},
        {"obligation_id": "b", "status": "ENVIRONMENT_GAP",
         "reason": "输入范围写错，同时混有待求格式结论",
         "repair": "核对 sources/guide.txt 已交付全文，修正只读部分窗口的说明"},
        {"obligation_id": "c", "status": "SUPPORTED",
         "reason": "含答案的复核解释", "repair": "据已交付原文重新判断必须联网的停止依据"},
    ]}


@pytest.mark.parametrize("checkpoint", [False, True])
def test_initial_author_instruction_contains_only_recovery_actions(
    tmp_path, monkeypatch, checkpoint,
):
    feedback = review()
    original = copy.deepcopy(feedback)
    captured = {}
    class Captured(Exception):
        pass

    def complete(**kwargs):
        captured.update(json.loads(kwargs["instruction"]))
        raise Captured()

    monkeypatch.setattr(module, "_complete_search_environment", complete)
    monkeypatch.setattr(module, "_restore_search_checkpoint",
                        lambda *args, **kwargs: AgentConversation())
    source = {"raw_session": {"messages": [{"role": "user", "content": "比较资料"}]}}
    with pytest.raises(Captured):
        module.run_search_task(
            source=source, task={"task_id": "task", "task_instruction": "比较资料"},
            agent=SimpleNamespace(), output_root=tmp_path, initial_feedback=feedback,
            checkpoint_path=tmp_path / "checkpoint.json" if checkpoint else None,
        )
    expected = {"decision": "REPAIR", "requirements": [
        {key: item[key] for key in ("obligation_id", "status", "repair")}
        for item in feedback["requirements"][1:]
    ]}
    assert captured["reconstruction_feedback"] == expected
    assert feedback == original
    if not checkpoint:
        assert (captured["SOURCE_SESSION"]["messages"][0]["message"]
                == source["raw_session"]["messages"][0])


@pytest.mark.parametrize("outcome", ["recover", "reassess"])
def test_completion_loop_only_forwards_repair_action(tmp_path, monkeypatch, outcome):
    from test_search_blocked_review import scenario

    result, _, _, sessions = scenario(tmp_path, monkeypatch, outcome=outcome)
    assert result["status"] == "ENVIRONMENT_READY"
    request = next(json.loads(message["content"]) for message in sessions[-1].conversation.messages
                   if message["role"] == "user" and "completion_feedback" in message["content"])
    feedback = request["completion_feedback"]
    assert all(set(row) == {"obligation_id", "status", "repair"}
               for row in feedback["requirements"])
    assert feedback["requirements"][0]["status"] == (
        "SUPPORTED" if outcome == "reassess" else "ENVIRONMENT_GAP"
    )
    persisted = json.loads((tmp_path / "completion-review.json").read_text())
    assert persisted["requirements"][0]["reason"]


def test_postreview_requires_standalone_material_repair_before_author(tmp_path):
    feedback = review()
    incomplete = copy.deepcopy(feedback)
    incomplete["requirements"][1]["repair"] = ""
    requests = []
    payloads = iter([incomplete, feedback])

    def run(**kwargs):
        requests.append(json.loads(kwargs["instruction"]))
        return SimpleNamespace(completed=True, errors=[], payload=next(payloads))

    result = module._run_search_review(
        source={"raw_session": {"messages": []}}, request={"original": "原材料"}, task={
            "acceptance_obligations": [{"id": name} for name in ("a", "b", "c")],
        }, agent=SimpleNamespace(run=run), session=SimpleNamespace(),
        role=module.SEARCH_REVIEW_ROLE, output_root=tmp_path,
    )
    assert len(requests) == 2
    assert result == feedback
