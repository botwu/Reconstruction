"""Harbor 在腾讯 AGS 预置模板上的异步环境适配。

AGS 当前提供的是 E2B 兼容的同步 SDK。这个模块刻意不让同步调用进入
Harbor 的事件循环，并把沙盒创建、传输和销毁都记入控制端 ledger。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shlex
import stat
import tarfile
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeVar, override

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.models.task.config import NetworkMode

from .sandbox_ledger import (
    SandboxCleanupError,
    SandboxLedgerAudit,
    assert_all_sandboxes_deleted,
    audit_sandbox_ledger,
)

EnvironmentRole = Literal["agent", "verifier"]
_T = TypeVar("_T")


class UnsafeTransferError(RuntimeError):
    """文件树不能在不突破隔离边界的情况下安全传输。"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _remote_path(value: str, *, label: str) -> PurePosixPath:
    if not value or "\x00" in value:
        raise UnsafeTransferError(f"{label} 不能为空且不能包含 NUL")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise UnsafeTransferError(f"{label} 必须是无 '..' 的绝对 POSIX 路径: {value!r}")
    return path


def _safe_archive_name(name: str) -> PurePosixPath:
    if "\x00" in name:
        raise UnsafeTransferError("归档成员名包含 NUL")
    while name.startswith("./"):
        name = name[2:]
    path = PurePosixPath(name)
    if not name or name == "." or path.is_absolute() or ".." in path.parts:
        raise UnsafeTransferError(f"不安全的归档成员路径: {name!r}")
    return path


def _scan_safe_directory(
    source_dir: Path, *, max_files: int, max_bytes: int
) -> list[tuple[Path, str, os.stat_result]]:
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise UnsafeTransferError(f"源目录必须是非符号链接目录: {source_dir}")

    entries: list[tuple[Path, str, os.stat_result]] = []
    file_count = 0
    total_bytes = 0
    for current, dirnames, filenames in os.walk(source_dir, followlinks=False):
        current_path = Path(current)
        for name in sorted([*dirnames, *filenames]):
            path = current_path / name
            info = path.lstat()
            rel = path.relative_to(source_dir).as_posix()
            if stat.S_ISLNK(info.st_mode):
                raise UnsafeTransferError(f"源目录包含符号链接: {rel}")
            if stat.S_ISDIR(info.st_mode):
                entries.append((path, rel, info))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise UnsafeTransferError(f"源目录包含特殊文件: {rel}")
            if info.st_nlink != 1:
                raise UnsafeTransferError(f"源目录包含硬链接文件: {rel}")
            file_count += 1
            total_bytes += info.st_size
            if file_count > max_files:
                raise UnsafeTransferError(f"源目录文件数超过限制 {max_files}")
            if total_bytes > max_bytes:
                raise UnsafeTransferError(f"源目录总大小超过限制 {max_bytes} bytes")
            entries.append((path, rel, info))
    return entries


def _pack_safe_directory(source_dir: Path, *, max_files: int, max_bytes: int) -> bytes:
    entries = _scan_safe_directory(source_dir, max_files=max_files, max_bytes=max_bytes)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", dereference=False) as archive:
        for path, rel, scanned in entries:
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_mode, current.st_size) != (
                scanned.st_dev,
                scanned.st_ino,
                scanned.st_mode,
                scanned.st_size,
            ):
                raise UnsafeTransferError(f"打包期间源文件发生变化: {rel}")
            member = archive.gettarinfo(str(path), arcname=rel)
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            if member.isdir():
                archive.addfile(member)
            elif member.isreg():
                with path.open("rb") as stream:
                    archive.addfile(member, stream)
            else:  # 二次检查，防止扫描后发生类型切换。
                raise UnsafeTransferError(f"打包期间出现特殊文件: {rel}")
    return output.getvalue()


def _ensure_no_symlink_parents(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts[:-1]:
        current /= part
        if current.exists() and current.is_symlink():
            raise UnsafeTransferError(f"目标目录包含符号链接父级: {current}")


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise UnsafeTransferError(f"目标不是普通文件: {path}")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.chmod(temporary, mode & 0o777)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def _extract_safe_tar(data: bytes, target_dir: Path, *, max_files: int, max_bytes: int) -> None:
    members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
    seen: set[PurePosixPath] = set()
    file_count = 0
    total_bytes = 0

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive.getmembers():
            root_name = member.name
            while root_name.startswith("./"):
                root_name = root_name[2:]
            if root_name in {"", "."}:
                if member.isdir():
                    continue
                raise UnsafeTransferError("归档根成员必须是目录")
            path = _safe_archive_name(member.name)
            if path in seen:
                raise UnsafeTransferError(f"归档包含重复路径: {path}")
            seen.add(path)
            if member.isdir():
                pass
            elif member.isreg():
                if member.size < 0:
                    raise UnsafeTransferError(f"归档成员大小非法: {path}")
                file_count += 1
                total_bytes += member.size
                if file_count > max_files:
                    raise UnsafeTransferError(f"归档文件数超过限制 {max_files}")
                if total_bytes > max_bytes:
                    raise UnsafeTransferError(f"归档解压后大小超过限制 {max_bytes} bytes")
            else:
                raise UnsafeTransferError(f"归档包含链接或特殊文件: {path}")
            members.append((member, path))

        with tempfile.TemporaryDirectory(prefix="harbor-ags-extract-") as temp:
            staging = Path(temp)
            for member, relative in members:
                destination = staging.joinpath(*relative.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    os.chmod(destination, member.mode & 0o777)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise UnsafeTransferError(f"无法读取归档成员: {relative}")
                content = source.read(member.size + 1)
                if len(content) != member.size:
                    raise UnsafeTransferError(f"归档成员长度不一致: {relative}")
                _atomic_write(destination, content, member.mode)

            if target_dir.exists() and (target_dir.is_symlink() or not target_dir.is_dir()):
                raise UnsafeTransferError(f"下载目标必须是普通目录: {target_dir}")
            target_dir.mkdir(parents=True, exist_ok=True)

            # 先检查所有现有目标，避免发现冲突前就产生部分覆盖。
            for member, relative in members:
                destination = target_dir.joinpath(*relative.parts)
                _ensure_no_symlink_parents(target_dir, destination)
                if not destination.exists():
                    continue
                if destination.is_symlink():
                    raise UnsafeTransferError(f"目标树包含符号链接: {destination}")
                if member.isdir() and not destination.is_dir():
                    raise UnsafeTransferError(f"目录目标发生类型冲突: {destination}")
                if member.isreg() and not destination.is_file():
                    raise UnsafeTransferError(f"文件目标发生类型冲突: {destination}")

            for member, relative in members:
                source = staging.joinpath(*relative.parts)
                destination = target_dir.joinpath(*relative.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    os.chmod(destination, member.mode & 0o777)
                else:
                    _atomic_write(destination, source.read_bytes(), member.mode)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("ledger write 未取得进展")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class AGSPrebuiltEnvironment(BaseEnvironment):
    """使用固定 AGS 模板实现 Harbor 0.22.0 ``BaseEnvironment``。"""

    _ledger_lock = threading.Lock()
    _MODEL_ENV_PREFIXES = (
        "ANTHROPIC_",
        "OPENAI_",
        "TOKENHUB_",
        "SERPER_",
        "FIRECRAWL_",
    )

    def __init__(
        self,
        *args: Any,
        template: str | None = None,
        domain: str | None = None,
        api_key: str | None = None,
        role: EnvironmentRole | None = None,
        workspace_dir: str = "/home/user/workspace",
        agent_user: str = "user",
        sandbox_timeout_sec: int = 3_600,
        request_timeout_sec: float = 60.0,
        transfer_timeout_sec: int = 120,
        max_transfer_files: int = 10_000,
        max_transfer_bytes: int = 512 * 1024 * 1024,
        ledger_path: Path | str | None = None,
        trial_input_path: Path | str | None = None,
        trial_input_manifest_path: Path | str | None = None,
        sandbox_factory: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> None:
        session_id = str(kwargs.get("session_id", ""))
        inferred_role = self._infer_role(session_id)
        if role is not None and role != inferred_role:
            raise ValueError(f"显式角色 {role!r} 与 session_id 推导角色 {inferred_role!r} 冲突")
        self._role = inferred_role
        self._workspace_dir = str(_remote_path(workspace_dir, label="workspace_dir"))
        self._agent_user = agent_user
        self._template = (
            template
            or os.environ.get("AGS_TEMPLATE_ID")
            or os.environ.get("ROLLOUT_E2B_TEMPLATE")
            or ""
        ).strip()
        self._domain = (
            domain
            or os.environ.get("AGS_DOMAIN")
            or os.environ.get("E2B_DOMAIN")
            or os.environ.get("ROLLOUT_E2B_DOMAIN")
            or "ap-beijing.tencentags.com"
        ).strip()
        self._api_key = (
            api_key
            or os.environ.get("AGS_API_KEY")
            or os.environ.get("E2B_API_KEY")
            or os.environ.get("ROLLOUT_E2B_API_KEY")
            or ""
        ).strip()
        self._sandbox_timeout_sec = int(sandbox_timeout_sec)
        self._request_timeout_sec = float(request_timeout_sec)
        self._transfer_timeout_sec = int(transfer_timeout_sec)
        self._max_transfer_files = int(max_transfer_files)
        self._max_transfer_bytes = int(max_transfer_bytes)
        self._sandbox_factory = sandbox_factory or self._create_sandbox_sync
        self._sandbox: Any | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._created_ids: set[str] = set()
        self._terminal_ids: set[str] = set()
        self.sandbox_id: str | None = None

        trial_paths = kwargs.get("trial_paths")
        if ledger_path is None and trial_paths is not None:
            ledger_path = (
                Path(trial_paths.trial_dir).parent / "_control" / "ags-sandbox-ledger.jsonl"
            )
        self._ledger_path = Path(ledger_path) if ledger_path else None

        default_control_dir = (
            Path(trial_paths.trial_dir) / "control" if trial_paths is not None else None
        )
        self._trial_input_path = (
            Path(trial_input_path)
            if trial_input_path
            else default_control_dir / "input.json"
            if default_control_dir
            else None
        )
        self._trial_input_manifest_path = (
            Path(trial_input_manifest_path)
            if trial_input_manifest_path
            else default_control_dir / "input-manifest.json"
            if default_control_dir
            else None
        )

        super().__init__(*args, **kwargs)
        if self._role == "agent":
            self.default_user = self._agent_user
        # Agent 与 Verifier 的 sandbox 都不得常驻模型密钥。真 key 只由
        # CaptureProxy 通过 CAPTURE_UPSTREAM_API_KEY 按次注入。
        self._persistent_env = self._without_model_env(self._persistent_env)

    @staticmethod
    def _infer_role(session_id: str) -> EnvironmentRole:
        if "__verifier__" in session_id:
            return "verifier"
        if session_id.endswith("__env"):
            return "agent"
        raise ValueError(
            "无法从 session_id 识别环境角色；应为 '<trial>__env' 或 '<trial>__verifier__<key>'"
        )

    @staticmethod
    @override
    def type() -> str:
        return "ags-prebuilt"

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(disable_internet=True)

    @property
    def role(self) -> EnvironmentRole:
        return self._role

    @property
    def ledger_path(self) -> Path | None:
        return self._ledger_path

    @property
    def created_sandbox_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._created_ids))

    @property
    def cleanup_unverified_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._created_ids - self._terminal_ids))

    @classmethod
    @override
    def preflight(cls) -> None:
        template = os.environ.get("AGS_TEMPLATE_ID") or os.environ.get("ROLLOUT_E2B_TEMPLATE")
        key = (
            os.environ.get("AGS_API_KEY")
            or os.environ.get("E2B_API_KEY")
            or os.environ.get("ROLLOUT_E2B_API_KEY")
        )
        if not template:
            raise SystemExit("AGSPrebuiltEnvironment 需要 AGS_TEMPLATE_ID")
        if not key:
            raise SystemExit("AGSPrebuiltEnvironment 需要 AGS_API_KEY 或 E2B_API_KEY")

    @override
    def _validate_definition(self) -> None:
        if not self._template:
            raise ValueError("AGS 预置模板 ID 不能为空")
        if not self._domain:
            raise ValueError("AGS domain 不能为空")
        if not self.environment_dir.is_dir():
            raise FileNotFoundError(f"环境目录不存在: {self.environment_dir}")
        if self._sandbox_timeout_sec <= 0 or self._request_timeout_sec <= 0:
            raise ValueError("sandbox/request timeout 必须为正数")
        if self._max_transfer_files <= 0 or self._max_transfer_bytes <= 0:
            raise ValueError("传输文件数和字节限制必须为正数")
        if self._role == "agent":
            public_source = self._agent_public_source()
            if (public_source / "solution").exists():
                raise ValueError("Agent public workspace 不得包含 solution/")
            configured_workdir = self.task_env_config.workdir
            if configured_workdir and configured_workdir != self._workspace_dir:
                raise ValueError(
                    "AGS smoke Agent 工作目录固定为 "
                    f"{self._workspace_dir}，收到 {configured_workdir}"
                )

    def _agent_public_source(self) -> Path:
        """返回正式 Task Bundle 的公开输入面。"""

        workspace = self.environment_dir.parent / "workspace"
        if not workspace.is_dir():
            raise FileNotFoundError("正式 Task Bundle 缺少 task/workspace/")
        return workspace

    @staticmethod
    def _create_sandbox_sync(**kwargs: Any) -> Any:
        from e2b_code_interpreter import Sandbox

        return Sandbox.create(**kwargs)

    async def _complete_blocking(
        self, function: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> tuple[_T, asyncio.CancelledError | None]:
        task = asyncio.create_task(asyncio.to_thread(partial(function, *args, **kwargs)))
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except BaseException:
                break
        try:
            result = task.result()
        except BaseException as exc:
            if cancellation is not None:
                raise cancellation from exc
            raise
        return result, cancellation

    async def _call_sync(self, function: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        result, cancellation = await self._complete_blocking(function, *args, **kwargs)
        if cancellation is not None:
            raise cancellation
        return result

    async def _record(self, event: str, **fields: Any) -> None:
        if self._ledger_path is None:
            return
        record = {
            "schema_version": 1,
            "timestamp": _utc_now(),
            "event": event,
            "session_id": self.session_id,
            "context_id": str(self.context_id) if self.context_id else None,
            "environment_name": self.environment_name,
            "role": self._role,
            **fields,
        }

        def append() -> None:
            with self._ledger_lock:
                _append_jsonl(self._ledger_path, record)

        await self._call_sync(append)

    @override
    def _startup_env(self) -> dict[str, str]:
        return self._without_model_env(super()._startup_env())

    @override
    def _merge_env(self, env: dict[str, str] | None) -> dict[str, str] | None:
        merged = super()._merge_env(env)
        if not merged:
            return merged
        return self._without_model_env(merged)

    def _create_kwargs(self) -> dict[str, Any]:
        envs = self._startup_env()
        metadata = {
            "harbor": "1",
            "environment_name": self.environment_name,
            "session_id": self.session_id,
            "role": self._role,
        }
        if self.context_id:
            metadata["context_id"] = str(self.context_id)
        return {
            "template": self._template,
            "timeout": self._sandbox_timeout_sec,
            "request_timeout": self._request_timeout_sec,
            "api_key": self._api_key,
            "domain": self._domain,
            "metadata": metadata,
            "envs": envs,
            "secure": True,
            "allow_internet_access": self.network_policy.network_mode != NetworkMode.NO_NETWORK,
        }

    @classmethod
    def _without_model_env(cls, env: dict[str, str]) -> dict[str, str]:
        return {
            key: value
            for key, value in env.items()
            if not key.upper().startswith(cls._MODEL_ENV_PREFIXES)
        }

    async def _kill_sandbox(self, sandbox: Any, *, reason: str) -> None:
        sandbox_id = str(getattr(sandbox, "sandbox_id", "") or "")
        try:
            killed, cancellation = await self._complete_blocking(
                sandbox.kill, request_timeout=self._request_timeout_sec
            )
        except BaseException as exc:
            await self._record(
                "kill_failed",
                sandbox_id=sandbox_id or None,
                reason=reason,
                error_type=type(exc).__name__,
            )
            raise SandboxCleanupError(f"AGS sandbox {sandbox_id or '<unknown>'} 销毁失败") from exc

        # False 的 SDK 语义是“目标不存在”，同样确认不再运行。
        self._terminal_ids.add(sandbox_id)
        await self._record(
            "killed" if killed else "kill_not_found",
            sandbox_id=sandbox_id,
            reason=reason,
        )
        if cancellation is not None:
            raise cancellation

    async def _prepare_role_files(self) -> None:
        if self._sandbox is None:
            raise RuntimeError("Sandbox 尚未创建")
        root = self._workspace_dir if self._role == "agent" else "/tests"
        await self._call_sync(
            self._sandbox.files.make_dir,
            root,
            user="root",
            request_timeout=self._request_timeout_sec,
        )
        directories = [root, *self._mount_targets(writable_only=True)]
        result = await self.ensure_dirs(directories)
        if result is not None and result.return_code != 0:
            raise RuntimeError(
                f"创建 AGS 运行目录失败: {result.stderr or result.stdout or 'no output'}"
            )
        source = self._agent_public_source() if self._role == "agent" else self.environment_dir
        await self.upload_dir(source, root)

        # 冻结任务环境的准备入口；先于 agent/独立 verifier 启动。
        setup = self.environment_dir / "setup.sh"
        if setup.is_file():
            setup_root = "/opt/harbor-task-environment" if self._role == "agent" else root
            if self._role == "agent":
                await self.upload_dir(self.environment_dir, setup_root)
            prepared = await self.exec(
                f"timeout 180 sh {setup_root}/setup.sh", cwd="/", timeout_sec=210, user="root",
            )
            await self._record("task_environment_prepared", exit_code=prepared.return_code,
                               stdout=prepared.stdout, stderr=prepared.stderr)
            if prepared.return_code != 0:
                raise RuntimeError("Task environment setup.sh 执行失败")

        input_path = self._trial_input_path
        manifest_path = self._trial_input_manifest_path
        input_exists = bool(input_path and input_path.is_file())
        manifest_exists = bool(manifest_path and manifest_path.is_file())
        if input_exists != manifest_exists:
            raise UnsafeTransferError(
                "Trial control 输入不完整：input.json 与 input-manifest.json 必须同时存在"
            )
        if input_exists and input_path is not None and manifest_path is not None:
            await self.inject_trial_input(
                input_path,
                manifest_path,
            )

    @override
    async def start(self, force_build: bool) -> None:
        if force_build:
            raise ValueError("AGSPrebuiltEnvironment 使用冻结模板，不支持 force_build")
        async with self._lifecycle_lock:
            if self._sandbox is not None:
                raise RuntimeError("该 Environment 已经有运行中的 sandbox")
            if not self._api_key:
                raise RuntimeError("AGS API key 未配置")

            create_operation_id = os.urandom(16).hex()
            try:
                await self._record(
                    "create_requested",
                    template=self._template,
                    create_operation_id=create_operation_id,
                )
            except asyncio.CancelledError:
                await self._record(
                    "create_cancelled_without_sandbox",
                    create_operation_id=create_operation_id,
                )
                raise
            try:
                sandbox, cancellation = await self._complete_blocking(
                    self._sandbox_factory, **self._create_kwargs()
                )
            except asyncio.CancelledError:
                await self._record(
                    "create_cancelled_without_sandbox",
                    create_operation_id=create_operation_id,
                )
                raise
            except BaseException as exc:
                await self._record(
                    "create_failed",
                    create_operation_id=create_operation_id,
                    error_type=type(exc).__name__,
                )
                raise

            sandbox_id = str(getattr(sandbox, "sandbox_id", "") or "")
            if not sandbox_id:
                await self._kill_sandbox(sandbox, reason="missing_sandbox_id")
                raise RuntimeError("AGS create 返回的 sandbox 缺少 sandbox_id")
            if sandbox_id in self._created_ids:
                await self._kill_sandbox(sandbox, reason="sandbox_id_reused")
                raise RuntimeError(f"AGS 重用了 sandbox ID: {sandbox_id}")

            self._sandbox = sandbox
            self.sandbox_id = sandbox_id
            self._created_ids.add(sandbox_id)
            try:
                await self._record(
                    "created",
                    sandbox_id=sandbox_id,
                    template=self._template,
                    create_operation_id=create_operation_id,
                )
                if cancellation is not None:
                    raise cancellation
                await self._prepare_role_files()
                await self._record("ready", sandbox_id=sandbox_id)
            except BaseException as exc:
                try:
                    await self._kill_sandbox(
                        sandbox,
                        reason=(
                            "late_create_after_cancel"
                            if isinstance(exc, asyncio.CancelledError)
                            else "start_setup_failed"
                        ),
                    )
                except BaseException as cleanup_exc:
                    self._sandbox = None
                    if sandbox_id in self._terminal_ids:
                        raise exc from cleanup_exc
                    raise SandboxCleanupError(
                        f"启动失败后无法清理 AGS sandbox {sandbox_id}"
                    ) from cleanup_exc
                self._sandbox = None
                raise

    @override
    async def stop(self, delete: bool) -> None:
        async with self._lifecycle_lock:
            sandbox = self._sandbox
            if sandbox is None:
                await self._record("stop_already_absent", delete_requested=delete)
                return
            sandbox_id = str(getattr(sandbox, "sandbox_id", "") or "")
            try:
                await self._kill_sandbox(sandbox, reason="stop")
            except asyncio.CancelledError:
                if sandbox_id in self._terminal_ids:
                    self._sandbox = None
                raise
            except BaseException:
                # 保留引用，使调用方可以再次尝试 stop；异常绝不吞掉。
                raise
            else:
                self._sandbox = None
                await self._record(
                    "stopped",
                    sandbox_id=sandbox_id,
                    delete_requested=delete,
                    provider_is_ephemeral=True,
                )

    def assert_cleanup_verified(self) -> None:
        pending = self.cleanup_unverified_ids
        if pending:
            raise SandboxCleanupError("以下 AGS sandbox 无法确认已删除: " + ", ".join(pending))

    def _require_sandbox(self) -> Any:
        if self._sandbox is None:
            raise RuntimeError("Sandbox 未启动")
        return self._sandbox

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        sandbox = self._require_sandbox()
        effective_user = self._resolve_user(user)
        effective_env = self._merge_env(env)
        effective_cwd = cwd or self.task_env_config.workdir
        if effective_cwd is None:
            effective_cwd = self._workspace_dir if self._role == "agent" else "/tests"

        def run_command() -> Any:
            try:
                return sandbox.commands.run(
                    command,
                    cwd=effective_cwd,
                    envs=effective_env,
                    user=str(effective_user) if effective_user is not None else None,
                    timeout=(
                        timeout_sec if timeout_sec is not None else self._transfer_timeout_sec
                    ),
                    request_timeout=self._request_timeout_sec,
                )
            except BaseException as exc:
                # E2B 对非零退出码抛 CommandExitException；它同时携带完整结果。
                if hasattr(exc, "exit_code"):
                    return exc
                raise

        result = await self._call_sync(
            run_command,
        )
        stdout = getattr(result, "stdout", None)
        stderr = getattr(result, "stderr", None)
        callback = self._output_callback()
        if callback is not None:
            if stdout:
                await callback(stdout, "stdout")
            if stderr:
                await callback(stderr, "stderr")
        return ExecResult(
            stdout=stdout,
            stderr=stderr,
            return_code=int(getattr(result, "exit_code", 0)),
        )

    async def start_background_process(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | int | None = None,
    ) -> int:
        """通过 AGS/E2B 原生后台 API 启动进程并返回 PID。

        ``nohup ... &`` 仍会让 E2B 的前台输出流等待到超时，因此不能用
        ``exec()`` 模拟后台任务。这里取得启动事件中的 PID 后主动断开事件
        流；按照 E2B ``CommandHandle.disconnect()`` 契约，断开不会终止进程。
        """

        sandbox = self._require_sandbox()
        effective_user = self._resolve_user(user)
        effective_env = self._merge_env(env)
        effective_cwd = cwd or self.task_env_config.workdir
        if effective_cwd is None:
            effective_cwd = self._workspace_dir if self._role == "agent" else "/tests"

        def start_command() -> int:
            handle = sandbox.commands.run(
                command,
                background=True,
                cwd=effective_cwd,
                envs=effective_env,
                user=str(effective_user) if effective_user is not None else None,
                timeout=0,
                request_timeout=self._request_timeout_sec,
            )
            pid = getattr(handle, "pid", None)
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                try:
                    handle.kill()
                finally:
                    raise RuntimeError("AGS 后台命令未返回有效 PID")
            try:
                handle.disconnect()
            except BaseException:
                # 已收到启动事件但无法安全断开时，不能留下不可管理进程。
                try:
                    handle.kill()
                finally:
                    raise
            return pid

        return await self._call_sync(start_command)

    async def _remove_remote_temp(self, path: str) -> None:
        sandbox = self._require_sandbox()
        await self._call_sync(
            sandbox.files.remove,
            path,
            user="root",
            request_timeout=self._request_timeout_sec,
        )

    async def _upload_bytes(self, data: bytes, target_path: str) -> None:
        sandbox = self._require_sandbox()
        target = str(_remote_path(target_path, label="target_path"))
        if len(data) > self._max_transfer_bytes:
            raise UnsafeTransferError("上传文件超过大小限制")
        await self._call_sync(
            sandbox.files.write,
            target,
            data,
            user="root",
            request_timeout=self._request_timeout_sec,
        )

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)

        def read_safe() -> bytes:
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise UnsafeTransferError(f"上传源必须是普通文件: {source}")
            if info.st_nlink != 1:
                raise UnsafeTransferError(f"上传源不能是硬链接: {source}")
            if info.st_size > self._max_transfer_bytes:
                raise UnsafeTransferError("上传文件超过大小限制")
            data = source.read_bytes()
            if len(data) != info.st_size or source.lstat().st_ino != info.st_ino:
                raise UnsafeTransferError(f"读取期间上传源发生变化: {source}")
            return data

        data = await self._call_sync(read_safe)
        await self._upload_bytes(data, target_path)

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        sandbox = self._require_sandbox()
        source = Path(source_dir)
        target_path = _remote_path(target_dir, label="target_dir")
        if target_path == PurePosixPath("/"):
            raise UnsafeTransferError("目录上传目标不能是 sandbox 根目录")
        target = str(target_path)
        archive = await self._call_sync(
            _pack_safe_directory,
            source,
            max_files=self._max_transfer_files,
            max_bytes=self._max_transfer_bytes,
        )
        remote_archive = f"/tmp/.harbor-ags-upload-{os.urandom(12).hex()}.tar.gz"
        await self._call_sync(
            sandbox.files.write,
            remote_archive,
            archive,
            user="root",
            request_timeout=self._request_timeout_sec,
        )
        primary_error: BaseException | None = None
        try:
            verifier_permissions = (
                # Agent 的公开来源继续保持 root 所有；只有独立 verifier
                # 需要读取 Harbor 从 Agent 重物化的 0600 Artifact。
                f" && chmod -R a+rX -- {shlex.quote(target)}" if self._role == "verifier" else ""
            )
            command = (
                f"mkdir -p {shlex.quote(target)} && "
                f"tar -xzf {shlex.quote(remote_archive)} --no-same-owner "
                f"-C {shlex.quote(target)}{verifier_permissions}"
            )
            result = await self.exec(
                command,
                cwd="/",
                timeout_sec=self._transfer_timeout_sec,
                user="root",
            )
            if result.return_code != 0:
                raise RuntimeError(
                    f"AGS 目录上传解包失败: {result.stderr or result.stdout or 'no output'}"
                )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                await self._remove_remote_temp(remote_archive)
            except BaseException:
                if primary_error is None:
                    raise
                self.logger.exception("上传失败后清理远端临时归档也失败")

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        sandbox = self._require_sandbox()
        source = str(_remote_path(source_path, label="source_path"))
        target = Path(target_path)
        check = await self.exec(
            f"test -f {shlex.quote(source)} && test ! -L {shlex.quote(source)} "
            f"&& stat -c %s {shlex.quote(source)}",
            cwd="/",
            timeout_sec=self._transfer_timeout_sec,
            user="root",
        )
        if check.return_code != 0:
            raise UnsafeTransferError(f"下载源不是非链接普通文件: {source}")
        try:
            expected_size = int((check.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise UnsafeTransferError(f"无法取得下载源大小: {source}") from exc
        if expected_size > self._max_transfer_bytes:
            raise UnsafeTransferError("下载文件超过大小限制")
        data = await self._call_sync(
            sandbox.files.read,
            source,
            format="bytes",
            user="root",
            request_timeout=self._request_timeout_sec,
        )
        if not isinstance(data, (bytes, bytearray)) or len(data) != expected_size:
            raise UnsafeTransferError(f"下载文件长度不一致: {source}")
        await self._call_sync(_atomic_write, target, bytes(data))

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        sandbox = self._require_sandbox()
        source = str(_remote_path(source_dir, label="source_dir"))
        target = Path(target_dir)
        remote_archive = f"/tmp/.harbor-ags-download-{os.urandom(12).hex()}.tar.gz"
        command = (
            f"test -d {shlex.quote(source)} && test ! -L {shlex.quote(source)} && "
            f"tar -czf {shlex.quote(remote_archive)} -C {shlex.quote(source)} ."
        )
        result = await self.exec(
            command,
            cwd="/",
            timeout_sec=self._transfer_timeout_sec,
            user="root",
        )
        if result.return_code != 0:
            raise UnsafeTransferError(
                f"下载源目录无法安全打包: {result.stderr or result.stdout or source}"
            )
        primary_error: BaseException | None = None
        try:
            data = await self._call_sync(
                sandbox.files.read,
                remote_archive,
                format="bytes",
                user="root",
                request_timeout=self._request_timeout_sec,
            )
            if not isinstance(data, (bytes, bytearray)):
                raise UnsafeTransferError("AGS SDK 未按二进制返回目录归档")
            data = bytes(data)
            # 压缩包自身也设硬上限；解压后的严格上限由成员元数据再次校验。
            if len(data) > self._max_transfer_bytes + 4 * 1024 * 1024:
                raise UnsafeTransferError("远端目录压缩包超过传输限制")
            await self._call_sync(
                _extract_safe_tar,
                data,
                target,
                max_files=self._max_transfer_files,
                max_bytes=self._max_transfer_bytes,
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                await self._remove_remote_temp(remote_archive)
            except BaseException:
                if primary_error is None:
                    raise
                self.logger.exception("下载失败后清理远端临时归档也失败")

    async def inject_trial_input(
        self,
        input_path: Path | str,
        manifest_path: Path | str | None = None,
    ) -> None:
        """把同一份控制端输入投影给 Agent 或独立 Verifier。

        Agent 只能获得 ``input.json``；Verifier 同时获得可信 manifest。传入
        manifest 时先在宿主侧核对哈希和长度，拒绝不一致的控制面输入。
        """

        input_file = Path(input_path)
        manifest_file = Path(manifest_path) if manifest_path is not None else None
        payload = await self._call_sync(input_file.read_bytes)
        manifest_payload: bytes | None = None
        if manifest_file is not None:
            manifest_payload = await self._call_sync(manifest_file.read_bytes)
            try:
                manifest = json.loads(manifest_payload)
                if not isinstance(manifest, dict) or not manifest.get("schema_version"):
                    raise ValueError
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise UnsafeTransferError("控制端 input manifest 格式无效") from exc
            expected_hash = manifest.get("sha256")
            expected_size = manifest.get("size_bytes")
            actual_hash = hashlib.sha256(payload).hexdigest()
            if expected_hash != actual_hash or expected_size != len(payload):
                raise UnsafeTransferError("控制端 input manifest 与 input.json 不一致")

        if self._role == "agent":
            await self._upload_bytes(payload, f"{self._workspace_dir}/input.json")
            return

        await self._upload_bytes(payload, "/tests/control/input.json")
        if manifest_payload is None:
            raise UnsafeTransferError("Verifier 动态输入必须提供 input manifest")
        await self._upload_bytes(manifest_payload, "/tests/control/input-manifest.json")


__all__ = [
    "AGSPrebuiltEnvironment",
    "SandboxCleanupError",
    "SandboxLedgerAudit",
    "UnsafeTransferError",
    "assert_all_sandboxes_deleted",
    "audit_sandbox_ledger",
]
