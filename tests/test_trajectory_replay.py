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
    assert capture["files"] == [{
        "path": "a.txt",
        "completeness": "PARTIAL",
        "source_event_id": "e-1",
        "content_sha256": __import__("hashlib").sha256(b"before\n").hexdigest(),
    }]
    assert capture["status"] == "PARTIAL"
    assert {item["classification"] for item in capture["withheld_changes"]} == {
        "withheld_change",
        "agent_created_file",
    }


def test_replay_marks_observed_file_partial_after_mutation(tmp_path):
    source = tmp_path / "source"
    (source / "private").mkdir(parents=True)
    rows = [
        _event(1, "TOOL_CALL", "read", {"path": "a.txt"}),
        _event(2, "TOOL_RESULT", "read", "before\n"),
        _event(3, "TOOL_CALL", "edit", {"path": "a.txt", "new_string": "after"}),
    ]
    (source / "private/event_occurrences.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n"
    )
    output = build_trajectory_replay(normalized_run_dir=source, output_root=tmp_path / "out")
    capture = json.loads((output / "replay_manifest.json").read_text())["captures"][0]
    assert capture["files"][0]["completeness"] == "PARTIAL"


def _write_events(source, rows):
    (source / "private").mkdir(parents=True)
    (source / "private/event_occurrences.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n"
    )


def test_replay_excludes_reads_after_write_and_records_partial_evidence(tmp_path):
    source = tmp_path / "source"
    _write_events(
        source,
        [
            _event(1, "TOOL_CALL", "write", {"path": "new.txt", "content": "new"}),
            _event(2, "TOOL_RESULT", "write", "ok"),
            _event(3, "TOOL_CALL", "read", {"path": "new.txt"}),
            _event(4, "TOOL_RESULT", "read", "new"),
        ],
    )
    output = build_trajectory_replay(normalized_run_dir=source, output_root=tmp_path / "out")
    capture = json.loads((output / "replay_manifest.json").read_text())["captures"][0]
    assert capture["files"] == []
    assert capture["partial_evidence"] == [
        {"path": "new.txt", "reason": "read_after_first_mutation", "source_event_id": "e-3"}
    ]


def test_replay_excludes_reads_after_shell_and_keeps_barrier(tmp_path):
    source = tmp_path / "source"
    _write_events(
        source,
        [
            _event(1, "TOOL_CALL", "shell", {"command": "touch generated.txt"}),
            _event(2, "TOOL_RESULT", "shell", ""),
            _event(3, "TOOL_CALL", "read", {"path": "generated.txt"}),
            _event(4, "TOOL_RESULT", "read", "generated"),
        ],
    )
    output = build_trajectory_replay(normalized_run_dir=source, output_root=tmp_path / "out")
    capture = json.loads((output / "replay_manifest.json").read_text())["captures"][0]
    assert capture["files"] == []
    assert capture["unknown_mutation_barriers"] == ["e-1"]
    assert capture["partial_evidence"][0]["reason"] == "read_after_first_mutation"


def test_replay_records_complete_observation_source(tmp_path):
    source = tmp_path / "source"
    _write_events(
        source,
        [
            _event(1, "TOOL_CALL", "read", {"path": "existing.txt"}),
            _event(2, "TOOL_RESULT", "read", "before\n"),
        ],
    )
    output = build_trajectory_replay(normalized_run_dir=source, output_root=tmp_path / "out")
    capture = json.loads((output / "replay_manifest.json").read_text())["captures"][0]
    assert capture["files"][0]["completeness"] == "COMPLETE"
    assert capture["files"][0]["source_event_id"] == "e-1"
