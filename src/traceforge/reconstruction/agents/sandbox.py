"""把角色 Agent 的文件/pytest 工具打到 AGS 沙盒，而不是宿主机 workspace。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents.roles import AgentRole
from traceforge.reconstruction.agents.session import AgentSession, safe_relpath
from traceforge.reconstruction.container_verification import (
    ContainerRuntime,
    pytest_test_runner,
)
from traceforge.verifier.grading import PytestVendorError, prepare_pytest_site

WORKSPACE_REMOTE = "/home/user/workspace"
EVIDENCE_REMOTE = "/evidence"
TESTS_REMOTE = "/tests/test_outputs.py"


@dataclass
class SandboxBinding:
    runtime: ContainerRuntime
    remote_root: str = WORKSPACE_REMOTE
    allow_exec: bool = False
    allow_tests: bool = False


def run_coro(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _exec(binding: SandboxBinding, command: str, *, user: str = "user", timeout: int = 30) -> Any:
    return run_coro(
        binding.runtime.exec(command, cwd="/", timeout_sec=timeout, user=user)
    )


def _stdout(result: Any) -> str:
    return str(getattr(result, "stdout", "") or "")


def _code(result: Any) -> int:
    value = getattr(result, "return_code", 1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _failure_detail(result: Any, *, encoded_content: str = "") -> str:
    """暴露可修复的运行错误，不把命令中的文件正文回显到日志。"""
    stderr = str(getattr(result, "stderr", "") or "").strip()
    if encoded_content:
        stderr = stderr.replace(encoded_content, "<文件内容已省略>")
    return f"exit_code={_code(result)}; stderr={stderr[-2000:] or '<空>'}"


def sandbox_list_paths(binding: SandboxBinding, raw: str) -> list[str]:
    prefix = safe_relpath(raw) if raw not in {"", "."} else ""
    script = (
        "from pathlib import Path; import json; "
        f"root=Path({binding.remote_root!r}); "
        "print(json.dumps([p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()]))"
    )
    result = _exec(
        binding,
        f"python3 -c {shlex.quote(script)}",
    )
    if _code(result) != 0:
        return []
    try:
        paths = json.loads(_stdout(result) or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(paths, list):
        return []
    items = [str(item) for item in paths if isinstance(item, str)]
    if not prefix:
        return sorted(items)
    return sorted(item for item in items if item == prefix or item.startswith(prefix + "/"))


def sandbox_read_file(binding: SandboxBinding, path: str) -> str:
    if safe_relpath(path) is None:
        return "error: unsafe path"
    script = (
        "from pathlib import Path\n"
        f"p = Path({binding.remote_root!r}) / {path!r}\n"
        "print(p.read_text(encoding='utf-8'), end='')\n"
    )
    result = _exec(binding, f"python3 -c {shlex.quote(script)}")
    if _code(result) != 0:
        return "error: file not found"
    return _stdout(result)


def sandbox_write_file(binding: SandboxBinding, path: str, content: str) -> str:
    if safe_relpath(path) is None:
        return "error: unsafe path"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    script = (
        "import base64\n"
        "from pathlib import Path\n"
        f"p = Path({binding.remote_root!r}) / {path!r}\n"
        "p.parent.mkdir(parents=True, exist_ok=True)\n"
        f"p.write_bytes(base64.b64decode({encoded!r}))\n"
        "print('wrote', p.as_posix())\n"
    )
    result = _exec(binding, f"python3 -c {shlex.quote(script)}")
    if _code(result) != 0:
        return "error: sandbox write failed; " + _failure_detail(result, encoded_content=encoded)
    return f"wrote {path}"


def sandbox_write_test(binding: SandboxBinding, content: str) -> str:
    if not binding.allow_tests:
        return "error: this agent cannot write tests"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    script = (
        "import base64\n"
        "from pathlib import Path\n"
        "p = Path('/tests/test_outputs.py')\n"
        "p.parent.mkdir(parents=True, exist_ok=True)\n"
        f"p.write_bytes(base64.b64decode({encoded!r}))\n"
    )
    result = _exec(binding, f"python3 -c {shlex.quote(script)}", user="root")
    if _code(result) != 0:
        return "error: sandbox test write failed"
    return "wrote tests/test_outputs.py"


def sandbox_run_pytest(
    binding: SandboxBinding, names: tuple[str, ...], *, test_sha256: str | None = None
) -> list[dict[str, Any]]:
    if not binding.allow_tests:
        return [{"name": "", "status": "INFRA_ERROR", "error_code": "TESTS_NOT_ALLOWED"}]
    runs = run_coro(pytest_test_runner(binding.runtime, names))
    return [
        {
            "name": run.name,
            "status": run.status,
            "stdout": run.stdout,
            "stderr": run.stderr,
            "error_code": run.error_code,
            "test_sha256": test_sha256,
            "input_sha256": run.input_sha256,
            "input_unchanged": run.input_unchanged,
        }
        for run in runs
    ]


def _normalize_user_records(records: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        if isinstance(item, dict) and item.get("id"):
            rows.append(
                {
                    "id": str(item["id"]),
                    "message_index": item.get("message_index"),
                    "text": str(item.get("text") or ""),
                }
            )
        elif isinstance(item, str):
            rows.append({"id": f"user:{index}", "message_index": index, "text": item})
    return rows


def staging_user_texts(records: list[Any], root: Path) -> Path:
    dest = root / "evidence"
    dest.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, Any]] = []
    for item in _normalize_user_records(records):
        stem = str(item["id"]).replace(":", "_")
        filename = f"{stem}.txt"
        (dest / filename).write_text(str(item.get("text") or ""), encoding="utf-8")
        index_rows.append(
            {
                "id": item["id"],
                "message_index": item.get("message_index"),
                "file": filename,
            }
        )
    (dest / "index.json").write_text(
        json.dumps(index_rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return dest


def bind_role_sandbox(role: AgentRole, runtime: ContainerRuntime) -> SandboxBinding:
    if role.name in {"intent", "session_tasks"}:
        return SandboxBinding(runtime, EVIDENCE_REMOTE, allow_exec=False, allow_tests=False)
    if role.name == "verifier":
        return SandboxBinding(runtime, WORKSPACE_REMOTE, allow_exec=False, allow_tests=True)
    if role.name == "completion":
        return SandboxBinding(runtime, WORKSPACE_REMOTE, allow_exec=True, allow_tests=False)
    return SandboxBinding(runtime, WORKSPACE_REMOTE, allow_exec=False, allow_tests=False)


async def prepare_role_sandbox(
    *,
    role: AgentRole,
    runtime: ContainerRuntime,
    session: AgentSession,
    staging_root: Path,
) -> SandboxBinding:
    binding = bind_role_sandbox(role, runtime)
    session.allow_environment_probe = role.name == "sufficiency"
    read_only = role.name in {"intent", "session_tasks", "sufficiency", "verifier"}
    await runtime.start(read_only=read_only)
    session.sandbox_started = True
    if role.name in {"intent", "session_tasks"}:
        await runtime.upload_dir(
            staging_user_texts(session.user_records or session.user_texts, staging_root),
            EVIDENCE_REMOTE,
        )
    elif role.name in {"completion", "sufficiency", "verifier"}:
        # ReplayResult is normally kept in memory. Uploading only the host
        # workspace therefore produced an empty sandbox for the real Hermes
        # path, even though the local scratch tree had foo.py.
        # Build one validated upload tree from host workspace and replay;
        # replay files win over stale host files.
        upload_root = staging_root / "sandbox_workspace"
        if upload_root.exists():
            shutil.rmtree(upload_root)
        upload_root.mkdir(parents=True, exist_ok=True)
        if session.workspace is not None and session.workspace.is_dir():
            for source in session.workspace.rglob("*"):
                if not source.is_file() or source.is_symlink():
                    continue
                rel = source.relative_to(session.workspace).as_posix()
                if safe_relpath(rel) is None:
                    continue
                target = upload_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        for rel, content in session.replay_files.items():
            if safe_relpath(rel) is None or not isinstance(content, str):
                continue
            target = upload_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        await runtime.upload_dir(upload_root, WORKSPACE_REMOTE)
        if role.name in {"sufficiency", "verifier"} and session.workspace is not None:
            from traceforge.reconstruction.python_runtime import prepare_python_runtime

            await prepare_python_runtime(
                runtime, workspace=session.workspace, remote_workspace=WORKSPACE_REMOTE,
                staging_root=staging_root,
            )
    if role.name == "completion":
        # AGS 上传保留 root 所有权；仅顶层可写仍无法补全已上传子目录。
        # 正文证据锁定继续由 session 的 write_file 策略执行。
        writable = await runtime.exec(
            "chown -R user:user /home/user/workspace && chmod -R u+rwX /home/user/workspace",
            cwd="/", timeout_sec=30, user="root",
        )
        if _code(writable) != 0:
            raise RuntimeError("COMPLETION_WORKSPACE_SETUP_FAILED: " + _failure_detail(writable))
    if role.name == "sufficiency":
        chmod = await runtime.exec(
            "chmod -R a-w /home/user/workspace && find /home/user/workspace -type f -exec chmod a+r {} +",
            cwd="/",
            timeout_sec=30,
            user="root",
        )
        if getattr(chmod, "return_code", 1) != 0:
            session.policy_errors.append("READ_ONLY_SETUP_FAILED")
        probe = await runtime.exec(
            "touch /home/user/workspace/.traceforge-write-probe",
            cwd="/",
            timeout_sec=10,
            user="user",
        )
        probe_code = getattr(probe, "return_code", 1)
        probe_stderr = str(getattr(probe, "stderr", "") or "")
        denied = any(
            marker in probe_stderr.lower()
            for marker in ("permission denied", "read-only", "readonly", "operation not permitted")
        )
        session.read_only_probe_blocked = bool(probe_code != 0 and denied)
        session.read_only_probe = {
            "setup_return_code": int(getattr(chmod, "return_code", 1)),
            "write_return_code": int(probe_code),
            "write_denied": session.read_only_probe_blocked,
            "stderr_sha256": hashlib.sha256(probe_stderr.encode("utf-8")).hexdigest(),
        }
        if not session.read_only_probe_blocked:
            session.policy_errors.append(
                "JUDGE_WORKSPACE_WRITEABLE" if probe_code == 0 else "READ_ONLY_PROBE_INCONCLUSIVE"
            )
    if role.name == "verifier":
        # Verifier tests may use a disposable copy, never the initial input.
        protect = await runtime.exec(
            "chmod -R a-w /home/user/workspace && find /home/user/workspace -type f -exec chmod a+r {} +",
            cwd="/", timeout_sec=30, user="root",
        )
        if getattr(protect, "return_code", 1) != 0:
            session.policy_errors.append("VERIFIER_INPUT_PROTECTION_FAILED")
        await runtime.exec("mkdir -p /tests", cwd="/", timeout_sec=10, user="root")
        # AGS 模板不预装 pytest。使用仓库锁定的 wheel 在宿主机展开，
        # 仅把 site-packages 上传到验证沙盒，保证离线且不依赖 Agent 环境。
        vendor_root = staging_root / "pytest_vendor_runtime"
        if vendor_root.exists():
            shutil.rmtree(vendor_root)
        try:
            site = prepare_pytest_site(vendor_root)
            await runtime.upload_dir(site.parent, "/tests")
        except (PytestVendorError, OSError) as exc:
            code = getattr(exc, "code", "PYTEST_VENDOR_ERROR")
            session.policy_errors.append(str(code))
    session.sandbox = binding
    session.allow_tests = binding.allow_tests
    session.allow_exec = binding.allow_exec
    return binding


class LocalExecRuntime:
    """把本地目录当成 AGS remote，供单测执行 python/pytest 命令。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.remote = self.root / "remote"
        self.tests = self.root / "tests"
        self.started = False
        self.stopped = False
        self.read_only = False
        self.execs: list[str] = []

    async def start(self, *, read_only: bool = False) -> None:
        self.started = True
        self.read_only = read_only
        self.remote.mkdir(parents=True, exist_ok=True)
        self.tests.mkdir(parents=True, exist_ok=True)

    async def stop(self, *, delete: bool = True) -> None:
        self.stopped = True

    async def upload_dir(self, source_dir: Path, target_dir: str) -> None:
        dest = self._map(target_dir)
        dest.mkdir(parents=True, exist_ok=True)
        for path in Path(source_dir).rglob("*"):
            if path.is_file():
                target = dest / path.relative_to(source_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())

    async def download_dir(self, source_dir: str, target_dir: Path) -> None:
        import shutil

        shutil.copytree(self._map(source_dir), target_dir, dirs_exist_ok=True)

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_sec: int | None = None,
        user: str | None = None,
    ) -> Any:
        del cwd
        self.execs.append(command)
        if self.read_only and command.strip().startswith("touch "):
            return _ExecResult(1, "", "read-only")
        if command.startswith(("chmod ", "chown ")):
            return _ExecResult(0, "", "")
        # Replace remote paths through placeholders. A direct sequential
        # replacement is unsafe when a local staging directory itself contains
        # a component such as /tests: the later /tests rule would then rewrite
        # the path we just produced and duplicate the local root.
        mappings = (
            ("/home/user/workspace", str(self.remote)),
            ("/evidence", str(self.root / "evidence_remote")),
            ("/tests/test_outputs.py", str(self.tests / "test_outputs.py")),
            ("/tests", str(self.tests)),
        )
        mapped = command
        placeholders: dict[str, str] = {}
        # The command is already shell-quoted. Replace raw remote path bytes
        # only; matching repr(remote) corrupts shell quote escape fragments.
        for index, (remote, local) in enumerate(sorted(mappings, key=lambda item: -len(item[0]))):
            token = f"__TRACEFORGE_REMOTE_PATH_{index}__"
            placeholders[token] = local
            mapped = mapped.replace(remote, token)
        for token, local in placeholders.items():
            mapped = mapped.replace(token, local)
        if mapped.startswith("mkdir "):
            Path(mapped.split()[-1]).mkdir(parents=True, exist_ok=True)
            return _ExecResult(0, "", "")
        # Use a known shell instead of an environment-specific /bin/sh wrapper.
        # No interactive stdin or startup file should hold a verifier run open.
        env = dict(os.environ)
        env.pop("BASH_ENV", None)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        # Hermes' runtime venv intentionally has no pytest. The local test
        # runtime is only an AGS stand-in, so use the bundled verification venv
        # when present while leaving real AGS commands untouched.
        local_python = os.environ.get("TRACEFORGE_LOCAL_PYTHON", "")
        bundled_python = Path("/tmp/e2b_venv/bin/python")
        if not local_python and bundled_python.is_file():
            local_python = str(bundled_python)
        if local_python:
            mapped = mapped.replace("python -m pytest", f"{shlex.quote(local_python)} -m pytest")
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "--noprofile", "--norc", "-c", mapped,
            cwd=str(self.root),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec or 30)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise
        return _ExecResult(proc.returncode or 0, stdout.decode(), stderr.decode())

    def _map(self, target_dir: str) -> Path:
        if target_dir.rstrip("/") == "/home/user/workspace":
            return self.remote
        if target_dir.rstrip("/") == "/evidence":
            dest = self.root / "evidence_remote"
            dest.mkdir(parents=True, exist_ok=True)
            return dest
        if target_dir.rstrip("/") == "/tests":
            return self.tests
        return self.remote


@dataclass
class _ExecResult:
    return_code: int
    stdout: str = ""
    stderr: str = ""
