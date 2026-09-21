from __future__ import annotations

from traceforge.reconstruction.env_replay import replay_from_timeline


def _read(content: str, **arguments: object) -> dict[str, object]:
    return {
        "call_id": "read",
        "name": "read",
        "arguments": {"filePath": "/work/main.py", **arguments},
        "result_text": content,
    }


def test_opencode_read_decodes_file_path_numbering_and_reminder() -> None:
    replay = replay_from_timeline([
        {"call_id": "list", "name": "bash", "arguments": {
            "command": "rg --files -g '!build/**' | sed -n '1,160p'",
            "workdir": "/work",
        }, "result_text": "main.py\n"},
        _read(
            "<path>/work/main.py</path>\n<type>file</type>\n<content>\n"
            "3: def main():\n4:     return 1\n"
            "\n(Showing lines 3-4 of 8. Use offset=5 to continue.)\n</content>\n"
            "[Reminder]\nnot file content",
            offset=3, limit=2,
        ),
    ])
    assert len(replay.files) == 1
    assert replay.files[0].path == "main.py"
    assert replay.files[0].content == "def main():\n    return 1\n"
    assert replay.files[0].completeness == "PARTIAL"
    assert not any(item["reason"] == "read_after_unparsed_mutation" for item in replay.partial_evidence)


def test_opencode_read_incomplete_wrapper_does_not_seed_file() -> None:
    replay = replay_from_timeline([_read(
        "<path>/work/main.py</path>\n<type>file</type>\n<content>\n1: partial"
    )])
    assert replay.files == ()
    assert replay.partial_evidence[0]["reason"] == "read_result_missing"


def test_opencode_edit_then_read_with_file_path_stays_withheld() -> None:
    replay = replay_from_timeline([
        {"call_id": "edit", "name": "edit", "arguments": {
            "filePath": "/work/main.py", "oldString": "before", "newString": "after",
        }},
        _read(
            "<path>/work/main.py</path>\n<type>file</type>\n<content>\n"
            "1: after\n\n(End of file - total 1 lines)\n</content>"
        ),
    ])
    assert replay.files == ()
    assert replay.withheld_changes[0].path == "work/main.py"
    assert replay.partial_evidence[0]["reason"] == "read_after_first_mutation"


def test_sed_pipeline_with_side_effect_still_blocks_later_reads() -> None:
    replay = replay_from_timeline([
        {"call_id": "exec", "name": "bash", "arguments": {
            "command": "rg --files | sed -n '1p;w output.txt'",
        }, "result_text": "main.py\n"},
        _read("after\n"),
    ])
    assert replay.files == ()
    assert any(item["reason"] == "read_after_unparsed_mutation" for item in replay.partial_evidence)


def test_powershell_numbered_read_keeps_indent_and_drops_separator() -> None:
    replay = replay_from_timeline([
        {"call_id": "read", "name": "exec", "arguments": {
            "command": (
                "$lines=Get-Content -LiteralPath util.py; "
                "for($i=1;$i -le 3;$i++){ '{0,4}: {1}' -f $i,$lines[$i-1] }"
            ),
        }, "result_text": "   1: def main():\n   2:     return 1\n   3: \n"},
    ])
    assert replay.files[0].path == "util.py"
    assert replay.files[0].content == "def main():\n    return 1\n\n"
    assert replay.files[0].completeness == "PARTIAL"


def test_native_numbered_file_without_display_command_keeps_its_bytes() -> None:
    replay = replay_from_timeline([
        {"call_id": "read", "name": "exec", "arguments": {
            "command": "Get-Content -LiteralPath numbers.txt",
        }, "result_text": "1: alpha\n2: beta\n3: gamma\n"},
    ])
    assert replay.files[0].content == "1: alpha\n2: beta\n3: gamma\n"


def test_numbered_power_shell_reset_cannot_be_cleaned_into_trusted_file() -> None:
    replay = replay_from_timeline([
        {"call_id": "read", "name": "exec", "arguments": {
            "command": "Get-Content main.py | ForEach-Object { '{0,4}: {1}' -f $i++, $_ }",
        }, "result_text": "1: a\n2: b\n3: c\n1: x\n2: y\n3: z\n"},
    ])
    assert replay.files == ()


def test_literal_export_prefix_keeps_readonly_git_status_trusted() -> None:
    replay = replay_from_timeline([
        {
            "call_id": "status",
            "name": "bash",
            "arguments": {
                "command": (
                    "export CI=true DEBIAN_FRONTEND=noninteractive "
                    "GIT_TERMINAL_PROMPT=0 VISUAL=''; git status --short"
                )
            },
            "result_text": "",
        },
        {
            "call_id": "read",
            "name": "read_file",
            "arguments": {"filePath": "main.py"},
            "result_text": "print(1)\n",
        },
    ])
    assert [item.path for item in replay.files] == ["main.py"]
    assert not any(
        item.get("reason") in {"read_after_unparsed_mutation", "read_after_first_mutation"}
        for item in replay.partial_evidence
    )


def test_quoted_export_command_substitution_remains_a_mutation_barrier() -> None:
    replay = replay_from_timeline([
        {
            "call_id": "status",
            "name": "bash",
            "arguments": {
                "command": "export CI='$(touch generated.py)'; git status --short"
            },
            "result_text": "",
        },
        {
            "call_id": "read",
            "name": "read_file",
            "arguments": {"filePath": "main.py"},
            "result_text": "print(1)\n",
        },
    ])
    assert replay.files == ()
    assert any(
        item.get("reason") in {"read_after_unparsed_mutation", "read_after_first_mutation"}
        for item in replay.partial_evidence
    )


def test_native_read_tool_error_does_not_shadow_later_source_read() -> None:
    replay = replay_from_timeline([
        {
            "call_id": "bad",
            "name": "Read",
            "arguments": {
                "file_path": "/work/main.py",
                "offset": 1,
                "limit": 20,
                "pages": "",
                "workdir": "/work",
            },
            "result_text": (
                '<tool_use_error>Invalid pages parameter: "". '
                "Use formats like 1-5.</tool_use_error>"
            ),
        },
        {
            "call_id": "cleared",
            "name": "Read",
            "status": "cleared",
            "arguments": {
                "file_path": "/work/main.py",
                "offset": 1,
                "limit": 20,
                "workdir": "/work",
            },
            "result_text": "[tool result content cleared]",
        },
        {
            "call_id": "good",
            "name": "Read",
            "arguments": {
                "file_path": "/work/main.py",
                "offset": 1,
                "limit": 20,
                "pages": "1",
                "workdir": "/work",
            },
            "result_text": "1\tdef main():\n2\t    return 1\n3\t",
        },
    ])
    assert [item.path for item in replay.files] == ["main.py"]
    assert replay.files[0].content == "def main():\n    return 1\n"
    assert replay.files[0].completeness == "PARTIAL"
    assert not any(item.get("path") == "main.py" and item.get("reason") == "read_result_missing"
                   for item in replay.partial_evidence)


def test_native_read_numbering_preserves_source_tabs_and_indent() -> None:
    replay = replay_from_timeline([{
        "call_id": "read",
        "name": "Read",
        "arguments": {
            "file_path": "/work/main.py", "offset": 1, "limit": 3, "workdir": "/work"
        },
        "result_text": "1\tif ready:\n2\t    return\tvalue\n3\t",
    }])
    assert replay.files[0].content == "if ready:\n    return\tvalue\n"


def test_quoted_grep_pattern_is_not_a_redirect_target() -> None:
    replay = replay_from_timeline([{
        "call_id": "grep",
        "name": "exec",
        "arguments": {
            "command": (
                'rg -n "#include <fstream>|#include <sstream>" '
                "mc_core/task.cpp"
            )
        },
        "result_text": "mc_core/task.cpp:3:#include <fstream>\n",
    }])
    assert replay.withheld_changes == ()
    assert not any(item.get("path") == "|#include" for item in replay.partial_evidence)
    assert list(replay.unknown_mutation_barriers) == ["grep"]


def _wrapped_read(start: int, end: int, total: int, lines: str, *, call_id: str) -> dict[str, object]:
    footer = (
        f"(End of file - total {total} lines)"
        if end == total
        else f"(Showing lines {start}-{end} of {total}. Use offset={end + 1} to continue.)"
    )
    return {
        "call_id": call_id,
        "name": "Read",
        "arguments": {
            "file_path": "/work/segmented.py",
            "offset": start,
            "limit": end - start + 1,
            "workdir": "/work",
        },
        "result_text": (
            "<path>/work/segmented.py</path>\n<type>file</type>\n<content>\n"
            + lines
            + f"\n{footer}\n</content>"
        ),
    }


def test_native_read_segments_merge_only_on_complete_consistent_coverage() -> None:
    first = "".join(f"{line}: line-{line}\n" for line in range(1, 4))
    second = "".join(f"{line}: line-{line}\n" for line in range(3, 6))
    replay = replay_from_timeline([
        _wrapped_read(1, 3, 5, first, call_id="first"),
        _wrapped_read(3, 5, 5, second, call_id="second"),
    ])
    assert replay.files[0].completeness == "COMPLETE"
    assert replay.files[0].content == "".join(f"line-{line}\n" for line in range(1, 6))


def test_native_read_segments_with_gap_remain_partial_and_auditable() -> None:
    first = "".join(f"{line}: line-{line}\n" for line in range(1, 3))
    last = "".join(f"{line}: line-{line}\n" for line in range(4, 6))
    replay = replay_from_timeline([
        _wrapped_read(1, 2, 5, first, call_id="first"),
        _wrapped_read(4, 5, 5, last, call_id="last"),
    ])
    assert len(replay.files) == 1
    assert replay.files[0].completeness == "PARTIAL"
    segment_ids = {
        item["source_event_id"]
        for item in replay.partial_evidence
        if item.get("reason") == "read_segment"
    }
    assert segment_ids == {"first", "last"}


def test_native_read_segments_conflict_stays_partial() -> None:
    first = "1: alpha\n2: beta\n"
    second = "2: changed\n3: gamma\n"
    replay = replay_from_timeline([
        _wrapped_read(1, 2, 3, first, call_id="first"),
        _wrapped_read(2, 3, 3, second, call_id="second"),
    ])
    assert len(replay.files) == 1
    assert replay.files[0].completeness == "PARTIAL"
    assert any(
        item.get("reason") == "read_segment_conflict"
        for item in replay.partial_evidence
    )
