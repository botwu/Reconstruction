"""调用方已知的领域必须到达任务提示与绑定校验。"""

from types import SimpleNamespace

import pytest

from traceforge.reconstruction.intent_recovery import run_intent_recovery


@pytest.mark.parametrize("repair", [True, False])
def test_terminal_domain_is_inherited_without_reclassifying(tmp_path, repair):
    source = {
        "entry_mode": "RAW_SESSION", "domain_route": "terminal",
        "raw_session": {"messages": [{"role": "user", "content": "增加智能识别表头功能"}]},
        "tasks": [{"task_id": "t", "intake_selected": True, "message_indices": [0]}],
        "tool_timeline": [],
    }
    calls = []

    class Agent:
        def run(self, *, role, instruction, session, output_root):
            calls.append(instruction)
            file_bound = repair and len(calls) == 2
            payload = {
                "task_id": "t", "task_instruction": "增加智能识别表头功能",
                "core_objective": "增加智能识别表头功能",
                "acceptance_obligations": [{"id": "o", "text": "支持智能识别表头",
                                             "evidence_ref_ids": ["user:0"]}],
                "environment_bindings": [{"obligation_id": "o",
                    "verifier_kind": "FILE" if file_bound else "NON_FILE",
                    "required_paths": ["app.py"] if file_bound else [],
                    "initial_required_paths": ["app.py"] if file_bound else [],
                    "output_paths": [], "observable": "包含关键字的表头可正确识别"}],
            }
            return SimpleNamespace(payload=payload, completed=True, errors=[], turns=[],
                                   final_text="{}", backend="fixture")

    result = run_intent_recovery(source=source, agent=Agent(), output_root=tmp_path,
                                replay_files_by_task={"t": ["app.py"]})
    assert len(calls) == 2
    assert '"domain_route": "terminal"' in calls[0]
    assert result["status"] == ("READY" if repair else "REVIEW")
    assert "domain_route" not in source["tasks"][0]
