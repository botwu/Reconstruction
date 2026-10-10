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
