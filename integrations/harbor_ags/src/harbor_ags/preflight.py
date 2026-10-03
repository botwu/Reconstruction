"""Harbor × AGS × Hermes 的版本和真实连通性预检。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import io
import json
import os
import shlex
import sys
import zipfile
from dataclasses import dataclass, field
from email.parser import Parser
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .agent import (
    _ANTHROPIC_VERSION,
    _ANTHROPIC_WHEEL,
    _ANTHROPIC_WHEEL_SHA256,
    _DOCSTRING_PARSER_VERSION,
    _DOCSTRING_PARSER_WHEEL,
    _DOCSTRING_PARSER_WHEEL_SHA256,
    _REMOTE_SITE_PACKAGES,
    _REMOTE_VENDOR_DIR,
    _VENDORED_WHEELS,
)

EXPECTED_PYTHON = (3, 12)
EXPECTED_PACKAGES = {
    "harbor": "0.22.0",
    "e2b": "2.10.2",
    "e2b-code-interpreter": "2.4.1",
}
VERSION_LOCK_PATH = Path(__file__).resolve().parents[2] / "version-lock.json"

_VENDORED_PACKAGE_VERSIONS = {
    _ANTHROPIC_WHEEL: ("anthropic", _ANTHROPIC_VERSION),
    _DOCSTRING_PARSER_WHEEL: ("docstring-parser", _DOCSTRING_PARSER_VERSION),
}


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


@dataclass
class PreflightReport:
    checks: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "harbor-ags-preflight/v1",
            "ok": self.ok,
            "checks": self.checks,
            "errors": self.errors,
        }


def _expected_anthropic_lock() -> dict[str, Any]:
    """根据 Agent 运行时常量生成 version-lock 的唯一预期结构。"""

    return {
        "version": _ANTHROPIC_VERSION,
        "wheel": _ANTHROPIC_WHEEL,
        "wheel_sha256": _ANTHROPIC_WHEEL_SHA256,
        "vendored_dependencies": [
            {
                "name": "docstring-parser",
                "version": _DOCSTRING_PARSER_VERSION,
                "wheel": _DOCSTRING_PARSER_WHEEL,
                "wheel_sha256": _DOCSTRING_PARSER_WHEEL_SHA256,
            }
        ],
    }


def _check_vendored_wheels(report: PreflightReport, version_lock_path: Path) -> None:
    """校验 version-lock 与包内 wheel，且把实际摘要写入报告。"""

    check: dict[str, Any] = {
        "version_lock": str(version_lock_path),
        "version": _ANTHROPIC_VERSION,
        "wheels": [],
    }
    report.checks["anthropic_sdk_lock"] = check
    try:
        lock = json.loads(version_lock_path.read_text(encoding="utf-8"))
        observed_lock = lock["hermes"]["anthropic_sdk"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        check["version_lock_matches"] = False
        report.errors.append(f"Anthropic version-lock 无法读取：{type(exc).__name__}")
        observed_lock = None

    expected_lock = _expected_anthropic_lock()
    lock_matches = observed_lock == expected_lock
    check["version_lock_matches"] = lock_matches
    if observed_lock is not None and not lock_matches:
        report.errors.append("Anthropic version-lock 与 Agent 运行时常量不匹配")

    for filename, expected_hash, _remote_path in _VENDORED_WHEELS:
        package_name, version = _VENDORED_PACKAGE_VERSIONS[filename]
        wheel_check: dict[str, Any] = {
            "name": package_name,
            "version": version,
            "wheel": filename,
            "expected_sha256": expected_hash,
            "present": False,
            "sha256": None,
            "verified": False,
            "zip_verified": False,
            "metadata_name": None,
            "metadata_version": None,
        }
        check["wheels"].append(wheel_check)
        resource = files("harbor_ags.resources").joinpath(filename)
        try:
            payload = resource.read_bytes()
        except (FileNotFoundError, OSError) as exc:
            report.errors.append(f"缺少 vendored wheel {filename}：{type(exc).__name__}")
            continue
        observed_hash = hashlib.sha256(payload).hexdigest()
        wheel_check.update(
            {
                "present": True,
                "sha256": observed_hash,
                "verified": observed_hash == expected_hash,
            }
        )
        if observed_hash != expected_hash:
            report.errors.append(
                f"vendored wheel 哈希不匹配 ({filename})："
                f"expected={expected_hash} observed={observed_hash}"
            )
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                bad_member = archive.testzip()
                metadata_members = [
                    name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
                ]
                if bad_member is not None or len(metadata_members) != 1:
                    raise ValueError("wheel ZIP 或 METADATA 结构无效")
                metadata = Parser().parsestr(archive.read(metadata_members[0]).decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as exc:
            report.errors.append(f"vendored wheel 内容无效 ({filename})：{type(exc).__name__}")
            continue
        metadata_name = (metadata.get("Name") or "").lower().replace("_", "-")
        metadata_version = metadata.get("Version")
        wheel_check.update(
            {
                "zip_verified": True,
                "metadata_name": metadata_name,
                "metadata_version": metadata_version,
            }
        )
        if metadata_name != package_name or metadata_version != version:
            report.errors.append(
                f"vendored wheel METADATA 不匹配 ({filename})："
                f"name={metadata_name or None} version={metadata_version or None}"
            )


def _wheel_extract_command() -> str:
    """远端二次校验 SHA 后，以 zipfile 离线展开两份 wheel。"""

    commands = ["set -eu"]
    for _filename, expected_hash, remote_path in _VENDORED_WHEELS:
        commands.append(
            f"test \"$(sha256sum {shlex.quote(remote_path)} | cut -d' ' -f1)\" = "
            f"{shlex.quote(expected_hash)}"
        )
    commands.extend(
        [
            f"test ! -e {shlex.quote(_REMOTE_SITE_PACKAGES)}",
            f"mkdir -p {shlex.quote(_REMOTE_SITE_PACKAGES)}",
        ]
    )
    for _filename, _expected_hash, remote_path in _VENDORED_WHEELS:
        commands.append(
            f"python3 -m zipfile -e {shlex.quote(remote_path)} {shlex.quote(_REMOTE_SITE_PACKAGES)}"
        )
    return "; ".join(commands)


def _anthropic_runtime_probe_command() -> str:
    """生成与 LosslessHermesAgent 相同的 SDK import/构造探针。"""

    wheel_rows = []
    for filename, _expected_hash, remote_path in _VENDORED_WHEELS:
        package_name, version = _VENDORED_PACKAGE_VERSIONS[filename]
        wheel_rows.append(
            "{"
            + ",".join(
                (
                    f"'name':{package_name!r}",
                    f"'version':{version!r}",
                    f"'wheel':{filename!r}",
                    f"'sha256':hashlib.sha256(Path({remote_path!r}).read_bytes()).hexdigest()",
                )
            )
            + "}"
        )

    vendor_prefix = repr(_REMOTE_SITE_PACKAGES + "/")
    python_probe = "\n".join(
        [
            "import hashlib",
            "import importlib.metadata as m",
            "import json",
            "from pathlib import Path",
            "import anthropic",
            "import docstring_parser",
            "from run_agent import AIAgent",
            "from agent.anthropic_adapter import build_anthropic_client",
            f"assert m.version('anthropic') == {_ANTHROPIC_VERSION!r}",
            f"assert m.version('docstring-parser') == {_DOCSTRING_PARSER_VERSION!r}",
            "module_from_vendor = all(("
            f"str(anthropic.__file__).startswith({vendor_prefix}),"
            f"str(docstring_parser.__file__).startswith({vendor_prefix}),"
            "))",
            "assert module_from_vendor",
            "client = build_anthropic_client(",
            "    api_key='preflight-dummy-not-secret',",
            "    base_url='http://127.0.0.1:9',",
            "    timeout=1,",
            ")",
            "try:",
            "    assert isinstance(client, anthropic.Anthropic)",
            "    client_class = type(client).__module__ + '.' + type(client).__name__",
            "finally:",
            "    client.close()",
            "print(json.dumps({",
            "    'version': m.version('anthropic'),",
            "    'docstring_parser_version': m.version('docstring-parser'),",
            "    'module_from_vendor': module_from_vendor,",
            "    'client_class': client_class,",
            f"    'wheels': [{','.join(wheel_rows)}],",
            "}, sort_keys=True))",
        ]
    )
    return f"python3 -c {shlex.quote(python_probe)}"


def _safe_exception(exc: BaseException) -> str:
    """报告只记录异常类型，不持久化可能包含请求参数的异常正文。"""

    return type(exc).__name__


def _public_base_url(value: str) -> str:
    """移除 endpoint 中可能携带凭据的 userinfo、query 和 fragment。"""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return "<configured-endpoint>"
    if not parsed.scheme or not parsed.hostname:
        return "<configured-endpoint>"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port_text = f":{port}" if port is not None else ""
    return urlunsplit((parsed.scheme, host + port_text, parsed.path, "", ""))


def _redact_report_credentials(report: PreflightReport, *secrets: str) -> None:
    """递归清除意外进入 Observation/异常字段的真实凭据。"""

    active_secrets = tuple(secret for secret in secrets if secret)
    if not active_secrets:
        return
    redacted = False

    def scrub(value: Any) -> Any:
        nonlocal redacted
        if isinstance(value, str):
            safe = value
            for secret in active_secrets:
                if secret in safe:
                    safe = safe.replace(secret, "<redacted>")
                    redacted = True
            return safe
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, tuple):
            return tuple(scrub(item) for item in value)
        return value

    report.checks = scrub(report.checks)
    report.errors = scrub(report.errors)
    if redacted:
        report.errors.append("预检报告检测到凭据内容并已脱敏")


def check_local(
    *,
    require_credentials: bool = True,
    version_lock_path: Path | None = None,
) -> PreflightReport:
    report = PreflightReport()
    version = sys.version_info[:2]
    report.checks["python"] = ".".join(map(str, version))
    if version != EXPECTED_PYTHON:
        report.errors.append(f"需要 Python 3.12，当前为 {sys.version.split()[0]}")

    packages: dict[str, str | None] = {}
    for package, expected in EXPECTED_PACKAGES.items():
        try:
            observed = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            observed = None
        packages[package] = observed
        if observed != expected:
            report.errors.append(f"{package} 需要 {expected}，当前为 {observed or '未安装'}")
    report.checks["packages"] = packages

    _check_vendored_wheels(report, version_lock_path or VERSION_LOCK_PATH)

    credentials = {
        "ags_key": bool(_first_env("AGS_API_KEY", "E2B_API_KEY", "ROLLOUT_E2B_API_KEY")),
        "tokenhub_key": bool(
            _first_env("TOKENHUB_KEY", "ANTHROPIC_API_KEY", "ROLLOUT_LLM_API_KEY")
        ),
    }
    report.checks["credentials_present"] = credentials
    if require_credentials:
        for name, present in credentials.items():
            if not present:
                report.errors.append(f"缺少 {name} 环境变量")

    report.checks["ags"] = {
        "domain": _first_env("AGS_DOMAIN", "E2B_DOMAIN", "ROLLOUT_E2B_DOMAIN")
        or "ap-beijing.tencentags.com",
        "template": _first_env("AGS_TEMPLATE_ID", "ROLLOUT_E2B_TEMPLATE") or "node-python-hermes",
    }
    model_base_url = (
        _first_env("ANTHROPIC_BASE_URL", "TOKENHUB_BASE_URL", "ROLLOUT_LLM_BASE_URL")
        or "https://tokenhub.sensetime.com"
    )
    report.checks["model"] = {
        "provider": "anthropic",
        "base_url": _public_base_url(model_base_url),
        "name": _first_env("ANTHROPIC_MODEL", "ROLLOUT_LLM_MODEL") or "claude-opus-4-8",
    }
    return report


def _write_report(path: Path, report: PreflightReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_command(sandbox: Any, command: str, **kwargs: Any) -> Any:
    """保留非零命令结果，让调用方能够给出精确阶段诊断。"""

    try:
        return sandbox.commands.run(command, **kwargs)
    except BaseException as exc:
        # E2B/AGS 对非零退出码抛异常，但异常本身携带 exit_code/stdout/stderr。
        if hasattr(exc, "exit_code"):
            return exc
        raise


async def probe_ags(report_path: Path | None = None) -> PreflightReport:
    """新建北京 AGS 沙盒，检查 Hermes pin 和 Anthropic 三种请求。"""

    report = check_local(require_credentials=True)
    if not report.ok:
        if report_path is not None:
            await asyncio.to_thread(_write_report, report_path, report)
        return report

    from e2b_code_interpreter import Sandbox

    ags_key = _first_env("AGS_API_KEY", "E2B_API_KEY", "ROLLOUT_E2B_API_KEY")
    tokenhub_key = _first_env("TOKENHUB_KEY", "ANTHROPIC_API_KEY", "ROLLOUT_LLM_API_KEY")
    assert ags_key and tokenhub_key
    domain = report.checks["ags"]["domain"]
    template = report.checks["ags"]["template"]
    model_cfg = report.checks["model"]
    model_base_url = (
        _first_env("ANTHROPIC_BASE_URL", "TOKENHUB_BASE_URL", "ROLLOUT_LLM_BASE_URL")
        or "https://tokenhub.sensetime.com"
    )
    sandbox = None
    sandbox_id = None
    try:
        sandbox = await asyncio.to_thread(
            Sandbox.create,
            api_key=ags_key,
            domain=domain,
            template=template,
            timeout=600,
            allow_internet_access=True,
            metadata={"traceforge": "harbor-ags", "role": "preflight"},
        )
        sandbox_id = getattr(sandbox, "sandbox_id", None)
        report.checks["sandbox_id"] = sandbox_id
        version_result = await asyncio.to_thread(
            _run_command,
            sandbox,
            "set -eu; /home/user/.local/bin/hermes version; "
            "git -C /home/user/.hermes/hermes-agent rev-parse HEAD",
            timeout=60,
            user="user",
        )
        if getattr(version_result, "exit_code", 1) != 0:
            report.errors.append("Hermes 预置版本/源码/import 探针失败")
        else:
            lines = [line.strip() for line in (version_result.stdout or "").splitlines() if line]
            report.checks["hermes"] = {
                "version_output": lines[0] if lines else None,
                "commit": next(
                    (
                        line
                        for line in lines
                        if len(line) == 40
                        and all(character in "0123456789abcdef" for character in line)
                    ),
                    None,
                ),
            }
            if report.checks["hermes"]["commit"] is None:
                report.errors.append("Hermes git commit 未能锁定")

        if not report.errors:
            mkdir_result = await asyncio.to_thread(
                _run_command,
                sandbox,
                f"mkdir -p {shlex.quote(_REMOTE_VENDOR_DIR)} && "
                "chown -R user:user /tmp/harbor_ags_runtime",
                timeout=30,
                user="root",
            )
            if getattr(mkdir_result, "exit_code", 1) != 0:
                report.errors.append("AGS vendored wheel 目录创建失败")

        if not report.errors:
            for filename, expected_hash, remote_path in _VENDORED_WHEELS:
                resource = files("harbor_ags.resources").joinpath(filename)
                payload = resource.read_bytes()
                observed_hash = hashlib.sha256(payload).hexdigest()
                if observed_hash != expected_hash:
                    report.errors.append(f"上传前 wheel 哈希不匹配：{filename}")
                    continue
                await asyncio.to_thread(
                    sandbox.files.write,
                    remote_path,
                    payload,
                    user="root",
                    request_timeout=60,
                )

        if not report.errors:
            extract_result = await asyncio.to_thread(
                _run_command,
                sandbox,
                _wheel_extract_command(),
                timeout=120,
                user="root",
            )
            if getattr(extract_result, "exit_code", 1) != 0:
                report.errors.append("AGS Anthropic wheel 远端哈希/离线展开失败")

        if not report.errors:
            sdk_result = await asyncio.to_thread(
                _run_command,
                sandbox,
                _anthropic_runtime_probe_command(),
                timeout=60,
                cwd="/home/user/.hermes/hermes-agent",
                user="user",
                envs={
                    "PYTHONPATH": _REMOTE_SITE_PACKAGES,
                },
            )
            if getattr(sdk_result, "exit_code", 1) != 0:
                report.errors.append("AGS Anthropic SDK import/构造探针失败")
            else:
                try:
                    sdk_report = json.loads((sdk_result.stdout or "").splitlines()[-1])
                except (IndexError, json.JSONDecodeError):
                    report.errors.append("Anthropic SDK 探针未返回结构化结果")
                else:
                    if not isinstance(sdk_report, dict):
                        report.errors.append("Anthropic SDK 探针结果类型无效")
                    else:
                        clean_sdk_report = {
                            "version": sdk_report.get("version"),
                            "docstring_parser_version": sdk_report.get("docstring_parser_version"),
                            "module_from_vendor": sdk_report.get("module_from_vendor"),
                            "client_class": sdk_report.get("client_class"),
                            "wheels": sdk_report.get("wheels"),
                        }
                        report.checks["anthropic_sdk"] = clean_sdk_report
                        expected_remote_wheels = [
                            {
                                "name": _VENDORED_PACKAGE_VERSIONS[filename][0],
                                "version": _VENDORED_PACKAGE_VERSIONS[filename][1],
                                "wheel": filename,
                                "sha256": expected_hash,
                            }
                            for filename, expected_hash, _remote_path in _VENDORED_WHEELS
                        ]
                        if clean_sdk_report["version"] != _ANTHROPIC_VERSION:
                            report.errors.append("AGS Anthropic SDK 版本不匹配")
                        if (
                            clean_sdk_report["docstring_parser_version"]
                            != _DOCSTRING_PARSER_VERSION
                        ):
                            report.errors.append("AGS docstring-parser 版本不匹配")
                        if clean_sdk_report["module_from_vendor"] is not True:
                            report.errors.append("AGS Anthropic SDK 未从 vendored 目录加载")
                        if clean_sdk_report["client_class"] != "anthropic.Anthropic":
                            report.errors.append("AGS Anthropic client 类型不匹配")
                        if clean_sdk_report["wheels"] != expected_remote_wheels:
                            report.errors.append("AGS vendored wheel 远端哈希不匹配")

        if not report.errors:
            resource = files("harbor_ags.resources").joinpath("ags_preflight_probe.py")
            with as_file(resource) as script_path:
                await asyncio.to_thread(
                    sandbox.files.write,
                    "/tmp/ags_preflight_probe.py",
                    script_path.read_text(encoding="utf-8"),
                    user="root",
                    request_timeout=60,
                )
            api_result = await asyncio.to_thread(
                _run_command,
                sandbox,
                "python3 /tmp/ags_preflight_probe.py",
                timeout=360,
                user="user",
                envs={
                    "ANTHROPIC_BASE_URL": model_base_url,
                    "ANTHROPIC_API_KEY": tokenhub_key,
                    "ANTHROPIC_MODEL": model_cfg["name"],
                },
            )
            if getattr(api_result, "exit_code", 1) != 0:
                report.errors.append("AGS -> TokenHub Anthropic Messages 探针失败")
            else:
                try:
                    api_report = json.loads((api_result.stdout or "").splitlines()[-1])
                except (IndexError, json.JSONDecodeError):
                    report.errors.append("Anthropic 探针未返回结构化结果")
                else:
                    if not isinstance(api_report, dict):
                        report.errors.append("Anthropic 探针结果类型无效")
                    else:
                        report.checks["anthropic"] = api_report
    except Exception as exc:
        report.errors.append("AGS 探针异常：" + _safe_exception(exc))
    finally:
        if sandbox is not None:
            try:
                await asyncio.to_thread(sandbox.kill)
                report.checks["cleanup"] = {"sandbox_id": sandbox_id, "kill_called": True}
            except Exception as exc:
                report.errors.append("沙盒清理失败：" + _safe_exception(exc))

    _redact_report_credentials(report, ags_key, tokenhub_key)
    if report_path is not None:
        await asyncio.to_thread(_write_report, report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-ags", action="store_true", help="真实创建北京 AGS 并调用 TokenHub")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(probe_ags(args.output)) if args.probe_ags else check_local()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
