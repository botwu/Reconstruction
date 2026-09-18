"""部署到独立 verifier 的 pytest 驱动器；从 artifact 读取最终 workspace。

AGS 预置模板没有 pytest，且 verifier 无网。grader 与 Bundle 自带锁定 wheel，
在 log 目录离线展开后用 ``python -I`` 启动，避免 Agent workspace 劫持 pytest。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

VENDOR_LOCK_SCHEMA = "traceforge.verifier-pytest-vendor/v1"
_PYTEST_RUNNER = (
    "import sys; "
    "sys.path.insert(0, sys.argv[1]); "
    "import pytest; "
    "raise SystemExit(pytest.main(sys.argv[2:]))"
)


class PytestVendorError(RuntimeError):
    """离线 pytest 运行时损坏或缺失。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def collect_test_results(path: Path) -> list[dict[str, str]]:
    """把 pytest JUnit 结果归一化，测试收集错误不可当成任务失败。"""
    tree = ET.parse(path)
    output = []
    for case in tree.iter("testcase"):
        status = "PASS"
        if case.find("error") is not None:
            status = "ERROR"
        elif case.find("failure") is not None:
            status = "FAIL"
        elif case.find("skipped") is not None:
            status = "SKIPPED"
        output.append(
            {"name": case.get("name", ""), "classname": case.get("classname", ""), "status": status}
        )
    return output


def vendor_paths(root: Path | None = None) -> tuple[Path, Path]:
    """返回 (vendor 目录, lock 文件)；默认与本文件同级，便于拷进 /tests。"""
    here = (root or Path(__file__).resolve().parent)
    return here / "vendor", here / "vendor_lock.json"


def prepare_pytest_site(destination: Path, *, root: Path | None = None) -> Path:
    """校验 SHA-256 后把锁定 wheel 展开到 destination/site-packages。"""
    vendor_dir, lock_path = vendor_paths(root)
    if not vendor_dir.is_dir() or not lock_path.is_file():
        raise PytestVendorError("PYTEST_VENDOR_MISSING", "缺少离线 pytest vendor")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PytestVendorError("PYTEST_VENDOR_CORRUPT", "vendor_lock.json 无法读取") from exc
    if not isinstance(lock, dict) or lock.get("schema_version") != VENDOR_LOCK_SCHEMA:
        raise PytestVendorError("PYTEST_VENDOR_CORRUPT", "vendor_lock schema 不匹配")
    wheels = lock.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        raise PytestVendorError("PYTEST_VENDOR_CORRUPT", "vendor_lock 没有 wheels")
    site = destination / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for item in wheels:
        if not isinstance(item, dict):
            raise PytestVendorError("PYTEST_VENDOR_CORRUPT", "vendor_lock wheel 条目非法")
        name = item.get("wheel")
        expected = item.get("sha256")
        if not isinstance(name, str) or not isinstance(expected, str):
            raise PytestVendorError("PYTEST_VENDOR_CORRUPT", "vendor_lock wheel 字段缺失")
        wheel = vendor_dir / name
        if not wheel.is_file():
            raise PytestVendorError("PYTEST_VENDOR_MISSING", f"缺少 wheel：{name}")
        observed = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if observed != expected:
            raise PytestVendorError(
                "PYTEST_VENDOR_CORRUPT",
                f"wheel 哈希不匹配：{name}",
            )
        try:
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(site)
        except zipfile.BadZipFile as exc:
            raise PytestVendorError("PYTEST_VENDOR_CORRUPT", f"wheel 无法展开：{name}") from exc
    return site


def pytest_command(tests: Path, xml_path: Path, site: Path) -> list[str]:
    """``python -I -c`` 注入 vendor，不使用 PYTHONPATH，也不把 workspace 放进 sys.path。"""
    return [
        sys.executable,
        "-I",
        "-c",
        _PYTEST_RUNNER,
        str(site),
        str(tests),
        "-q",
        f"--junitxml={xml_path}",
        "-p",
        "no:cacheprovider",
    ]


def grade(
    *,
    workspace: Path,
    tests: Path,
    log_dir: Path,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    """运行隐藏测试并同时发布 reward 与 verdict，不把基础设施错误折叠成 FAIL。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    verdict: dict[str, Any] = {
        "schema_version": "traceforge.pytest-verdict.v1",
        "status": "INFRA_ERROR",
        "tests": [],
        "error_code": None,
    }
    reward: float | None = None
    if not workspace.is_dir():
        verdict["error_code"] = "FINAL_WORKSPACE_MISSING"
    elif not tests.is_file():
        verdict["error_code"] = "HIDDEN_TESTS_MISSING"
    else:
        env = dict(os.environ)
        env.update(
            {
                "TRACEFORGE_WORKSPACE": str(workspace),
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        xml_path = log_dir / "junit.xml"
        try:
            site = prepare_pytest_site(log_dir / "_pytest_runtime")
            process = subprocess.run(
                pytest_command(tests, xml_path, site),
                # Never put the untrusted Agent workspace on sys.path[0].
                cwd=log_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            (log_dir / "pytest.stdout").write_text(process.stdout, encoding="utf-8")
            (log_dir / "pytest.stderr").write_text(process.stderr, encoding="utf-8")
            verdict["exit_code"] = process.returncode
            if not xml_path.is_file():
                verdict["error_code"] = "PYTEST_EXECUTION_ERROR"
            elif process.returncode not in (0, 1):
                verdict["error_code"] = "PYTEST_EXECUTION_ERROR"
            else:
                cases = collect_test_results(xml_path)
                verdict["tests"] = cases
                if not cases or any(x["status"] in {"ERROR", "SKIPPED"} for x in cases):
                    verdict["error_code"] = "TEST_CASES_INCOMPLETE"
                else:
                    passed = process.returncode == 0 and all(x["status"] == "PASS" for x in cases)
                    verdict["status"] = "TASK_PASS" if passed else "TASK_FAIL"
                    reward = 1.0 if passed else 0.0
        except PytestVendorError as exc:
            verdict["error_code"] = exc.code
        except subprocess.TimeoutExpired:
            verdict["error_code"] = "VERIFIER_TIMEOUT"
        except (OSError, ET.ParseError):
            verdict["error_code"] = "VERIFIER_IO_ERROR"
    verdict["reward"] = reward
    (log_dir / "verdict.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # Harbor 的 reward 通道无法表达未知；异常不写 reward 文件，结果读取器据 verdict 区分。
    reward_file = log_dir / "reward.json"
    if reward is not None:
        reward_file.write_text(json.dumps({"task": reward}) + "\n", encoding="utf-8")
    elif reward_file.exists():
        reward_file.unlink()
    return verdict


if __name__ == "__main__":
    result = grade(
        workspace=Path("/logs/artifacts/traceforge/workspace"),
        tests=Path("/tests/test_outputs.py"),
        log_dir=Path("/logs/verifier"),
    )
    raise SystemExit(2 if result["status"] == "INFRA_ERROR" else 0)
