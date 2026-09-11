import json

from traceforge.trajectory_replay.pipeline import build_trajectory_replay


def _event(seq, kind, name, args, call_id="c"):
    if kind == "TOOL_RESULT":
        payload = {"tool_call_id": call_id, "content": {"value": args}}
    else:
        payload = {
            "tool_call_id": call_id,
            "function": {"name": name, "arguments": {"value": args}},
        }
    return {
        "capture_occurrence_id": "cap-1",
        "sequence_number": seq,
        "event_kind": kind,
        "event_occurrence_id": f"e-{seq}",
        "payload": payload,
    }


def test_replay_restores_read_file_and_withholds_mutation(tmp_path):
    source = tmp_path / "source"
    (source / "private").mkdir(parents=True)
    rows = [
        _event(1, "TOOL_CALL", "read", {"path": "a.txt"}),
        _event(2, "TOOL_RESULT", "read", "before\n"),
        _event(3, "TOOL_CALL", "edit", {"path": "a.txt", "new_string": "after"}),
        _event(4, "TOOL_CALL", "write", {"path": "new.txt", "content": "new"}),
    ]
    (source / "private/event_occurrences.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n"
    )
    output = build_trajectory_replay(normalized_run_dir=source, output_root=tmp_path / "out")
    manifest = json.loads((output / "replay_manifest.json").read_text())
    capture = manifest["captures"][0]
    assert (output / "workspaces/cap-1/a.txt").read_text() == "before\n"
    assert not (output / "workspaces/cap-1/new.txt").exists()
    assert capture["status"] == "PARTIAL"
    assert {item["classification"] for item in capture["withheld_changes"]} == {
        "withheld_change",
        "agent_created_file",
    }
