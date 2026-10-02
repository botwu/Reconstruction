"""在目标沙盒解析 requirements，冻结 wheel，后续角色只作离线安装。"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents.session import safe_relpath

RUNTIME_NAME = "python_runtime"
REMOTE_RUNTIME = "/opt/traceforge-python-runtime"
INSTALL_SCRIPT = '''#!/bin/sh
set -eu
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -m pip install --disable-pip-version-check --no-index --no-deps --require-hashes \\
    --no-compile --target /opt/traceforge-python-runtime/site \\
    --find-links "$bundle/wheels" -r "$bundle/requirements.lock"
python3 - <<'PY'
import site
from pathlib import Path
path = Path(site.getsitepackages()[0]) / "traceforge-task.pth"
path.write_text('import sys; sys.path.insert(0, "/opt/traceforge-python-runtime/site")\\n')
PY
'''


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_wheels(
    root: Path, requirements: Path, *, install_script: str = INSTALL_SCRIPT,
) -> dict[str, Any]:
    """保留原声明，锁定由目标 Python 下载的全部直接和间接依赖。"""
    entries, locked = [], []
    for wheel in sorted((root / "wheels").glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            metadata = [n for n in archive.namelist()
                        if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise ValueError("PYTHON_WHEEL_METADATA_INVALID")
            parsed = Parser().parsestr(archive.read(metadata[0]).decode())
        name, version, digest = parsed["Name"], parsed["Version"], _hash(wheel)
        if not name or not version or any(c in name + version for c in "\r\n\t "):
            raise ValueError("PYTHON_WHEEL_METADATA_INVALID")
        locked.append(f"{name}=={version} --hash=sha256:{digest}")
        entries.append({"file": f"wheels/{wheel.name}", "sha256": digest})
    if not entries:
        raise ValueError("PYTHON_WHEELS_MISSING")
    shutil.copyfile(requirements, root / "requirements.source.txt")
    (root / "requirements.lock").write_text("\n".join(locked) + "\n")
    (root / "install.sh").write_text(install_script)
    manifest = {"schema_version": "traceforge.python-runtime.v1",
                "requirements_sha256": _hash(requirements),
                "files": entries + [{"file": name, "sha256": _hash(root / name)} for name in
                                    ("requirements.source.txt", "requirements.lock", "install.sh")]}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def validate_python_runtime(
    root: Path, requirements: Path, *, install_script: str = INSTALL_SCRIPT,
) -> dict[str, Any]:
    manifest = json.loads((root / "manifest.json").read_text())
    if (manifest.get("schema_version") != "traceforge.python-runtime.v1"
            or manifest.get("requirements_sha256") != _hash(requirements)):
        raise ValueError("PYTHON_RUNTIME_REQUIREMENTS_CHANGED")
    expected = {"manifest.json", *(item["file"] for item in manifest["files"])}
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("PYTHON_RUNTIME_CHANGED")
    for item in manifest["files"]:
        path = root / item["file"]
        if (path.is_symlink() or root.resolve() not in path.resolve().parents
                or _hash(path) != item["sha256"]):
            raise ValueError("PYTHON_RUNTIME_CHANGED")
    if (root / "install.sh").read_text() != install_script:
        raise ValueError("PYTHON_RUNTIME_INSTALLER_CHANGED")
    return manifest


def read_locked_wheel_member(
    bundle: Path, requirements: str, *, distribution: str, version: str, member: str,
) -> tuple[str, dict[str, Any]]:
    """仅从已校验的运行时锁读取指定依赖成员，不接受模型提供的宿主路径。"""
    manifest = validate_python_runtime(bundle, bundle / "requirements.source.txt")
    if hashlib.sha256(requirements.encode()).hexdigest() != manifest["requirements_sha256"]:
        raise ValueError("PYTHON_RUNTIME_REQUIREMENTS_CHANGED")
    if not isinstance(member, str) or safe_relpath(member) != member:
        raise ValueError("依赖成员路径无效")
    normalize = lambda value: re.sub(r"[-_.]+", "-", str(value)).lower()
    matches = []
    for entry in manifest["files"]:
        if not entry["file"].endswith(".whl"):
            continue
        with zipfile.ZipFile(bundle / entry["file"]) as archive:
            metadata = [name for name in archive.namelist()
                        if name.count("/") == 1 and name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise ValueError("PYTHON_WHEEL_METADATA_INVALID")
            package = Parser().parsestr(archive.read(metadata[0]).decode())
            if (normalize(package["Name"]) != normalize(distribution)
                    or package["Version"] != version):
                continue
            entries = [item for item in archive.infolist() if item.filename == member]
            if len(entries) != 1 or entries[0].is_dir() or entries[0].external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("依赖成员不存在、不唯一或不是普通文件")
            raw = archive.read(member)
            matches.append((raw.decode("utf-8"), {
                "distribution": package["Name"], "version": package["Version"],
                "wheel": entry["file"], "wheel_sha256": entry["sha256"],
                "member": member, "member_sha256": hashlib.sha256(raw).hexdigest(),
            }))
    if len(matches) != 1:
        raise ValueError("锁定依赖中没有唯一的指定包版本")
    return matches[0]


async def prepare_python_runtime(
    runtime: Any, *, workspace: Path, remote_workspace: str, staging_root: Path,
) -> Path | None:
    """只消费实际 requirements 文件；不猜测自由文本依赖或在宿主机安装。"""
    requirements = workspace / "requirements.txt"
    if not requirements.is_file():
        return None
    bundle = workspace.parent / RUNTIME_NAME
    receipt: dict[str, Any] = {"requirements_sha256": _hash(requirements), "steps": []}

    async def execute(stage: str, command: str, timeout: int = 180) -> None:
        receipt.update(status="RUNNING", current_stage=stage, timeout_seconds=timeout)
        (staging_root / "python-runtime-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        result = await runtime.exec("timeout " + str(timeout) + " " + command,
                                    cwd="/", timeout_sec=timeout + 30, user="root")
        receipt["steps"].append({"stage": stage, "exit_code": result.return_code,
                                 "stdout": result.stdout, "stderr": result.stderr})
        if result.return_code:
            raise RuntimeError("PYTHON_RUNTIME_" + stage.upper() + "_FAILED")

    try:
        if not bundle.exists():
            temporary = staging_root / RUNTIME_NAME
            temporary.mkdir(parents=True, exist_ok=False)
            args = ["python3", "-m", "pip", "download", "--disable-pip-version-check",
                    "--timeout", "12", "--retries", "0", "--index-url", "https://pypi.org/simple",
                    "--only-binary=:all:", "--dest", "/tmp/traceforge-wheels", "-r",
                    remote_workspace + "/requirements.txt"]
            # 进程自己限时收尾，避免 AGS RPC 超时吞掉 pip 的失败输出。
            await execute("download", shlex.join(args), timeout=900)
            await runtime.download_dir("/tmp/traceforge-wheels", temporary / "wheels")
            freeze_wheels(temporary, requirements)
            temporary.rename(bundle)
        receipt["manifest"] = validate_python_runtime(bundle, requirements)
        await runtime.upload_dir(bundle, REMOTE_RUNTIME)
        await execute("install", "sh " + REMOTE_RUNTIME + "/install.sh")
        receipt["status"] = "READY"
        return bundle
    except Exception as exc:
        receipt.update(status="ERROR", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        (staging_root / "python-runtime-receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        )
