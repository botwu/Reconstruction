import json
import subprocess
import sys
from pathlib import Path

from traceforge.verifier.grading import (
    PytestVendorError,
    collect_test_results,
    grade,
    prepare_pytest_site,
    pytest_command,
    vendor_paths,
)


def test_failure_message_preserves_original_values(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    message = (
        "首次异常：测试替身连续超时\n"
        + "调用栈\n" * 600
        + "Authorization: Bearer sk-fixture-original-value; token=business-value"
    )
    junit.write_text(
        '<testsuite><testcase name="test_failed"><failure>'
        + message + '</failure></testcase></testsuite>', encoding="utf-8",
    )
    assert collect_test_results(junit) == [{
        "name": "test_failed", "classname": "", "status": "FAIL", "message": message,
    }]


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


def test_vendored_pytest_is_imported_from_extracted_site(tmp_path: Path) -> None:
    site = prepare_pytest_site(tmp_path / "runtime")
    script = (
        "import sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "import pytest; "
        "print(pytest.__file__)"
    )
    process = subprocess.run(
        [sys.executable, "-I", "-c", script, str(site)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert process.stdout.strip().startswith(str(site))


def test_corrupt_vendor_lock_is_infrastructure_error(tmp_path: Path) -> None:
    vendor_dir, lock_path = vendor_paths()
    root = tmp_path / "broken"
    (root / "vendor").mkdir(parents=True)
    for wheel in vendor_dir.glob("*.whl"):
        (root / "vendor" / wheel.name).write_bytes(wheel.read_bytes())
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["wheels"][0]["sha256"] = "0" * 64
    (root / "vendor_lock.json").write_text(json.dumps(lock), encoding="utf-8")
    try:
        prepare_pytest_site(tmp_path / "site", root=root)
    except PytestVendorError as exc:
        assert exc.code == "PYTEST_VENDOR_CORRUPT"
    else:
        raise AssertionError("expected PytestVendorError")
    hidden = tmp_path / "hidden.py"
    hidden.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    # grade() 使用包内 vendor，损坏副本不影响正常评分
    verdict = grade(workspace=tmp_path, tests=hidden, log_dir=tmp_path / "logs")
    assert verdict["status"] == "TASK_PASS"


def test_pytest_command_uses_isolated_vendor_runner(tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    command = pytest_command(tmp_path / "test_outputs.py", tmp_path / "junit.xml", site)
    assert command[:3] == [sys.executable, "-I", "-c"]
    assert str(site) in command
