from __future__ import annotations

import json

import pytest

from traceforge.reconstruction.exec_wrapper import ordered_parallel_exec_calls


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
