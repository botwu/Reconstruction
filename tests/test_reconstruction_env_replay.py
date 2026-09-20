from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.reconstruction.completion_holes import index_completion_holes
from traceforge.reconstruction.env_replay import (
    _paths_from_unknown_command,
    foreign_mutation_paths,
    is_identifier_filename,
    prior_visible_files,
    replay_from_timeline,
    replay_selected_environment,
)


def test_exec_cat_is_first_observation() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {"command": "cat foo.py"},
                "result_text": "def main():\n    return 1\n",
            }
        ]
    )
    assert [item.path for item in replay.files] == ["foo.py"]
    assert replay.files[0].content.startswith("def main")
    assert replay.files[0].completeness == "COMPLETE"
    assert replay.unknown_mutation_barriers == ()


def test_write_after_read_keeps_earliest_and_withholds_change() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "read",
                "arguments": {"path": "a.txt"},
                "result_text": "before\n",
            },
            {
                "call_id": "c2",
                "name": "edit",
                "arguments": {"path": "a.txt", "new_string": "after"},
            },
        ]
    )
    assert replay.files[0].content == "before\n"
    assert replay.files[0].completeness == "COMPLETE"
    assert replay.withheld_changes[0].classification == "withheld_change"


def test_unscoped_exec_withholds_later_initial_observation() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {"command": "npm install"},
                "result_text": "ok",
            },
            {
                "call_id": "c2",
                "name": "exec",
                "arguments": {"command": "cat lib.py"},
                "result_text": "x = 1\n",
            },
        ]
    )
    assert replay.files == ()
    assert list(replay.unknown_mutation_barriers) == ["c1"]
    assert any(item.get("reason") == "read_after_unparsed_mutation" for item in replay.partial_evidence)


def test_agent_created_file_stays_out_of_workspace() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {"command": "echo hi > new.txt"},
                "result_text": "",
            }
        ]
    )
    assert replay.files == ()
    assert replay.withheld_changes[0].classification == "agent_created_file"


def test_js_exec_sed_is_partial_read() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const r = await tools.exec_command("
                        '{cmd:"sed -n \'1,20p\' package.json"})'
                    )
                },
                "result_text": '{\n  "name": "demo"\n}\n',
            }
        ]
    )
    assert [item.path for item in replay.files] == ["package.json"]
    assert replay.files[0].completeness == "PARTIAL"
    assert replay.files[0].content.startswith("{")


def test_js_quoted_cmd_and_get_content() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        'const r = await tools.shell_command({"command":'
                        '"Get-Content -LiteralPath \'scripts/app.js\'"})'
                    )
                },
                "result_text": "export const x = 1\n",
            }
        ]
    )
    assert [item.path for item in replay.files] == ["scripts/app.js"]
    assert replay.files[0].completeness == "COMPLETE"


def test_windows_read_is_relativized_to_session_workdir() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c0",
                "name": "exec",
                "arguments": {
                    "command": "Get-ChildItem",
                    "workdir": r"C:\Users\admin\workspace",
                },
                "result_text": "ok",
            },
            {
                "call_id": "c1",
                "name": "read",
                "arguments": {
                    "path": r"C:\Users\admin\workspace\led_analyzer\app.py",
                    "offset": 10,
                    "limit": 20,
                },
                "result_text": "print(1)\n",
            },
        ]
    )
    assert [item.path for item in replay.files] == ["led_analyzer/app.py"]
    assert replay.files[0].completeness == "PARTIAL"


def test_codex_skill_path_is_not_workspace() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        'const r = await tools.exec_command({cmd:'
                        '"sed -n 1,20p /Users/x/.codex/skills/foo/SKILL.md"})'
                    )
                },
                "result_text": "# skill\n",
            }
        ]
    )
    assert replay.files == ()
    assert list(replay.unknown_mutation_barriers) == ["c1"]


def test_stderr_redirect_is_not_a_write() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {"command": "rg foo README.md 2>/dev/null"},
                "result_text": "hit",
            }
        ]
    )
    assert replay.files == ()
    assert replay.withheld_changes == ()
    assert list(replay.unknown_mutation_barriers) == ["c1"]


def test_named_dump_splits_multiple_files() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "command": (
                        "$files='a.md','b.md'; foreach ($file in $files) "
                        '{ "--- $file ---"; Get-Content $file }'
                    )
                },
                "result_text": "--- a.md ---\nalpha\n--- b.md ---\nbeta\n",
            }
        ]
    )
    assert [item.path for item in replay.files] == ["a.md", "b.md"]
    assert replay.files[0].content == "alpha\n"
    assert replay.files[1].content == "beta\n"


def test_powershell_variable_path_is_rejected() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {"command": "Get-Content -LiteralPath $file"},
                "result_text": "nope",
            }
        ]
    )
    assert replay.files == ()


def test_powershell_assigned_get_content_is_a_read() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "command": "$p='Injector.cpp'; $a=Get-Content $p; $a[180..575]",
                },
                "result_text": (
                    " 180: int inject() {\r\n"
                    " 181:     return 0;\r\n"
                    " 182: }\r\n"
                ),
            }
        ]
    )
    assert [item.path for item in replay.files] == ["Injector.cpp"]
    assert replay.files[0].completeness == "PARTIAL"
    assert "int inject" in replay.files[0].content
    assert replay.unknown_mutation_barriers == ()


def test_readonly_unknown_then_named_dump_plants_partial() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const r = await Promise.all([\n"
                        '  tools.exec_command({cmd:"$p=\'Injector.cpp\'; '
                        '$a=Get-Content $p; $a[180..575]"}),\n'
                        '  tools.exec_command({cmd:"$a=Get-Content modules/API.h; '
                        '$a[154..290]"})\n'
                        "]);"
                    )
                },
                "result_text": "mixed blob",
            },
            {
                "call_id": "c2",
                "name": "exec",
                "arguments": {
                    "command": (
                        "$files=@('Injector.cpp','modules/API.h'); "
                        "foreach($f in $files){ Write-Output \"--- $f ---\" }"
                    )
                },
                "result_text": (
                    "--- Injector.cpp ---\n"
                    " 180: int inject() {\r\n"
                    " 181:     return 0;\r\n"
                    " 182: }\r\n"
                    "--- modules/API.h ---\n"
                    " 154: #pragma once\r\n"
                    " 155: struct Api {};\r\n"
                    " 156: \r\n"
                ),
            },
        ]
    )
    assert replay.files == ()
    assert "c1" in replay.unknown_mutation_barriers
    assert "c2" in replay.unknown_mutation_barriers


def test_multiple_get_content_in_one_exec_is_barrier() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "command": "Get-Content a.py; Get-Content b.py",
                },
                "result_text": "both",
            },
            {
                "call_id": "c2",
                "name": "exec",
                "arguments": {"command": "cat a.py"},
                "result_text": "ALPHA\n",
            },
        ]
    )
    assert [item.path for item in replay.files] == ["a.py"]
    assert replay.files[0].content == "ALPHA\n"
    assert list(replay.unknown_mutation_barriers) == ["c1"]
    assert all(
        item.get("reason") != "unparsed_mutation_scope" for item in replay.partial_evidence
    )
    assert all(item.get("reason") != "read_after_unparsed_mutation" for item in replay.partial_evidence)


def test_read_after_same_path_mutation_is_dropped() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "write",
                "arguments": {"path": "new.txt", "content": "new"},
            },
            {
                "call_id": "c2",
                "name": "read",
                "arguments": {"path": "new.txt"},
                "result_text": "new",
            },
        ]
    )
    assert replay.files == ()
    assert replay.partial_evidence[0]["reason"] == "read_after_first_mutation"


def test_unknown_command_extracts_path_ctor_but_rejects_junk() -> None:
    extracted = _paths_from_unknown_command(
        "python3 -c \"from pathlib import Path; Path('answer.py').write_text('SOLUTION')\""
    )
    assert extracted == ["answer.py"]
    assert _paths_from_unknown_command("sed -n '154..290p' notes.md") == []
    assert "154..290" not in _paths_from_unknown_command("echo 154..290 0.5f 10.1177 0.4.12")
    assert "0.5f" not in _paths_from_unknown_command("echo 0.5f")
    assert "10.1177" not in _paths_from_unknown_command("echo 10.1177/doi")
    assert _paths_from_unknown_command("sha256sum tmp/$d-src.sha") == []


def test_unknown_python_write_then_cat_is_not_complete() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "command": (
                        "python3 -c \"from pathlib import Path; "
                        "Path('answer.py').write_text('SOLUTION')\""
                    )
                },
                "result_text": "",
            },
            {
                "call_id": "c2",
                "name": "exec",
                "arguments": {"command": "cat answer.py"},
                "result_text": "SOLUTION",
            },
        ]
    )
    assert replay.files == ()
    assert "c1" in replay.unknown_mutation_barriers
    reasons = {item["reason"] for item in replay.partial_evidence}
    assert "unparsed_mutation_scope" in reasons
    assert "read_after_unparsed_mutation" in reasons
    leaked = [
        item
        for item in replay.partial_evidence
        if item.get("reason") == "read_after_unparsed_mutation"
    ]
    assert leaked[0]["path"] == "answer.py"
    assert leaked[0]["content"] == "SOLUTION"


@pytest.mark.parametrize(
    "mutation_command",
    [
        "cp template.py answer.py",
        "mv template.py answer.py",
        "python3 -c \"open('answer.py','a').write('SOLUTION')\"",
        "sed -i 's/ORIGINAL/SOLUTION/' answer.py",
        "Set-Content -Path answer.py -Value SOLUTION; Get-Content -Path answer.py",
    ],
)
def test_unparsed_copy_append_and_compound_writes_cannot_seed_initial_file(
    mutation_command: str,
) -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "mutate",
                "name": "exec",
                "arguments": {"command": mutation_command},
                "result_text": "SOLUTION\n",
            },
            {
                "call_id": "read-after",
                "name": "exec",
                "arguments": {"command": "cat answer.py"},
                "result_text": "SOLUTION\n",
            },
        ]
    )
    assert replay.files == ()
    assert "mutate" in replay.unknown_mutation_barriers
    assert any(
        item.get("reason") == "read_after_unparsed_mutation"
        for item in replay.partial_evidence
    )


def test_install_script_does_not_certify_later_file_as_initial() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {"command": "npm install"},
                "result_text": "ok",
            },
            {
                "call_id": "c2",
                "name": "exec",
                "arguments": {"command": "cat lib.py"},
                "result_text": "x = 1\n",
            },
        ]
    )
    assert replay.files == ()
    assert any(item.get("reason") == "read_after_unparsed_mutation" for item in replay.partial_evidence)


def test_first_read_then_unknown_write_keeps_first_observation() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {"command": "cat answer.py"},
                "result_text": "ORIGINAL\n",
            },
            {
                "call_id": "c2",
                "name": "exec",
                "arguments": {
                    "command": "python3 -c \"Path('answer.py').write_text('SOLUTION')\""
                },
                "result_text": "",
            },
        ]
    )
    assert [item.path for item in replay.files] == ["answer.py"]
    assert replay.files[0].content == "ORIGINAL\n"
    assert replay.files[0].completeness == "COMPLETE"


def test_create_then_read_stays_out_of_public_tree() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "write",
                "arguments": {"path": "created.py", "content": "new"},
            },
            {
                "call_id": "c2",
                "name": "exec",
                "arguments": {"command": "cat created.py"},
                "result_text": "new",
            },
        ]
    )
    assert replay.files == ()
    assert replay.withheld_changes[0].classification == "agent_created_file"
    assert replay.partial_evidence[0]["reason"] == "read_after_first_mutation"


def test_other_span_write_is_not_this_span_initial_state() -> None:
    timeline = [
        {
            "call_id": "w1",
            "span_id": "s-other",
            "name": "write",
            "arguments": {"path": "shared.py", "content": "OTHER"},
            "result_text": "ok",
        },
        {
            "call_id": "r1",
            "span_id": "s-this",
            "name": "exec",
            "arguments": {"command": "cat shared.py"},
            "result_text": "OTHER",
        },
    ]
    mutated, untrusted = foreign_mutation_paths(timeline, {"s-this"})
    assert mutated == {"shared.py"}
    replay = replay_selected_environment(timeline, {"s-this"})
    assert replay.files == ()
    assert replay.partial_evidence[0]["reason"] == "read_after_first_mutation"
    assert prior_visible_files(timeline, {"s-this"}) == ()


def test_other_span_read_is_visible_on_this_task() -> None:
    timeline = [
        {
            "call_id": "r0",
            "span_id": "s-other",
            "name": "exec",
            "arguments": {"command": "cat leftover.py"},
            "result_text": "LEFTOVER\n",
        },
        {
            "call_id": "r1",
            "span_id": "s-this",
            "name": "exec",
            "arguments": {"command": "cat foo.py"},
            "result_text": "FOO\n",
        },
    ]
    visible = prior_visible_files(timeline, {"s-this"})
    assert [item.path for item in visible] == ["leftover.py"]
    assert visible[0].provenance == "PRIOR_VISIBLE_OBSERVATION"
    replay = replay_selected_environment(timeline, {"s-this"})
    assert {item.path: item.content for item in replay.files} == {
        "foo.py": "FOO\n",
        "leftover.py": "LEFTOVER\n",
    }


def test_later_span_read_is_visible_on_session_environment() -> None:
    timeline = [
        {
            "call_id": "r1",
            "span_id": "s-this",
            "name": "exec",
            "arguments": {"command": "cat foo.py"},
            "result_text": "FOO\n",
        },
        {
            "call_id": "r2",
            "span_id": "s-later",
            "name": "exec",
            "arguments": {"command": "cat later.py"},
            "result_text": "LATER\n",
        },
    ]
    replay = replay_selected_environment(timeline, {"s-this"})
    assert {item.path: item.content for item in replay.files} == {
        "foo.py": "FOO\n",
        "later.py": "LATER\n",
    }


def test_l22_like_prior_reads_thicken_tree_without_untrusted_injector() -> None:
    timeline = [
        {
            "call_id": "r0",
            "span_id": "s-earlier",
            "name": "exec",
            "arguments": {"command": "cat modules/API.h"},
            "result_text": "#pragma once\n",
        },
        {
            "call_id": "r1",
            "span_id": "s-earlier",
            "name": "exec",
            "arguments": {"command": "cat modules/GUI.h"},
            "result_text": "#pragma once\nstruct Gui {};\n",
        },
        {
            "call_id": "u1",
            "span_id": "s-this",
            "name": "exec",
            "arguments": {
                "command": (
                    "python3 -c \"from pathlib import Path; "
                    "Path('Injector.cpp').write_text('hacked')\""
                )
            },
            "result_text": "",
        },
        {
            "call_id": "r2",
            "span_id": "s-this",
            "name": "exec",
            "arguments": {"command": "cat Loader.cpp"},
            "result_text": "int loader() { return 1; }\n",
        },
        {
            "call_id": "r3",
            "span_id": "s-this",
            "name": "exec",
            "arguments": {"command": "cat imgui/imgui.cpp"},
            "result_text": "void imgui() {}\n",
        },
        {
            "call_id": "r4",
            "span_id": "s-this",
            "name": "exec",
            "arguments": {"command": "cat Injector.cpp"},
            "result_text": "hacked",
        },
    ]
    replay = replay_selected_environment(timeline, {"s-this"})
    paths = {item.path for item in replay.files}
    assert paths == {"modules/API.h", "modules/GUI.h"}
    assert "Injector.cpp" not in paths
    from traceforge.reconstruction.completion_holes import index_completion_holes

    index = index_completion_holes(replay, timeline, {"core_objective": "读 injector"})
    assert all(item["path"] != "Injector.cpp" for item in index.holes)


def test_unknown_python_write_then_cat_still_hidden_with_visible_prior() -> None:
    timeline = [
        {
            "call_id": "r0",
            "span_id": "s-other",
            "name": "exec",
            "arguments": {"command": "cat leftover.py"},
            "result_text": "LEFTOVER\n",
        },
        {
            "call_id": "c1",
            "span_id": "s-this",
            "name": "exec",
            "arguments": {
                "command": (
                    "python3 -c \"from pathlib import Path; "
                    "Path('answer.py').write_text('SOLUTION')\""
                )
            },
            "result_text": "",
        },
        {
            "call_id": "c2",
            "span_id": "s-this",
            "name": "exec",
            "arguments": {"command": "cat answer.py"},
            "result_text": "SOLUTION",
        },
    ]
    replay = replay_selected_environment(timeline, {"s-this"})
    assert [item.path for item in replay.files] == ["leftover.py"]
    assert all(item.content != "SOLUTION" for item in replay.files)


def test_exec_wrapper_is_stripped_from_get_content() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        'const r = await tools.exec_command({cmd:"Get-Content build.bat"})'
                    )
                },
                "result_text": (
                    "Script completed\nWall time 0.7 seconds\nOutput:\n"
                    "@echo off\r\nsetlocal\r\n"
                ),
            }
        ]
    )
    assert [item.path for item in replay.files] == ["build.bat"]
    assert replay.files[0].content.startswith("@echo off")
    assert "Script completed" not in replay.files[0].content


def test_exit_code_wall_time_wrapper_is_stripped() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        'const r = await tools.shell_command({command:"Get-Content README.md"})'
                    )
                },
                "result_text": (
                    "Script completed\nWall time 0.5 seconds\nOutput:\n"
                    "Exit code: 0\nWall time: 0.5 seconds\nOutput:\n"
                    "\r\n## Xiaohongshu account monitoring\r\n\r\n"
                    "The workflow reuses the existing n8n instance.\r\n"
                ),
            }
        ]
    )
    assert [item.path for item in replay.files] == ["README.md"]
    assert replay.files[0].content.lstrip("\r\n").startswith("## Xiaohongshu")
    assert "Exit code:" not in replay.files[0].content
    assert "Wall time" not in replay.files[0].content
    assert "Output:" not in replay.files[0].content


def test_l49_listing_segments_do_not_bind_readme() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "call_readme_listing",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const results = await Promise.all([\n"
                        '  tools.shell_command({command:"Get-Command ssh,scp"}),\n'
                        '  tools.shell_command({command:"Get-ChildItem | '
                        'Format-Table Name,Length,LastWriteTime"}),\n'
                        '  tools.shell_command({command:"Get-Content README.md"})\n'
                        "]);\n"
                        "results.forEach((r,i)=>{text(`--- ${i+1} ---`);text(r)});\n"
                    )
                },
                "result_text": (
                    "Script completed\nWall time 0.4 seconds\nOutput:\n"
                    "--- 1 ---Exit code: 0\nWall time: 0.4 seconds\nOutput:\n"
                    "\r\nName    Source\r\n----    ------\r\n"
                    "ssh.exe C:\\Windows\\System32\\OpenSSH\\ssh.exe\r\n"
                    "--- 2 ---Exit code: 0\nWall time: 0.5 seconds\nOutput:\n"
                    "\r\nName            Length LastWriteTime\r\n"
                    "----            ------ -------------\r\n"
                    "README.md          1234 2026/7/23\r\n"
                    "--- 3 ---Exit code: 0\nWall time: 0.5 seconds\nOutput:\n"
                    "\r\nName            Length LastWriteTime\r\n"
                    "----            ------ -------------\r\n"
                    "deploy               1 2026/7/23\r\n"
                ),
            }
        ]
    )
    assert all(item.path != "README.md" for item in replay.files)
    assert "call_readme_listing" in replay.unknown_mutation_barriers


def test_misaligned_numbered_segments_do_not_bind_whole_blob() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "call_sql_mixed",
                "name": "exec",
                "arguments": {
                    "command": "Get-Content workflows/code/xhs_api_build_sql.js"
                },
                "result_text": (
                    "--- 1 ---Exit code: 0\nWall time: 0.3 seconds\nOutput:\n"
                    "137:  \"  'periodNoteCount', period_note_count,\",\n"
                    "151:  \"  'periodLikes', COALESCE(a.period_likes, 0),\",\n"
                    "--- 2 ---Exit code: 0\nWall time: 0.5 seconds\nOutput:\n"
                    "\r\nfunction text(value) {\r\n  return String(value ?? '').trim();\r\n}\r\n"
                    "--- 3 ---Exit code: 0\nWall time: 0.3 seconds\nOutput:\n"
                    "294:    function formatHeat(item) { return item.heatScore; }\n"
                ),
            }
        ]
    )
    assert all(
        item.path != "workflows/code/xhs_api_build_sql.js" for item in replay.files
    )
    assert "call_sql_mixed" in replay.unknown_mutation_barriers


def test_l49_numbered_segments_align_get_content_only() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const results = await Promise.all([\n"
                        '  tools.shell_command({command:"rg -n periodNoteCount '
                        'workflows/code/xhs_api_build_sql.js"}),\n'
                        '  tools.shell_command({command:"Get-Content '
                        'workflows/code/xhs_api_build_sql.js"}),\n'
                        '  tools.shell_command({command:"rg -n formatHeat '
                        'frontend/xhs-monitor.html"})\n'
                        "]);\n"
                        "results.forEach((r,i)=>{text(`--- ${i+1} ---`);text(r)});\n"
                    )
                },
                "result_text": (
                    "Script completed\nWall time 0.5 seconds\nOutput:\n"
                    "--- 1 ---Exit code: 0\nWall time: 0.3 seconds\nOutput:\n"
                    "137:  \"  'periodNoteCount', period_note_count,\",\n"
                    "--- 2 ---Exit code: 0\nWall time: 0.5 seconds\nOutput:\n"
                    "\r\nfunction text(value) {\r\n  return String(value ?? '').trim();\r\n}\r\n"
                    "--- 3 ---Exit code: 0\nWall time: 0.3 seconds\nOutput:\n"
                    "294:    function formatHeat(item) { return item.heatScore; }\n"
                ),
            }
        ]
    )
    assert [item.path for item in replay.files] == ["workflows/code/xhs_api_build_sql.js"]
    assert "function text(value)" in replay.files[0].content
    assert "periodNoteCount" not in replay.files[0].content
    assert "formatHeat" not in replay.files[0].content
    assert "Exit code:" not in replay.files[0].content


def test_get_content_convertfrom_json_does_not_bind_summary() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const result = await tools.shell_command({\n"
                        '  command: "$w = Get-Content -Raw -LiteralPath '
                        "'workflows/dist/xhs-monitor-v1.json' | ConvertFrom-Json\\n"
                        'Write-Output (ConvertTo-Json @{nodeCount=45})"\n'
                        "});"
                    )
                },
                "result_text": (
                    "Script completed\nWall time 0.5 seconds\nOutput:\n"
                    "Exit code: 0\nWall time: 0.5 seconds\nOutput:\n"
                    '{\r\n    "nodeCount":  45,\r\n    "duplicateNames":  []\r\n}\r\n'
                ),
            }
        ]
    )
    assert all(item.path != "workflows/dist/xhs-monitor-v1.json" for item in replay.files)


def test_promise_all_listing_does_not_bind_to_get_content() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "call_y684",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const r = await Promise.all([\n"
                        '  tools.exec_command({cmd:"rg -n Loader README* *.md"}),\n'
                        '  tools.exec_command({cmd:"Get-ChildItem | '
                        'Format-Table Name,Length,LastWriteTime"}),\n'
                        '  tools.exec_command({cmd:"Get-Content Injector.cpp"})\n'
                        "]);"
                    )
                },
                "result_text": (
                    "Script completed\nWall time 0.7 seconds\nOutput:\n"
                    "rg: README*: 文件名、目录名或卷标语法不正确。 (os error 123)\n"
                    "rg: *.md: 文件名、目录名或卷标语法不正确。 (os error 123)\n"
                    "\r\nName                Length  LastWriteTime\r\n"
                    "----                ------  -------------\r\n"
                    "Injector.cpp        23165   2026/7/23 12:05:43\r\n"
                    " 208:     static bool InjectViaRemoteThread() {\r\n"
                ),
            }
        ]
    )
    assert all(item.path != "Injector.cpp" for item in replay.files)
    assert "call_y684" in replay.unknown_mutation_barriers
    assert any(
        item.get("reason") == "unparsed_readonly" for item in replay.partial_evidence
    )


def test_mixed_numbered_slices_do_not_bind_to_last_get_content() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "call_X92",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const r = await Promise.all([\n"
                        '  tools.exec_command({cmd:"'
                        "$a=Get-Content imgui/imgui.cpp; "
                        "for($i=1038;$i -le 1065;$i++){ '{0,4}: {1}' -f ($i+1),$a[$i] }; "
                        "$a=Get-Content modules/RobloxCore.h; "
                        "for($i=70;$i -le 94;$i++){ '{0,4}: {1}' -f ($i+1),$a[$i] }"
                        '"}),\n'
                        '  tools.exec_command({cmd:"'
                        "$a=Get-Content README.md; "
                        "for($i=1;$i -le 55;$i++){ '{0,4}: {1}' -f $i,$a[$i-1] }"
                        '"})\n'
                        "]);"
                    )
                },
                "result_text": (
                    "1039:         AddRectFilled(ImVec2(x, sliderY), ImVec2(x + w * t, sliderY + 6), grabCol);\r\n"
                    "1040:     }\r\n"
                    "1041: \r\n"
                    "1059: bool ImGui::SliderInt(const char* label, int* v, int v_min, int v_max, const char* format) {\r\n"
                    "1060:     float fv = (float)*v;\r\n"
                    "1061:     bool ret = SliderFloat(label, &fv, (float)v_min, (float)v_max, format);\r\n"
                    "  71:     return Mem::ReadString((LPVOID)addr, buf, maxLen);\r\n"
                    "  72: }\r\n"
                    "  73: \r\n"
                    "  74: names[core] {\r\n"
                    "  75: \r\n"
                    "  76:     static DWORD_PTR g_DataModelPtr = 0;\r\n"
                    "   1: # UNC injector\r\n"
                    "   2: \r\n"
                    "   3: ## Features\r\n"
                    "   4: \r\n"
                    "   5: - ESP\r\n"
                    "   6: - Noclip\r\n"
                ),
            }
        ]
    )
    assert all(item.path != "README.md" for item in replay.files)
    assert "call_X92" in replay.unknown_mutation_barriers


def test_identifier_tokens_are_not_filenames() -> None:
    assert is_identifier_filename("dllName.c") is True
    assert is_identifier_filename("config.targetName.c") is True
    assert is_identifier_filename("value.c") is True
    assert is_identifier_filename("RobloxDLL.cpp") is False
    assert is_identifier_filename("modules/API.h") is False


def _l22_bindings_source() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "artifacts/eligible-live/L22-bindings/reconstruction_source.json"
    )


@pytest.mark.skipif(not _l22_bindings_source().is_file(), reason="L22-bindings source missing")
def test_l22_bindings_timeline_replay_does_not_misbind() -> None:
    source = json.loads(_l22_bindings_source().read_text(encoding="utf-8"))
    replay = replay_from_timeline(list(source.get("tool_timeline") or []))
    by_path = {item.path: item for item in replay.files}
    injector = by_path.get("Injector.cpp")
    if injector is not None:
        assert not injector.content.lstrip().startswith("rg:")
        assert "LastWriteTime" not in injector.content
    readme = by_path.get("README.md")
    if readme is not None:
        assert "AddRectFilled" not in readme.content
        assert "g_DataModelPtr" not in readme.content
    loader_bat = by_path.get("loader_build.bat")
    if loader_bat is not None:
        assert "WriteProcessMemory" not in loader_bat.content


def test_named_dump_survives_sibling_dumpbin_select_string() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const r = await Promise.all([\n"
                        '  tools.exec_command({cmd:"$files=@(\'Injector.cpp\','
                        "'Loader.cpp'); foreach($f in $files){ "
                        'Write-Output \\"--- $f ---\\"; $a=Get-Content $f }"}),\n'
                        '  tools.exec_command({cmd:"& dumpbin.exe /imports '
                        "Injector.exe | Select-String 'CreateRemoteThread'\"})\n"
                        "]);"
                    )
                },
                "result_text": (
                    "--- Injector.cpp ---\n"
                    " 317:         int queuedCount = 0;\r\n"
                    " 318:         return false;\r\n"
                    " 319:     }\r\n"
                    "--- Loader.cpp ---\n"
                    " 350:         AddLog(\"x\");\r\n"
                    " 351:         return false;\r\n"
                    " 352:     }\r\n"
                ),
            }
        ]
    )
    assert {item.path for item in replay.files} == {"Injector.cpp", "Loader.cpp"}
    assert all(item.completeness == "PARTIAL" for item in replay.files)


def test_named_dump_drops_when_sibling_writes() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const r = await Promise.all([\n"
                        '  tools.exec_command({cmd:"Set-Content -Path Injector.cpp '
                        '-Value FAKE"}),\n'
                        '  tools.exec_command({cmd:"$files=@(\'Injector.cpp\','
                        "'Loader.cpp'); foreach($f in $files){ "
                        'Write-Output \\"--- $f ---\\"; Get-Content $f }"}),\n'
                        "]);"
                    )
                },
                "result_text": (
                    "--- Injector.cpp ---\n"
                    " 317:         FAKE\r\n"
                    " 318:         FAKE\r\n"
                    " 319:         FAKE\r\n"
                    "--- Loader.cpp ---\n"
                    " 350:         FAKE\r\n"
                    " 351:         FAKE\r\n"
                    " 352:         FAKE\r\n"
                ),
            }
        ]
    )
    assert replay.files == ()


def test_syntax_check_does_not_poison_later_named_dumps() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "cl",
                "name": "exec",
                "arguments": {
                    "command": (
                        'cmd.exe /d /s /c "vcvars64.bat >nul && '
                        'cl.exe /nologo /std:c++17 /Zs Injector.cpp"'
                    )
                },
                "result_text": "Injector.cpp",
            },
            {
                "call_id": "dump",
                "name": "exec",
                "arguments": {
                    "command": (
                        "$files=@('Injector.cpp','Loader.cpp'); "
                        'foreach($f in $files){ Write-Output "--- $f ---"; '
                        "Get-Content $f }"
                    )
                },
                "result_text": (
                    "--- Injector.cpp ---\n"
                    " 317:         int queuedCount = 0;\r\n"
                    " 318:         return false;\r\n"
                    " 319:     }\r\n"
                    "--- Loader.cpp ---\n"
                    " 350:         AddLog(\"x\");\r\n"
                    " 351:         return false;\r\n"
                    " 352:     }\r\n"
                ),
            },
        ]
    )
    assert {item.path for item in replay.files} == {"Injector.cpp", "Loader.cpp"}


def test_named_dump_drops_trailing_dumpbin() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "command": (
                        "$files=@('Loader.cpp','loader_build.bat'); "
                        "foreach($f in $files){ Write-Output \"--- $f ---\" }; "
                        "& dumpbin.exe /imports Injector.exe"
                    )
                },
                "result_text": (
                    "--- Loader.cpp ---\n"
                    " 350:         AddLog(\"x\");\r\n"
                    " 351:         return false;\r\n"
                    " 352:     }\r\n"
                    "--- loader_build.bat ---\n"
                    "  40: echo.\r\n"
                    "  41: echo Compiling Loader.exe\r\n"
                    "  42: \r\n"
                    "                         654 WriteProcessMemory\r\n"
                    "                         47A QueueUserAPC\r\n"
                ),
            }
        ]
    )
    # Merely printing filenames is not evidence that the command read their bodies.
    assert replay.files == ()
    assert replay.unknown_mutation_barriers


def test_result_segments_align_multiple_get_content() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const r = await Promise.all([\n"
                        '  tools.exec_command({cmd:"Get-Content a.py"}),\n'
                        '  tools.exec_command({cmd:"Get-Content b.py"}),\n'
                        '  tools.exec_command({cmd:"Get-Content c.py"})\n'
                        "]);"
                    )
                },
                "result_text": (
                    "Script completed\nWall time 0.5 seconds\nOutput:\n"
                    "--- result 1 ---\n"
                    "  1: ALPHA\r\n"
                    "  2: one\r\n"
                    "  3: more\r\n"
                    "--- result 2 ---\n"
                    "  1: BETA\r\n"
                    "  2: two\r\n"
                    "  3: more\r\n"
                    "--- result 3 ---\n"
                    "  1: GAMMA\r\n"
                    "  2: three\r\n"
                    "  3: more\r\n"
                ),
            }
        ]
    )
    paths = {item.path: item for item in replay.files}
    assert set(paths) == {"a.py", "b.py", "c.py"}
    assert paths["a.py"].completeness == "PARTIAL"
    assert paths["b.py"].completeness == "PARTIAL"
    assert paths["c.py"].completeness == "PARTIAL"
    assert "ALPHA" in paths["a.py"].content
    assert "BETA" in paths["b.py"].content
    assert "GAMMA" in paths["c.py"].content
    assert "BETA" not in paths["a.py"].content


def test_promise_all_get_content_drops_rg_tail() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        "const r = await Promise.all([\n"
                        '  tools.exec_command({cmd:"Get-Content build.bat"}),\n'
                        '  tools.exec_command({cmd:"rg -n SliderInt imgui/imgui.cpp"})\n'
                        "]);"
                    )
                },
                "result_text": (
                    "Script completed\nWall time 0.7 seconds\nOutput:\n"
                    "@echo off\r\nexit /b 0\r\n"
                    "imgui/imgui.cpp:1059:bool ImGui::SliderInt(\n"
                ),
            }
        ]
    )
    assert [item.path for item in replay.files] == ["build.bat"]
    assert replay.files[0].content.startswith("@echo off")
    assert "imgui/imgui.cpp" not in replay.files[0].content


def test_named_dump_line_ranges_are_partial() -> None:
    replay = replay_from_timeline(
        [
            {
                "call_id": "c1",
                "name": "exec",
                "arguments": {
                    "input": (
                        'const r = await tools.exec_command({cmd:"'
                        "$files=@('Loader.cpp','modules/Noclip.h'); "
                        'foreach($f in $files){ Write-Output \\"--- $f ---\\" }"})'
                    )
                },
                "result_text": (
                    "Script completed\nWall time 0.7 seconds\nOutput:\n"
                    "--- Loader.cpp ---\n"
                    " 350:         AddLog(\"x\");\r\n"
                    " 351:         return false;\r\n"
                    " 352:     }\r\n"
                    "--- modules/Noclip.h ---\n"
                    "  18: static int g_savedPartCount = 0;\r\n"
                    "  19: \r\n"
                    "  20: void Update() {\r\n"
                ),
            }
        ]
    )
    assert replay.files == ()
    assert replay.unknown_mutation_barriers


def _l22_e2e3_source() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "artifacts/eligible-live/L22-e2e-3/reconstruction_source.json"
    )


@pytest.mark.skipif(not _l22_e2e3_source().is_file(), reason="L22-e2e-3 source missing")
def test_l22_e2e3_named_dumps_plant_source_files() -> None:
    source = json.loads(_l22_e2e3_source().read_text(encoding="utf-8"))
    replay = replay_from_timeline(list(source.get("tool_timeline") or []))
    paths = {item.path for item in replay.files}
    assert {
        "Injector.cpp",
        "Loader.cpp",
        "build.bat",
        "modules/API.h",
        "modules/GUI.h",
        "modules/Noclip.h",
        "modules/RobloxCore.h",
    } <= paths


def _l22_p1_e2e_source() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "artifacts/eligible-live/L22-p1-e2e/reconstruction_source.json"
    )


@pytest.mark.skipif(not _l22_p1_e2e_source().is_file(), reason="L22-p1-e2e source missing")
def test_l22_replay_plants_observed_bodies_not_listing_names() -> None:
    source = json.loads(_l22_p1_e2e_source().read_text(encoding="utf-8"))
    replay = replay_selected_environment(
        list(source.get("tool_timeline") or []),
        {str(item) for item in source.get("selected_span_ids") or []},
    )
    paths = {item.path for item in replay.files}
    assert "build.bat" in paths
    assert "Injector.cpp" in paths
    assert "RobloxDLL.cpp" not in paths
    assert "cl.exe" not in paths
    index = index_completion_holes(
        replay,
        list(source.get("tool_timeline") or []),
        {"core_objective": "read the injector"},
    )
    hole_kinds = {item["path"]: item["kind"] for item in index.holes}
    assert "roblox_injector.log" not in hole_kinds
    assert hole_kinds.get("RobloxDLL.cpp") in {None, "STUB"}



@pytest.mark.parametrize("command", [
    "cp template.py answer.py",
    "mv template.py answer.py",
    "python3 -c \"open('answer.py', 'a').write('SOLUTION')\"",
    "sed -i 's/old/SOLUTION/' answer.py",
    "Set-Content -Path answer.py -Value SOLUTION; Get-Content -Path answer.py",
])
def test_mutation_then_read_never_becomes_initial_file(command):
    replay = replay_from_timeline([
        {"call_id": "mutation", "name": "exec", "arguments": {"cmd": command}, "result_text": ""},
        {"call_id": "read", "name": "exec", "arguments": {"cmd": "cat answer.py"}, "result_text": "SOLUTION"},
    ])
    assert replay.files == ()
    assert any(item.get("reason") in {"unparsed_mutation_scope", "read_after_unparsed_mutation", "read_after_first_mutation"} for item in replay.partial_evidence)
