from traceforge.reconstruction.tool_process_sketch import build_tool_process_sketch


def test_sketch_extracts_paths_and_result_shape() -> None:
    sketch = build_tool_process_sketch(
        [
            {
                "call_id": "c1",
                "name": "write_file",
                "arguments": {"path": "app.py", "content": "print(1)"},
                "result_text": "",
            },
            {
                "call_id": "c2",
                "name": "exec",
                "arguments": {"command": "ls"},
                "result_text": '{"ok": true}',
            },
        ],
        task={"core_objective": "补齐 app.py"},
    )
    assert sketch["sufficient"] is True
    assert "app.py" in sketch["candidate_paths"]
    shapes = {item["evidence_ref_id"]: item["result_shape"] for item in sketch["events"]}
    assert shapes["c1"] == "wrote"
    assert shapes["c2"] == "json"


def test_sketch_without_files_or_q_is_insufficient() -> None:
    sketch = build_tool_process_sketch(
        [{"call_id": "c1", "name": "chat", "arguments": {}, "result_text": "ok"}],
        task={"core_objective": "解释一下这段日志为什么超时"},
    )
    assert sketch["sufficient"] is False
    assert "TOOL_PROCESS_INSUFFICIENT" in sketch["reason_codes"]


def test_file_binding_makes_empty_timeline_sufficient() -> None:
    sketch = build_tool_process_sketch(
        [],
        task={
            "core_objective": "读入口",
            "environment_bindings": [
                {
                    "obligation_id": "obl-001",
                    "verifier_kind": "FILE",
                    "required_paths": ["app.py"],
                }
            ],
        },
    )
    assert sketch["sufficient"] is True
    assert sketch["required_paths"] == ["app.py"]
