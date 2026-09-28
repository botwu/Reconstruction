"""限行读取的已知行段应到达补全上下文，不推断总长度或改写正文。"""

import json

import pytest

from test_parallel_exec_replay import UTF8, batch, call, result
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.terminal_universe_environment import materialize_environment
from traceforge.reconstruction.workspace_completion import completion_evidence_context


@pytest.mark.parametrize("command,start", [
    ("Get-Content app.py -TotalCount 2", 1),
    ("Get-Content app.py -Head 2", 1),
    ("Get-Content app.py -TotalCount '2'", 1),
    ("Get-Content app.py -Encoding UTF8 | Select-Object -First 2", 1),
    ("Get-Content app.py | Select-Object -Skip 160", 161),
    (UTF8 + "Get-Content app.py | Select-Object -SkipLast 3", 1),
])
def test_known_shell_ranges_keep_bytes_and_unknown_total(command, start):
    text = "one\r\n  two\r\n"
    replay = replay_from_timeline([batch([call(command)], [result(text)])])
    segment = next(row for row in replay.partial_evidence if row["reason"] == "read_segment")
    assert segment["line_numbers"] == [start, start + 1]
    assert segment["line_contents"] == ["one", "  two"]
    assert segment["total_lines"] is None
    assert segment["range_valid"] is True
    assert segment["result_block_index"] == 1
    assert replay.files[0].content == text
    assert replay.files[0].completeness == "PARTIAL"


def test_known_shell_ranges_reach_completion_and_public_excerpts(tmp_path):
    first = batch([call("Get-Content app.py -TotalCount 2")], [result("a\nb\n")])
    later = batch([call("Get-Content app.py | Select-Object -Skip 3")], [result("d\ne\n")])
    later["call_id"] = "later"
    replay = replay_from_timeline([first, later])
    context = completion_evidence_context(replay, {})
    card = context["replay_files"][0]
    assert card["replay_materialized_ranges"] == [[1, 2]]
    assert [row["ranges"] for row in card["observed_read_segments"]] == [[[1, 2]], [[4, 5]]]
    materialize_environment(replay, {"files": []}, tmp_path / "env",
                            evidence_refs={"batch", "later"})
    workspace = tmp_path / "env/workspace"
    index = json.loads((workspace / ".traceforge/source-excerpts.json").read_text())["files"][0]
    assert index["total_lines"] is None
    assert index["missing_line_ranges"] is None
    assert [(row["line_start"], row["line_end"]) for row in index["segments"]] == [(1, 2), (4, 5)]
    assert "4: d\n5: e" in (workspace / index["segments"][1]["excerpt_path"]).read_text()
    assert (workspace / "app.py").read_text() == "a\nb\n"


@pytest.mark.parametrize("command", [
    "Get-Content app.py -Tail 2",
    "Get-Content app.py | Select-Object -Last 2",
    "Get-Content app.py | Select-Object -First 2 -Skip 3",
])
def test_unknown_shell_range_does_not_invent_start_line(command):
    replay = replay_from_timeline([batch([call(command)], [result("observed\n")])])
    assert replay.files[0].completeness == "PARTIAL"
    assert not any(row["reason"] == "read_segment" for row in replay.partial_evidence)


def test_mixed_parallel_or_later_mutation_never_exports_shell_read_segment(tmp_path):
    first = batch([call("Get-Content app.py -TotalCount 1")], [result("first\n")])
    mixed = batch([call("Get-Content app.py | Select-Object -Skip 1"),
                   call('python -c "change_files()"')], [result("answer\n"), result("")])
    mixed["call_id"] = "mixed"
    later = batch([call("Get-Content app.py | Select-Object -Skip 1")], [result("answer\n")])
    later["call_id"] = "later"
    replay = replay_from_timeline([first, mixed, later])
    segments = [row for row in replay.partial_evidence if row["reason"] == "read_segment"]
    assert [row["source_event_id"] for row in segments] == ["batch"]
    materialize_environment(replay, {"files": []}, tmp_path / "env",
                            evidence_refs={"batch", "mixed", "later"})
    assert all("answer" not in path.read_text() for path in (tmp_path / "env/workspace/.traceforge").rglob("*")
               if path.is_file())


def test_conflicting_shell_decodes_are_recorded_without_replacing_initial_bytes(tmp_path):
    first = batch([call("Get-Content app.py -TotalCount 2")], [result("old\n")])
    later = batch([call("Get-Content app.py -Encoding UTF8 | Select-Object -First 2")],
                  [result("different\n")])
    later["call_id"] = "later"
    replay = replay_from_timeline([first, later])
    assert replay.files[0].content == "old\n"
    assert any(row["reason"] == "read_segment_conflict" for row in replay.partial_evidence)
    materialize_environment(replay, {"files": []}, tmp_path / "env",
                            evidence_refs={"batch", "later"})
    assert not (tmp_path / "env/workspace/.traceforge/source-excerpts.json").exists()


def test_shell_range_exceeding_requested_cap_is_not_exported(tmp_path):
    event = batch([call("Get-Content app.py -TotalCount 1")], [result("a\nb\n")])
    replay = replay_from_timeline([event])
    assert any(row["reason"] == "read_segment_range_mismatch" for row in replay.partial_evidence)
    materialize_environment(replay, {"files": []}, tmp_path / "env", evidence_refs={"batch"})
    assert not (tmp_path / "env/workspace/.traceforge/source-excerpts.json").exists()
