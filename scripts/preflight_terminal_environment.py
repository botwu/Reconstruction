#!/usr/bin/env python3
"""区分本地静态预检与真实 AGS pytest 冒烟；两者均不代表任务端到端通过。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents.roles import VERIFIER_ROLE
from traceforge.reconstruction.agents.runtime import HermesUnavailableError, resolve_hermes_home
from traceforge.reconstruction.agents.sandbox import (
    prepare_role_sandbox,
    run_coro,
    sandbox_run_pytest,
    sandbox_write_test,
)
from traceforge.reconstruction.agents.session import AgentSession
from traceforge.reconstruction.container_verification import (
    ContainerRuntime,
    build_ags_runtime_factory,
    resolve_sandbox_api_key,
)
from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    load_channel_connection,
    load_e2b_api_key,
)
from traceforge.reconstruction.run_config import load_screening_limits, resolve_role_matrix
from traceforge.verifier.grading import PytestVendorError, prepare_pytest_site

_SMOKE_TESTS = """import os
from pathlib import Path

def test_preflight_pass():
    root = Path(os.environ["TRACEFORGE_WORKSPACE"])
    assert (root / "preflight.txt").read_text() == "traceforge-preflight\\n"

def test_preflight_fail():
    root = Path(os.environ["TRACEFORGE_WORKSPACE"])
    assert (root / "preflight.txt").read_text() == "intentional-mismatch"
"""


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _pytest_version() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, type(exc).__name__
    # 失败的子进程输出可能包含环境配置，不把它复制到报告。
    return result.returncode == 0, "当前解释器可调用 pytest" if result.returncode == 0 else (
        f"pytest 版本探针退出码：{result.returncode}"
    )


def run_sandbox_smoke(
    runtime_factory: Callable[[], ContainerRuntime], output_root: Path
) -> dict[str, Any]:
    """使用真实 verifier 初始化及执行接口测试 PASS、FAIL、输入不变和清理。"""
    runtime: ContainerRuntime | None = None
    session = AgentSession(allow_tests=True)
    result: dict[str, Any] = {
        "status": "FAIL",
        "checks": [],
        "cleanup_confirmed": False,
        "model_calls": False,
    }
    checks = result["checks"]
    try:
        output_root.mkdir(parents=True, exist_ok=False)
        workspace = output_root / "workspace"
        workspace.mkdir()
        (workspace / "preflight.txt").write_text("traceforge-preflight\n", encoding="utf-8")
        session.workspace = workspace
        runtime = runtime_factory()
        binding = run_coro(
            prepare_role_sandbox(
                role=VERIFIER_ROLE,
                runtime=runtime,
                session=session,
                staging_root=output_root / "verifier",
            )
        )
        checks.append(
            _check(
                "verifier_setup",
                not session.policy_errors,
                (",".join(session.policy_errors) or "已上传工作区和离线 pytest"),
            )
        )
        if session.policy_errors:
            return result
        wrote = sandbox_write_test(binding, _SMOKE_TESTS)
        checks.append(
            _check(
                "hidden_test_upload",
                wrote == "wrote tests/test_outputs.py",
                (
                    "隐藏测试已上传"
                    if wrote == "wrote tests/test_outputs.py"
                    else "隐藏测试上传失败"
                ),
            )
        )
        if wrote != "wrote tests/test_outputs.py":
            return result
        runs = sandbox_run_pytest(
            binding,
            ("test_preflight_pass", "test_preflight_fail"),
            test_sha256=hashlib.sha256(_SMOKE_TESTS.encode()).hexdigest(),
        )
        # 不保存远程 stdout/stderr；只保存可定位的结构化证据。
        result["runs"] = [
            {
                key: run.get(key)
                for key in (
                    "name",
                    "status",
                    "error_code",
                    "test_sha256",
                    "input_sha256",
                    "input_unchanged",
                )
            }
            for run in runs
        ]
        statuses = [(run.get("name"), run.get("status")) for run in runs]
        expected = [
            ("test_preflight_pass", "PASS"),
            ("test_preflight_fail", "FAIL"),
        ]
        checks.append(
            _check(
                "pytest_pass_and_fail",
                statuses == expected,
                ("实际结果：" + json.dumps(statuses, ensure_ascii=False)),
            )
        )
        unchanged = len(runs) == 2 and all(
            run.get("input_unchanged") is True
            and isinstance(run.get("input_sha256"), str)
            and len(run["input_sha256"]) == 64
            for run in runs
        )
        checks.append(_check("workspace_unchanged", unchanged, "执行前后工作区摘要一致"))
    except Exception as exc:
        # 网络/沙盒边界的异常必须归档为失败，但异常消息可能含凭据。
        checks.append(_check("sandbox_exception", False, type(exc).__name__))
    finally:
        if runtime is not None:
            try:
                run_coro(runtime.stop(delete=True))
                result["cleanup_confirmed"] = True
            except Exception as exc:
                checks.append(_check("sandbox_cleanup", False, type(exc).__name__))
        result["status"] = (
            "PASS"
            if checks and all(item["ok"] for item in checks) and result["cleanup_confirmed"]
            else "FAIL"
        )
    return result


def build_report(
    config: Path,
    harbor_root: Path,
    hermes_home: Path | None,
    *,
    sandbox_output: Path | None = None,
    runtime_factory: Callable[[], ContainerRuntime] | None = None,
) -> dict[str, Any]:
    """默认仅检查本地配置；显式 sandbox_output 才创建远程沙盒。"""
    checks: list[dict[str, Any]] = []
    matrix = {}
    limits: tuple[int, int, int] | None = None
    try:
        matrix = resolve_role_matrix(config)
        limits = load_screening_limits(config)
        checks.append(_check("role_config", True, "四个角色与筛选预算已解析"))
    except (ModelGatewayError, OSError, ValueError) as exc:
        checks.append(_check("role_config", False, type(exc).__name__))
    for role, settings in matrix.items():
        try:
            load_channel_connection(config, settings.channel)
            checks.append(_check(f"channel:{role}", True, settings.channel))
        except (ModelGatewayError, OSError, ValueError) as exc:
            checks.append(_check(f"channel:{role}", False, type(exc).__name__))
    try:
        e2b = bool(resolve_sandbox_api_key() or load_e2b_api_key(config))
        checks.append(_check("sandbox_key", e2b, "已配置" if e2b else "未配置"))
    except (ModelGatewayError, OSError, ValueError) as exc:
        checks.append(_check("sandbox_key", False, type(exc).__name__))
    try:
        home = resolve_hermes_home(hermes_home)
        checks.append(_check("hermes_home", True, str(home)))
    except (HermesUnavailableError, OSError, ValueError) as exc:
        checks.append(_check("hermes_home", False, type(exc).__name__))
    for name in ("hermes-batch.yaml", "oracle.yaml", "nop.yaml"):
        path = harbor_root / "configs" / name
        checks.append(_check(f"harbor_config:{name}", path.is_file(), str(path)))
    harbor_command = Path(shutil.which("harbor") or harbor_root / ".venv/bin/harbor")
    checks.append(
        _check(
            "harbor_command",
            harbor_command.is_file() and os.access(harbor_command, os.X_OK),
            str(harbor_command),
        )
    )
    pytest_ok, pytest_detail = _pytest_version()
    checks.append(_check("host_pytest", pytest_ok, pytest_detail))
    try:
        with tempfile.TemporaryDirectory(prefix="traceforge-pytest-preflight-") as temp:
            prepare_pytest_site(Path(temp))
        checks.append(_check("pytest_vendor", True, "锁定 wheel 完整且 SHA-256 匹配"))
    except (PytestVendorError, OSError, ValueError) as exc:
        checks.append(_check("pytest_vendor", False, type(exc).__name__))
    static_ok = all(item["ok"] for item in checks)
    smoke: dict[str, Any] = {"status": "UNCHECKED", "reason": "未指定 --sandbox-output"}
    if sandbox_output is not None:
        if static_ok:
            try:
                factory = runtime_factory or build_ags_runtime_factory(
                    harbor_root=harbor_root,
                    output_root=sandbox_output / "ags",
                    config_path=config,
                )
                smoke = run_sandbox_smoke(factory, sandbox_output)
            except Exception as exc:
                smoke = {"status": "FAIL", "error_type": type(exc).__name__}
        else:
            smoke = {"status": "NOT_RUN", "reason": "本地静态预检失败"}
    status = "STATIC_PASS" if static_ok else "REVIEW"
    if sandbox_output is not None:
        status = "SANDBOX_SMOKE_PASS" if static_ok and smoke["status"] == "PASS" else "REVIEW"
    unchecked = ["model_api_connectivity", "harbor_red_calibration", "hermes_rollout"]
    if smoke["status"] == "UNCHECKED":
        unchecked.insert(0, "sandbox_creation_pytest_and_cleanup")
    return {
        "schema_version": "traceforge.environment-preflight.v2",
        "status": status,
        "scope": "LOCAL_STATIC_AND_SANDBOX_SMOKE" if sandbox_output else "LOCAL_STATIC_ONLY",
        "static_checks": checks,
        "sandbox_smoke": smoke,
        "unchecked_probes": unchecked,
        "end_to_end_verified": False,
        "roles": {name: settings.public() for name, settings in matrix.items()},
        "screening_limits": dict(
            zip(
                ("max_input_chars", "max_messages_for_triage", "max_source_requests_for_triage"),
                limits,
                strict=True,
            )
        )
        if limits
        else None,
        "policy": {
            "source_read_only": True,
            "temporary_vendor_expansion": True,
            "remote_sandbox_requested": sandbox_output is not None,
            "secrets_persisted": False,
            "model_calls": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--harbor-root", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--sandbox-output",
        type=Path,
        default=None,
        help="显式创建 AGS 沙盒做 pytest 冒烟；提供新的产物目录",
    )
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error(f"拒绝覆盖已有报告：{args.output}")
    if args.sandbox_output and args.sandbox_output.exists():
        parser.error(f"拒绝复用已有冒烟目录：{args.sandbox_output}")
    report = build_report(
        args.config, args.harbor_root, args.hermes_home, sandbox_output=args.sandbox_output
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] in {"STATIC_PASS", "SANDBOX_SMOKE_PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
