"""并行工具结果按原槽位关联，正文、目录和初态边界不得靠猜测恢复。"""

import json

import pytest

from traceforge.reconstruction.env_replay import (
    _unknown_looks_like_mutation,
    normalize_file_ops,
    replay_from_timeline,
)

UTF8 = "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "


def batch(calls, results, *, source_tail="for (const r of results) text(r);"):
    source = "const results = await Promise.all([" + ",".join(
        "tools.exec_command(" + json.dumps(call) + ")" for call in calls
    ) + "]);\n" + source_tail
    texts = ["Script completed\nWall time 1 seconds\nOutput:\n"] + [
        json.dumps(result) if isinstance(result, dict) else result for result in results
    ]
    return {
        "call_id": "batch", "name": "exec", "arguments": {"input": source},
        "result_text": "".join(texts),
        "result_blocks": [{"index": i, "text": text} for i, text in enumerate(texts)],
    }


def call(command, workdir="/work"):
    return {"cmd": command, "workdir": workdir, "shell": "powershell"}


def result(text):
    return {"exit_code": 0, "output": text, "wall_time_seconds": 0.1}


def test_parallel_json_preserves_source_bytes_and_nested_context():
    event = batch(
        [call(UTF8 + "Get-Content -LiteralPath app.py", r"F:\Project\demo"),
         call("Get-Content -LiteralPath README.md", r"F:\Project\demo")],
        [result("if ready:\n    value = 'README.md:9: example'\n"), result("# Demo\n")],
    )
    replay = replay_from_timeline([event])
    assert {f.path: f.content for f in replay.files} == {
        "app.py": "if ready:\n    value = 'README.md:9: example'\n", "README.md": "# Demo\n",
    }
    assert replay.workspace_root.replace("\\", "/") == "F:/Project/demo"
    ops = normalize_file_ops([event])
    assert [(op["result_block_index"], op["shell"]) for op in ops] == [(1, "powershell"), (2, "powershell")]
    assert all(op["workdir"] == r"F:\Project\demo" for op in ops)


def test_bad_middle_json_retains_slot_and_valid_following_file():
    event = batch(
        [call("Get-Content first.py"), call("Select-String -Path *.py -Pattern TODO"),
         call("Get-Content last.py")],
        [result("first\n"), '{"wall_time_seconds":2.[REDACTED],"output":"bad"}', result("last\n")],
    )
    replay = replay_from_timeline([event])
    assert {f.path: f.content for f in replay.files} == {"first.py": "first\n", "last.py": "last\n"}
    bad = next(row for row in replay.partial_evidence if row.get("result_error") == "EXEC_RESULT_INVALID_JSON")
    assert bad["result_block_index"] == 2
    last = next(op for op in normalize_file_ops([event]) if op.get("path") == "last.py")
    assert last["result_block_index"] == 3


def test_different_nested_workdirs_do_not_collapse_file_identity():
    event = batch(
        [call("Get-Content main.py", "/work/a"), call("Get-Content main.py", "/work/b")],
        [result("a\n"), result("b\n")],
    )
    replay = replay_from_timeline([event])
    assert replay.workspace_root == "/work"
    assert {f.path: f.content for f in replay.files} == {"a/main.py": "a\n", "b/main.py": "b\n"}


@pytest.mark.parametrize("tail,results", [
    ("for (const r of results.reverse()) text(r);", [result("a"), result("b")]),
    ("for (const r of results) text(r);", [result("a")]),
])
def test_unproven_mapping_never_assigns_output_to_a_file(tail, results):
    event = batch([call("Get-Content a.py"), call("Get-Content b.py")], results, source_tail=tail)
    assert replay_from_timeline([event]).files == ()


@pytest.mark.parametrize("bad_result", [
    {"exit_code": 1, "output": "failed body"},
    {"exit_code": True, "output": "ambiguous body"},
    {"session_id": 10, "output": "still running"},
    {"exit_code": 0, "output": ["wrong type"]},
])
def test_invalid_or_unfinished_result_is_not_source(bad_result):
    event = batch([call("Get-Content bad.py"), call("Get-Content good.py")],
                  [bad_result, result("good\n")])
    assert {f.path: f.content for f in replay_from_timeline([event]).files} == {"good.py": "good\n"}


def test_mixed_parallel_execution_does_not_invent_read_before_mutation():
    before = {"call_id": "before", "name": "read", "arguments": {"path": "old.py"},
              "result_text": "original\n"}
    event = batch(
        [call("Get-Content new.py"), call('python -c "run_step()"')],
        [result("untrusted\n"), result("")],
    )
    after = {"call_id": "after", "name": "read", "arguments": {"path": "later.py"},
             "result_text": "later\n"}
    replay = replay_from_timeline([before, event, after])
    assert {f.path: f.content for f in replay.files} == {"old.py": "original\n"}
    assert any(row.get("result_error") == "PARALLEL_EXECUTION_ORDER_UNKNOWN" for row in replay.partial_evidence)
    assert any(row["reason"] == "read_after_unparsed_mutation" and row["path"] == "later.py"
               for row in replay.partial_evidence)


@pytest.mark.parametrize("command", [
    "Get-Content parser.py -TotalCount 260",
    "Get-Content parser.py -totalcount 260",
    "Get-Content parser.py | Select-Object -First 260",
    "Get-Content parser.py | Select-Object -Skip 260",
])
def test_capped_or_sliced_powershell_read_remains_partial(command):
    event = batch([call(command)], [result("observed\n")])
    replay = replay_from_timeline([event])
    assert replay.files[0].content == "observed\n"
    assert replay.files[0].completeness == "PARTIAL"


def test_only_fixed_encoding_prefix_is_exempt_from_mutation_barrier():
    assert not _unknown_looks_like_mutation(UTF8 + "Get-Content app.py")
    assert _unknown_looks_like_mutation(UTF8 + 'python -c "run_step()"')
    assert _unknown_looks_like_mutation(UTF8 + "Set-Content app.py changed")
    assert _unknown_looks_like_mutation(
        "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(run_step()); Get-Content app.py"
    )


def test_workdirs_are_shared_coordinates_across_batches():
    first = batch([call("Get-Content main.py", "/work/a")], [result("a\n")])
    second = batch([call("Get-Content main.py", "/work/b")], [result("b\n")])
    second["call_id"] = "second-batch"
    replay = replay_from_timeline([first, second])
    assert replay.workspace_root == "/work"
    assert {f.path: f.content for f in replay.files} == {"a/main.py": "a\n", "b/main.py": "b\n"}


@pytest.mark.parametrize("key,value", [("workdir", 1), ("shell", False)])
def test_invalid_context_types_are_diagnostic_not_crashes(key, value):
    arguments = {**call("Get-Content app.py"), key: value}
    replay = replay_from_timeline([batch([arguments], [result("body")])])
    assert replay.files == ()
    assert any(row.get("result_error") == "EXEC_CONTEXT_INVALID" for row in replay.partial_evidence)


@pytest.mark.parametrize("command,output", [
    ('Get-Content app.py; Write-Output "extra"', "body\nextra\n"),
    ("Get-Content app.py | Select-String -Pattern TODO", "app.py:9: TODO\n"),
    ("Get-Content *.py", "one body\nanother body\n"),
])
def test_stdout_transform_or_multiple_files_never_becomes_source(command, output):
    replay = replay_from_timeline([batch([call(command)], [result(output)])])
    assert replay.files == ()
    assert any(row.get("result_error") == "EXEC_STDOUT_NOT_PLAIN_FILE" for row in replay.partial_evidence)


def test_existing_sequential_text_wrapper_stays_compatible():
    event = {
        "call_id": "sequential", "name": "exec",
        "arguments": {"input": 'const r = await tools.exec_command({cmd: "Get-Content app.py"}); text(r);'},
        "result_text": "observed\n", "result_blocks": [{"index": 0, "text": "observed\n"}],
    }
    assert replay_from_timeline([event]).files[0].content == "observed\n"


@pytest.mark.parametrize("with_blocks", [True, False])
def test_numbered_parallel_output_does_not_imply_execution_order(with_blocks):
    event = batch([call("Get-Content app.py"), call('python -c "run_step()"')],
                  [result("unsafe"), result("")])
    event["result_text"] = "--- result 1 ---\nunsafe\n--- result 2 ---\n"
    if with_blocks:
        event["result_blocks"] = [{"index": 0, "text": event["result_text"]}]
    else:
        event.pop("result_blocks")
    replay = replay_from_timeline([event])
    assert replay.files == ()
    assert any(row.get("result_error") == "PARALLEL_EXECUTION_ORDER_UNKNOWN"
               for row in replay.partial_evidence)


def test_numbered_parallel_output_preserves_child_workdirs():
    event = batch([call("Get-Content main.py", "/work/a"),
                   call("Get-Content main.py", "/work/b")], [])
    event["result_text"] = "--- result 1 ---\na\n--- result 2 ---\nb\n"
    event["result_blocks"] = [{"index": 0, "text": event["result_text"]}]
    replay = replay_from_timeline([event])
    assert {f.path: f.content.strip() for f in replay.files} == {"a/main.py": "a", "b/main.py": "b"}


def test_posix_glob_does_not_merge_separate_source_files():
    replay = replay_from_timeline([batch([call("cat *.py")], [result("one\ntwo\n")])])
    assert replay.files == ()


def test_native_write_and_parallel_read_share_workspace_coordinates():
    written = {"call_id": "write", "name": "write_file",
               "arguments": {"path": "main.py", "workdir": "/work/a", "content": "changed"},
               "result_text": "ok"}
    event = batch([call("Get-Content main.py", "/work/a"),
                   call("Get-Content other.py", "/work/b")],
                  [result("changed"), result("other")])
    replay = replay_from_timeline([written, event])
    assert {f.path: f.content for f in replay.files} == {"b/other.py": "other"}
    assert replay.withheld_changes[0].path == "a/main.py"


def test_binary_encoding_output_is_not_source():
    replay = replay_from_timeline([batch([call("Get-Content app.py -Encoding Byte")],
                                        [result("97\n98\n")])])
    assert replay.files == ()


@pytest.mark.parametrize("raw_arguments", [False, True])
def test_cross_drive_batches_do_not_alias_files(raw_arguments):
    first = batch([call("Get-Content main.py", "F:/Project/demo")], [result("f")])
    second = batch([call("Get-Content main.py", "G:/Project/demo")], [result("g")])
    second["call_id"] = "other-drive"
    if raw_arguments:
        for event in (first, second):
            event["arguments"] = event["arguments"]["input"]
    replay = replay_from_timeline([first, second])
    assert replay.files == ()
    assert any(row.get("result_error") == "EXEC_WORKDIR_UNRESOLVED"
               for row in replay.partial_evidence)


def test_parallel_stdout_projection_preserves_named_files_and_slots():
    command = ('Write-Output "--- a.py ---"; Get-Content a.py; '
               'Write-Output "--- b.py ---"; Get-Content b.py')
    event = batch([call(command), call("rg TODO *.py")],
                  ["--- a.py ---\nA\n--- b.py ---\nB\n", "c.py:9: TODO\n"],
                  source_tail="for (const r of results) text(r.output);")
    replay = replay_from_timeline([event])
    assert {f.path: f.content.strip() for f in replay.files} == {"a.py": "A", "b.py": "B"}
    assert all(row["result_block_index"] == 1 for row in replay.partial_evidence
               if row["reason"] == "exec_result_observation")


def test_parallel_json_projection_preserves_exit_failure():
    tail = "for (const r of results) text(JSON.stringify({exit_code:r.exit_code, output:r.output}));"
    event = batch([call("Get-Content failed.py"), call("Get-Content good.py")],
                  [{"exit_code": 1, "output": "failed"}, result("good\n")], source_tail=tail)
    assert {f.path: f.content for f in replay_from_timeline([event]).files} == {"good.py": "good\n"}


def test_parallel_stdout_projection_does_not_bypass_mixed_write_barrier():
    event = batch([call("Get-Content app.py"), call("Set-Content app.py changed")],
                  ["changed", ""], source_tail="for (const r of results) text(r.output);")
    assert replay_from_timeline([event]).files == ()
