"""生成包的隐藏面、路径和 verifier 基础设施错误验证。"""

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
        '<testsuites><testsuite><testcase name="test_a"><failure/></testcase><testcase name="test_b"><error/></testcase></testsuite></testsuites>'  # noqa: E501
    )
    assert [x["status"] for x in collect_test_results(path)] == ["FAIL", "ERROR"]
