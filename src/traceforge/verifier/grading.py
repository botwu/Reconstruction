"""部署到独立 verifier 的 pytest 驱动器；从 artifact 读取最终 workspace。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


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
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    str(tests),
                    "-q",
                    "--junitxml",
                    str(xml_path),
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            (log_dir / "pytest.stdout").write_text(process.stdout, encoding="utf-8")
            (log_dir / "pytest.stderr").write_text(process.stderr, encoding="utf-8")
            verdict["exit_code"] = process.returncode
            if process.returncode not in (0, 1):
                verdict["error_code"] = "PYTEST_EXECUTION_ERROR"
            elif not xml_path.is_file():
                verdict["error_code"] = "JUNIT_MISSING"
            else:
                cases = collect_test_results(xml_path)
                verdict["tests"] = cases
                if not cases or any(x["status"] in {"ERROR", "SKIPPED"} for x in cases):
                    verdict["error_code"] = "TEST_CASES_INCOMPLETE"
                else:
                    passed = process.returncode == 0 and all(x["status"] == "PASS" for x in cases)
                    verdict["status"] = "TASK_PASS" if passed else "TASK_FAIL"
                    reward = 1.0 if passed else 0.0
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
