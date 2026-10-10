"""环境 Agent 不把验收后端的 FILE 标签当成只读任务。"""

import copy

import pytest

from traceforge.reconstruction.environment_bindings import workspace_task_context


@pytest.mark.parametrize("kind", ["FILE", "NON_FILE"])
def test_workspace_context_preserves_user_goal_and_path_lifecycle(kind):
    task = {
        "task_id": "task-1", "task_instruction": "新增关键词表头识别并输出说明。",
        "source_task": {"user_texts": ["有店铺即可，其他表头也是"]},
        "mandatory_constraints": ["保留现有行为"],
        "acceptance_obligations": [{
            "id": "obl-1", "text": "识别带日期的表头", "evidence_ref_ids": ["user:25"],
            "verifier_kind": kind, "observable": "由后续验收执行行为检查",
        }],
        "environment_bindings": [{
            "obligation_id": "obl-1", "verifier_kind": kind,
            "initial_required_paths": ["excel.py"], "output_paths": ["report.md"],
            "required_paths": ["excel.py", "report.md"],
        }],
    }
    before = copy.deepcopy(task)
    context = workspace_task_context(task)
    assert context["task_instruction"] == task["task_instruction"]
    assert context["source_task"] == task["source_task"]
    assert context["mandatory_constraints"] == ["保留现有行为"]
    assert context["acceptance_obligations"] == [{
        "id": "obl-1", "text": "识别带日期的表头", "evidence_ref_ids": ["user:25"],
    }]
    assert context["initial_required_paths"] == ["excel.py"]
    assert context["output_paths"] == ["report.md"]
    assert "environment_bindings" not in context
    assert task == before
