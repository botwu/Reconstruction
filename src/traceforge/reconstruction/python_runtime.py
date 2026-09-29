"""在目标沙盒解析 requirements，冻结 wheel，后续角色只作离线安装。"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any

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


def freeze_wheels(root: Path, requirements: Path) -> dict[str, Any]:
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
    (root / "install.sh").write_text(INSTALL_SCRIPT)
    manifest = {"schema_version": "traceforge.python-runtime.v1",
                "requirements_sha256": _hash(requirements),
                "files": entries + [{"file": name, "sha256": _hash(root / name)} for name in
                                    ("requirements.source.txt", "requirements.lock", "install.sh")]}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def validate_python_runtime(root: Path, requirements: Path) -> dict[str, Any]:
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
    if (root / "install.sh").read_text() != INSTALL_SCRIPT:
        raise ValueError("PYTHON_RUNTIME_INSTALLER_CHANGED")
    return manifest


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
