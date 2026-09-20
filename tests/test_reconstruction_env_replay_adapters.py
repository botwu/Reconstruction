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
