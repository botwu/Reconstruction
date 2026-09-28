"""生成包的隐藏面、路径和 verifier 基础设施错误验证。"""

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace

import pytest

from traceforge.verifier.bundle import compile_bundle
from traceforge.verifier.grading import collect_test_results, grade
from traceforge.verifier.synthesis import SolutionVariant, VerifierCandidate


def _verifier():
    return VerifierCandidate(
        "traceforge.verifier-candidate.v1",
        "verifier-1",
        "UNVALIDATED",
        "def test_output():\n    assert True\n",
        (
            SolutionVariant("oracle", "echo good", "独立实现"),
            SolutionVariant("alternative", "echo alternative", "另一种实现"),
        ),
        (SolutionVariant("bad", "echo wrong", "错误实现"),),
        ("test_output",),
        (),
        {"output": ("test_output",)},
        "独立计算",
        (),
        "fixture",
        "v1",
        "prompt-hash",
        "response-hash",
    )


def _verifier_with_script(script: str) -> VerifierCandidate:
    return replace(
        _verifier(),
        oracle_solutions=(SolutionVariant("fixture", script, "repository fixture"),),
    )


def _run_solution(output, workspace, runner_root):
    solution = output / "task/solution"
    script = (solution / "solve.sh").read_text(encoding="utf-8")
    script = script.replace("/home/user/workspace", workspace.as_posix())
    script = script.replace("/solution", solution.as_posix())
    runner = runner_root / "run-solve.sh"
    runner.write_text(script, encoding="utf-8")
    runner.chmod(0o755)
    subprocess.run(
        ["/bin/sh", runner.as_posix()],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


def test_bundle_keeps_solution_and_verifier_outside_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "input.txt").write_text("input")
    output = compile_bundle(
        task={"core_objective": "完成任务"},
        workspace_root=root,
        verifier=_verifier(),
        output_root=tmp_path / "out",
    )
    assert (output / "task/workspace/input.txt").read_text() == "input"
    assert not (output / "task/workspace/tests").exists()
    assert (output / "task/solution/solve.sh").is_file()
    assert (output / "task/tests/test_outputs.py").is_file()
    assert (output / "task/tests/vendor_lock.json").is_file()
    assert list((output / "task/tests/vendor").glob("pytest-*.whl"))
    assert (output / "artifact_manifest.json").is_file()
    assert (output / "task/workspace/input.txt").stat().st_mode & 0o002
    assert (output / "task/workspace").stat().st_mode & 0o002


def test_bundle_rejects_symlink(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "link").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="符号链接"):
        compile_bundle(
            task={"core_objective": "完成任务"},
            workspace_root=root,
            verifier=_verifier(),
            output_root=tmp_path / "out",
        )


def test_missing_workspace_is_infrastructure_error(tmp_path):
    logs = tmp_path / "logs"
    result = grade(workspace=tmp_path / "missing", tests=tmp_path / "tests.py", log_dir=logs)
    assert result["status"] == "INFRA_ERROR"
    assert result["reward"] is None
    assert not (logs / "reward.json").exists()


def test_junit_keeps_collection_errors_distinct(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuites><testsuite><testcase name="test_a"><failure message="bad">assert x</failure></testcase><testcase name="test_b"><error/></testcase></testsuite></testsuites>'  # noqa: E501
    )
    rows = collect_test_results(path)
    assert [x["status"] for x in rows] == ["FAIL", "ERROR"]
    assert rows[0]["message"] == "assert x"


def test_bundle_wraps_direct_python_solution(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    verifier = _verifier()
    verifier = VerifierCandidate(
        verifier.schema_version,
        verifier.candidate_id,
        verifier.status,
        verifier.test_outputs_py,
        (
            SolutionVariant(
                "python",
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "Path('done.txt').write_text('ok')",
                "Python fixture",
            ),
        ),
        verifier.mutation_solutions,
        verifier.missing_capability_tests,
        verifier.protective_tests,
        verifier.obligation_coverage,
        verifier.expected_value_strategy,
        verifier.open_questions,
        verifier.model,
        verifier.prompt_version,
        verifier.prompt_sha256,
        verifier.response_sha256,
    )
    output = compile_bundle(
        task={"core_objective": "????????????"},
        workspace_root=root,
        verifier=verifier,
        output_root=tmp_path / "out",
    )
    solve = (output / "task/solution/solve.sh").read_text()
    assert "exec python3 /solution/solve.py" in solve
    assert (output / "task/solution/solve.py").read_text().startswith("#!/usr/bin/env python3")


def test_bundle_executes_direct_python_fixture(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = "value = 'python'\nfrom pathlib import Path\nPath('done.txt').write_text(value)"
    output = compile_bundle(
        task={"core_objective": "fixture"},
        workspace_root=workspace,
        verifier=_verifier_with_script(script),
        output_root=tmp_path / "out",
    )
    assert (output / "task/solution/solve.py").is_file()
    _run_solution(output, workspace, tmp_path)
    assert (workspace / "done.txt").read_text() == "python"


def test_bundle_keeps_shell_heredoc_as_shell_fixture(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "Path('done.txt').write_text('heredoc')\n"
        "PY"
    )
    output = compile_bundle(
        task={"core_objective": "fixture"},
        workspace_root=workspace,
        verifier=_verifier_with_script(script),
        output_root=tmp_path / "out",
    )
    assert not (output / "task/solution/solve.py").exists()
    _run_solution(output, workspace, tmp_path)
    assert (workspace / "done.txt").read_text() == "heredoc"


def test_bundle_digest_includes_compiler_contract(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = compile_bundle(
        task={"core_objective": "fixture"},
        workspace_root=workspace,
        verifier=_verifier_with_script("echo fixture"),
        output_root=tmp_path / "out",
    )
    manifest = __import__("json").loads(
        (output / "compile_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["compiler_version"] == "traceforge.bundle-compiler.v5-task-acceptance"
    assert manifest["entrypoint_contract"] == {
        "workspace_mount": "/home/user/workspace",
        "solution_mount": "/solution",
        "shell_entrypoint": "solve.sh",
        "python_entrypoint": "solve.py",
    }


def test_solution_receives_workspace_environment(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = (
        "import os\nfrom pathlib import Path\n"
        "(Path(os.environ['TRACEFORGE_WORKSPACE']) / 'done.txt').write_text('ok')"
    )
    output = compile_bundle(
        task={"core_objective": "fixture"}, workspace_root=workspace,
        verifier=_verifier_with_script(script), output_root=tmp_path / "out",
    )
    _run_solution(output, workspace, tmp_path)
    assert (workspace / "done.txt").read_text() == "ok"


def test_bundle_permissions_preserve_source_and_executable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o750)
    source = workspace / "run.sh"
    source.write_text("#!/bin/sh\necho ok\n")
    source.chmod(0o750)
    output = compile_bundle(
        task={"core_objective": "fixture"}, workspace_root=workspace,
        verifier=_verifier(), output_root=tmp_path / "out",
    )
    assert source.stat().st_mode & 0o777 == 0o750
    assert workspace.stat().st_mode & 0o777 == 0o750
    assert (output / "task/workspace/run.sh").stat().st_mode & 0o777 == 0o776


def test_bundle_carries_hidden_portable_task_acceptance(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("input")
    acceptance = {
        "task_id": "task-response",
        "acceptance_obligations": [
            {"id": "reply", "text": "返回 acceptance-report", "observable": "最终回复"}
        ],
        "environment_bindings": [{"obligation_id": "reply", "verifier_kind": "NON_FILE"}],
        "response_contract": {
            "schema_version": "traceforge.response-contract.v1",
            "checks": [{"obligation_id": "reply", "kind": "acceptance_report",
                        "required_fields": {"summary": "string"}}],
        },
    }
    output = compile_bundle(
        task={"core_objective": "完成任务", **acceptance},
        workspace_root=workspace, verifier=_verifier(), output_root=tmp_path / "out",
    )
    copied = tmp_path / "portable"
    shutil.copytree(output, copied)
    relative = "task/tests/control/input-manifest.json"
    manifest = json.loads((copied / relative).read_text())
    assert manifest["task_acceptance"] == acceptance
    assert [p.name for p in (copied / "task/workspace").iterdir()] == ["input.txt"]
    assert "response_contract" not in (copied / "task/instruction.md").read_text()
    artifacts = json.loads((copied / "artifact_manifest.json").read_text())
    assert artifacts["bundle_file_sha256"][relative] == hashlib.sha256(
        (copied / relative).read_bytes()
    ).hexdigest()


def test_response_contract_changes_bundle_digest_without_public_instruction_change(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = {
        "core_objective": "完成任务",
        "response_contract": {"schema_version": "traceforge.response-contract.v1",
                              "checks": [{"kind": "acceptance_report", "obligation_id": "reply",
                                          "required_fields": {"summary": "string"}}]},
    }
    first = compile_bundle(task=task, workspace_root=workspace, verifier=_verifier(),
                           output_root=tmp_path / "out")
    task["response_contract"]["checks"][0]["required_fields"]["status"] = "string"
    second = compile_bundle(task=task, workspace_root=workspace, verifier=_verifier(),
                            output_root=tmp_path / "out")
    assert first.name != second.name
    assert (first / "task/instruction.md").read_bytes() == (second / "task/instruction.md").read_bytes()
    before = json.loads((first / "task/tests/control/input-manifest.json").read_text())
    after = json.loads((second / "task/tests/control/input-manifest.json").read_text())
    assert before["task_acceptance"]["response_contract"] != after["task_acceptance"]["response_contract"]


def test_bundle_keeps_missing_response_contract_explicit(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = compile_bundle(task={"core_objective": "完成任务"}, workspace_root=workspace,
                            verifier=_verifier(), output_root=tmp_path / "out")
    manifest = json.loads((output / "task/tests/control/input-manifest.json").read_text())
    assert manifest["task_acceptance"] == {
        "task_id": None, "acceptance_obligations": [], "environment_bindings": [],
        "response_contract": None,
    }
