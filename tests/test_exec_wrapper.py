from __future__ import annotations

import json

import pytest

from traceforge.reconstruction.exec_wrapper import (
    ordered_parallel_exec_calls,
    parallel_exec_prints_stdout,
)


def _wrapper(calls: str, *, batch: str = "results", item: str = "r") -> str:
    return (
        f"const {batch} = await Promise.all([{calls}]);\n"
        f"for (const {item} of {batch}) text({item});"
    )


def test_preserves_order_literal_values_and_nested_execution_context() -> None:
    command = r"Get-Content -LiteralPath tests\test_excel.py -Encoding UTF8"
    source = _wrapper(
        "tools.exec_command({cmd: " + json.dumps(command)
        + ', workdir: "F:\\\\Project\\\\example", shell: "powershell", login: false, '
        'max_output_tokens: 14000}),\n'
        'tools.shell_command({"command": "cat package.json", "workdir": "/repo"})',
        batch="outputs",
        item="output",
    )
    assert ordered_parallel_exec_calls(source) == [
        {
            "cmd": command,
            "workdir": r"F:\Project\example",
            "shell": "powershell",
            "login": False,
            "max_output_tokens": 14000,
        },
        {"command": "cat package.json", "workdir": "/repo"},
    ]


@pytest.mark.parametrize("calls", [
    'tools.exec_command({cmd: command})',
    'tools.exec_command({cmd: ' + chr(96) + 'cat $' + '{path}' + chr(96) + '})',
    "tools.exec_command({cmd: 'cat a.py'})",
    'tools.exec_command({cmd: "cat a.py", cmd: "cat b.py"})',
    'tools.exec_command({cmd: "cat a.py", "cmd": "cat b.py"})',
    'tools.exec_command({cmd: "cat a.py", __proto__: null})',
    'tools.exec_command({...options, cmd: "cat a.py"})',
    'tools.exec_command({cmd: "cat a.py", get workdir() { return "/repo"; }})',
    'tools.exec_command({cmd: "cat a.py", max_output_tokens: NaN})',
    'tools.exec_command({cmd: "cat a.py", workdir: resolvePath()})',
    'tools.exec_command({cmd: "cat a.py"}), runSomething()',
    'tools.exec_command({cmd: "cat a.py"}).then(text)',
])
def test_rejects_dynamic_or_non_literal_calls(calls: str) -> None:
    assert ordered_parallel_exec_calls(_wrapper(calls)) is None


@pytest.mark.parametrize("source", [
    'const results = await Promise.all([tools.exec_command({cmd: "cat a.py"})]);'
    'for (const r of results.reverse()) text(r);',
    'const results = await Promise.all([tools.exec_command({cmd: "cat a.py"})]);'
    'results.sort(); for (const r of results) text(r);',
    _wrapper('tools.exec_command({cmd: "cat a.py"})') + ' tools.exec_command({cmd: "touch x"});',
    'text("extra"); ' + _wrapper('tools.exec_command({cmd: "cat a.py"})'),
    _wrapper('tools.exec_command({cmd: "cat a.py"})').replace("text(r)", "text(results[0])"),
    _wrapper('tools.exec_command({cmd: "cat a.py"})', batch="text"),
    _wrapper('tools.exec_command({cmd: "cat a.py"})', item="text"),
    _wrapper('tools.exec_command({cmd: "cat a.py"})', item="results"),
])
def test_rejects_reordering_extra_emissions_and_shadowed_bindings(source: str) -> None:
    assert ordered_parallel_exec_calls(source) is None


def test_json_escaped_strings_are_not_interpreted_as_wrapper_syntax() -> None:
    command = 'printf "\\\\n"; echo "]); for (const r of results) text(r);"'
    assert ordered_parallel_exec_calls(
        _wrapper("tools.exec_command({cmd:" + json.dumps(command) + "})")
    ) == [{"cmd": command}]


@pytest.mark.parametrize("arguments", [
    '{cmd: "cat a.py", command: "cat b.py"}',
    '{cmd: "cat a.py", command: "cat a.py"}',
    '{cmd: ""}',
    '{command: ""}',
    '{cmd: "   "}',
    '{command: "   "}',
])
def test_rejects_ambiguous_or_empty_commands(arguments: str) -> None:
    assert ordered_parallel_exec_calls(
        _wrapper("tools.exec_command(" + arguments + ")")
    ) is None


@pytest.mark.parametrize(("printed", "stdout"), [
    ("r", False),
    ("r.output", True),
    ("JSON.stringify({exit_code:r.exit_code, output:r.output})", False),
])
def test_fixed_output_projections_preserve_call_arguments(printed: str, stdout: bool) -> None:
    source = _wrapper('tools.exec_command({cmd: "cat app.py", workdir: "/repo"})')
    source = source.replace("text(r)", f"text({printed})")
    assert ordered_parallel_exec_calls(source) == [{"cmd": "cat app.py", "workdir": "/repo"}]
    assert parallel_exec_prints_stdout(source) is stdout


@pytest.mark.parametrize("printed", [
    "r.output.trim()",
    'r["output"]',
    "other.output",
    "JSON.stringify(r)",
    "JSON.stringify({exit_code:r.output, output:r.exit_code})",
    "JSON.stringify({exit_code:r.exit_code, output:other.output})",
    "JSON.stringify({exit_code:r.exit_code, output:r.output, extra:1})",
])
def test_rejects_unproven_output_transformations(printed: str) -> None:
    source = _wrapper('tools.exec_command({cmd: "cat app.py"})')
    source = source.replace("text(r)", f"text({printed})")
    assert ordered_parallel_exec_calls(source) is None
    assert parallel_exec_prints_stdout(source) is False


@pytest.mark.parametrize(("batch", "item"), [("JSON", "r"), ("results", "JSON")])
def test_json_projection_cannot_shadow_json_binding(batch: str, item: str) -> None:
    source = _wrapper('tools.exec_command({cmd: "cat app.py"})', batch=batch, item=item)
    source = source.replace(
        f"text({item})",
        f"text(JSON.stringify({{exit_code:{item}.exit_code, output:{item}.output}}))",
    )
    assert ordered_parallel_exec_calls(source) is None


def test_stdout_mode_requires_valid_literal_call_arguments() -> None:
    source = _wrapper('tools.exec_command({cmd: command})').replace("text(r)", "text(r.output)")
    assert parallel_exec_prints_stdout(source) is False
