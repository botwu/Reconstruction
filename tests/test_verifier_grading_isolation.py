from pathlib import Path

from traceforge.verifier.grading import grade


def test_public_workspace_cannot_shadow_pytest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hidden = tmp_path / "hidden.py"
    hidden.write_text("def test_should_fail():\n    assert False\n", encoding="utf-8")
    (workspace / "pytest.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('--junitxml') + 1]).write_text("
        "'<testsuite><testcase name=\"forged\"/></testsuite>')\n",
        encoding="utf-8",
    )
    verdict = grade(workspace=workspace, tests=hidden, log_dir=tmp_path / "logs")
    assert verdict["status"] == "TASK_FAIL"
    assert verdict["reward"] == 0.0
