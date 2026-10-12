"""调用方已知的领域必须到达任务提示与绑定校验。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from traceforge.reconstruction.intent_recovery import run_intent_recovery


@pytest.mark.parametrize("verifier_kind", ["FILE", "NON_FILE"])
def test_terminal_domain_is_inherited_without_reclassifying(tmp_path, verifier_kind):
    source = {
        "entry_mode": "RAW_SESSION", "domain_route": "terminal",
        "raw_session": {"messages": [{"role": "user", "content": "增加智能识别表头功能"}]},
        "tasks": [{"task_id": "t", "intake_selected": True, "message_indices": [0]}],
        "tool_timeline": [],
    }
    original_source = deepcopy(source)
    bindings = [{
        "obligation_id": "o", "verifier_kind": verifier_kind,
        "required_paths": ["app.py"], "initial_required_paths": ["app.py"],
        "output_paths": [], "observable": "包含关键字的表头可正确识别",
    }]
    original_bindings = deepcopy(bindings)
    calls = []

    class Agent:
        def run(self, *, role, instruction, session, output_root):
            calls.append(instruction)
            payload = {
                "task_id": "t", "task_instruction": "增加智能识别表头功能",
                "core_objective": "增加智能识别表头功能",
                "acceptance_obligations": [{"id": "o", "text": "支持智能识别表头",
                                             "evidence_ref_ids": ["user:0"]}],
                "environment_bindings": bindings,
            }
            return SimpleNamespace(payload=payload, completed=True, errors=[], turns=[],
                                   final_text="{}", backend="fixture")

    result = run_intent_recovery(source=source, agent=Agent(), output_root=tmp_path,
                                replay_files_by_task={"t": ["app.py"]})
    assert len(calls) == 1
    assert '"domain_route": "terminal"' in calls[0]
    # 这里只验证领域传递不覆盖模型合同，不将结构通过当作语义分类正确。
    assert result["status"] == "READY"
    assert result["task"]["environment_bindings"] == original_bindings
    assert bindings == original_bindings
    assert source == original_source


@pytest.mark.parametrize(
    ("scope", "expected", "error"),
    [
        (
            {
                "status": "IN_SCOPE",
                "reason": "检索代码解释调用关系",
                "evidence_ref_ids": ["user:0"],
            },
            "READY",
            None,
        ),
        (
            {"status": "OUT_OF_SCOPE", "reason": "只续写已有小说", "evidence_ref_ids": ["user:0"]},
            "REVIEW",
            "DELIVERY_SCOPE_OUT_OF_SCOPE",
        ),
        (
            {
                "status": "NEEDS_REVIEW",
                "reason": "指代不足，目标待核",
                "evidence_ref_ids": ["user:0"],
            },
            "REVIEW",
            "DELIVERY_SCOPE_NEEDS_REVIEW",
        ),
        (None, "REVIEW", "DELIVERY_SCOPE_REQUIRED"),
        (
            {"status": "READY", "reason": "对象明确", "evidence_ref_ids": ["user:0"]},
            "REVIEW",
            "DELIVERY_SCOPE_INVALID",
        ),
        (
            {"status": "IN_SCOPE", "reason": "", "evidence_ref_ids": ["user:0"]},
            "REVIEW",
            "DELIVERY_SCOPE_INVALID",
        ),
        (
            {"status": "IN_SCOPE", "reason": "查资料", "evidence_ref_ids": ["user:99"]},
            "REVIEW",
            "DELIVERY_SCOPE_INVALID",
        ),
        (
            {"status": "IN_SCOPE", "reason": "查资料", "evidence_ref_ids": []},
            "REVIEW",
            "DELIVERY_SCOPE_INVALID",
        ),
    ],
)
def test_search_delivery_scope_is_grounded_and_never_inferred(tmp_path, scope, expected, error):
    source = {
        "domain_route": "retrieval",
        "raw_session": {"messages": [{"role": "user", "content": "按当前目标继续"}]},
        "tasks": [{"task_id": "t", "message_indices": [0]}],
        "tool_timeline": [],
    }
    original = deepcopy(source)
    calls = []

    def run(**kwargs):
        calls.append(kwargs["instruction"])
        payload = _scope_intent_payload("t", 0, scope)
        return SimpleNamespace(
            payload=payload, completed=True, errors=[], turns=[], final_text="{}", backend="fixture"
        )

    result = run_intent_recovery(
        source=source, agent=SimpleNamespace(run=run), output_root=tmp_path
    )
    assert result["status"] == expected
    assert len(calls) == 1
    assert source == original
    assert "SOURCE_SESSION=" in calls[0]
    assert "delivery_scope" in calls[0]
    if scope is not None:
        assert result["task"]["delivery_scope"] == scope
    if error:
        assert error in result["tasks"][0]["errors"]


def _scope_intent_payload(task_id, index, scope):
    payload = {
        "task_id": task_id,
        "task_instruction": "保持原用户目标",
        "core_objective": "保持原用户目标",
        "acceptance_obligations": [
            {"id": "o", "text": "完成原请求", "evidence_ref_ids": [f"user:{index}"]}
        ],
        "environment_bindings": [
            {
                "obligation_id": "o",
                "verifier_kind": "NON_FILE",
                "required_paths": [],
                "initial_required_paths": [],
                "output_paths": [],
                "observable": "回答原用户要求",
            }
        ],
    }
    if scope is not None:
        payload["delivery_scope"] = deepcopy(scope)
    return payload


def test_search_scope_blocks_author_but_continues_other_tasks(tmp_path, monkeypatch):
    import json

    from traceforge.reconstruction import search_environment

    # 真实案例的语义同类替身；原始审计案例只存私有产物，不将会话原文写入测试仓库。
    cases = [
        ("translate", "把给定商品资料译成中文", "OUT_OF_SCOPE"),
        ("plan", "依据进度摘要安排后续开发任务", "OUT_OF_SCOPE"),
        ("idea", "给出荒诞喜剧故事构想", "OUT_OF_SCOPE"),
        ("novel", "承接已有故事继续创作", "OUT_OF_SCOPE"),
        ("unclear", "按之前的继续", "NEEDS_REVIEW"),
        ("missing", "查阅指定文档", None),
        ("docs", "查阅项目文档比较三种能力", "IN_SCOPE"),
        ("code", "检索源码定位积分计算和调用关系", "IN_SCOPE"),
    ]
    messages = [{"role": "user", "content": text} for _, text, _ in cases]
    source = {
        "domain_route": "retrieval",
        "raw_session": {"messages": messages},
        "tasks": [
            {"task_id": task_id, "message_indices": [i]} for i, (task_id, _, _) in enumerate(cases)
        ],
        "tool_timeline": [],
    }
    original = deepcopy(source)
    intent_calls, authors = [], []

    def run(**kwargs):
        i = len(intent_calls)
        intent_calls.append(kwargs["instruction"])
        task_id, text, status = cases[i]
        scope = (
            {"status": status, "reason": text, "evidence_ref_ids": [f"user:{i}"]}
            if status
            else None
        )
        return SimpleNamespace(
            payload=_scope_intent_payload(task_id, i, scope),
            completed=True,
            errors=[],
            turns=[],
            final_text="{}",
            backend="fixture",
        )

    def author(**kwargs):
        authors.append(kwargs["task"]["task_id"])
        return {"task_id": kwargs["task"]["task_id"], "status": "ENVIRONMENT_READY", "errors": []}

    monkeypatch.setattr(search_environment, "run_search_task", author)
    manifest = search_environment.run_search_reconstruction(
        source=source,
        agent=SimpleNamespace(run=run),
        output_root=tmp_path,
    )
    result = json.loads(manifest.read_text())
    assert authors == ["docs", "code"]
    assert len(intent_calls) == len(cases)
    assert source == original
    assert [task["task_id"] for task in result["tasks"]] == [row[0] for row in cases]
    assert result["acceptance"] == "NOT_ASSESSED"
    for task, (_, _, expected_scope) in zip(result["tasks"], cases, strict=True):
        if expected_scope:
            assert task["delivery_scope"]["status"] == expected_scope
        if expected_scope != "IN_SCOPE":
            assert task["status"] == "BLOCKED"
            assert task["stopped_at"] == "intent"
            assert task["errors"]
