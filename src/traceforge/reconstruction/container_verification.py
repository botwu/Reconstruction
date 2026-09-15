"""Terminal-Universe 的容器内 completion、sufficiency 和 RED 校准协议。

该模块故意只依赖一个很窄的异步 runtime 接口。`harbor_ags.environment
AGSPrebuiltEnvironment` 可以通过 adapter 实现该接口，单测则使用 fake runtime。
这样模型不会收到宿主机 workspace 快照，所有读写和测试都发生在沙盒内。
"""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ContainerVerificationError(RuntimeError):
    """容器阶段违反了可审计边界。"""


class AGSRuntimeAdapter:
    """将 Harbor/AGS 的 ``AGSPrebuiltEnvironment`` 映射为本模块的 runtime。

    适配器不在模块导入时依赖 harbor 包，便于本地 fake runtime 单测；实际运行时传入已经
    构造好的 AGSPrebuiltEnvironment。只读语义由 ``run_sufficiency_container`` 上传后
    的 chmod 和写入探针共同保证。
    """

    def __init__(self, environment: Any) -> None:
        self.environment = environment

    async def start(self, *, read_only: bool = False) -> None:
        del read_only
        await self.environment.start(force_build=False)

    async def stop(self, *, delete: bool = True) -> None:
        await self.environment.stop(delete=delete)
        audit = getattr(self.environment, "assert_cleanup_verified", None)
        if audit is not None:
            result = audit()
            if result is False:
                raise ContainerVerificationError("AGS_SANDBOX_CLEANUP_UNVERIFIED")

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_sec: int | None = None,
        user: str | None = None,
    ) -> Any:
        return await self.environment.exec(
            command, cwd=cwd or "/", timeout_sec=timeout_sec or 30, user=user or "user"
        )

    async def upload_dir(self, source_dir: Path, target_dir: str) -> None:
        await self.environment.upload_dir(source_dir, target_dir)

    async def download_dir(self, source_dir: str, target_dir: Path) -> None:
        await self.environment.download_dir(source_dir, target_dir)


class ContainerRuntime(Protocol):
    async def start(self, *, read_only: bool = False) -> None: ...

    async def stop(self, *, delete: bool = True) -> None: ...

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_sec: int | None = None,
        user: str | None = None,
    ) -> Any: ...

    async def upload_dir(self, source_dir: Path, target_dir: str) -> None: ...

    async def download_dir(self, source_dir: str, target_dir: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class ContainerTestRun:
    name: str
    status: str
    stdout: str = ""
    stderr: str = ""
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class CompletionContainerResult:
    status: str
    workspace: Path | None
    added_files: tuple[str, ...]
    unchanged_files: tuple[str, ...]
    errors: tuple[str, ...]
    agent_output: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SufficiencyContainerResult:
    label: str
    confidence: float
    reason: str
    missing_critical: tuple[str, ...]
    write_blocked: bool
    errors: tuple[str, ...]
    agent_output: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RedCalibrationResult:
    status: str
    missing_runs: tuple[ContainerTestRun, ...]
    protective_runs: tuple[ContainerTestRun, ...]
    errors: tuple[str, ...]


AgentRunner = Callable[[ContainerRuntime, str], Awaitable[dict[str, Any]]]
JudgeRunner = Callable[[ContainerRuntime, str], Awaitable[dict[str, Any]]]
TestRunner = Callable[[ContainerRuntime, tuple[str, ...]], Awaitable[tuple[ContainerTestRun, ...]]]


def _tree_hash(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ContainerVerificationError(f"workspace 不允许符号链接：{path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


async def run_completion_container(
    *,
    runtime: ContainerRuntime,
    replay_workspace: Path,
    task_prompt: str,
    evidence_refs: set[str],
    output_workspace: Path,
    agent_runner: AgentRunner,
) -> CompletionContainerResult:
    """在 agent 沙盒内完成 workspace，再把结果安全下载并检查。"""

    if not replay_workspace.is_dir():
        raise ContainerVerificationError(f"replay workspace 不存在：{replay_workspace}")
    before = _tree_hash(replay_workspace)
    errors: list[str] = []
    agent_output: dict[str, Any] = {}
    started = False
    try:
        await runtime.start(read_only=False)
        started = True
        await runtime.upload_dir(replay_workspace, "/home/user/workspace")
        prompt = (
            task_prompt
            + "\nThe workspace is /home/user/workspace. Use shell/file tools inside the container. "
            "Only add evidence-backed context; never implement the task or write tests/solutions. "
            "Return a JSON manifest with evidence_ref_ids for every added file."
        )
        agent_output = await agent_runner(runtime, prompt)
        output_workspace.parent.mkdir(parents=True, exist_ok=True)
        await runtime.download_dir("/home/user/workspace", output_workspace)
    except BaseException as exc:
        errors.append(f"CONTAINER_AGENT_ERROR:{type(exc).__name__}:{exc}")
    finally:
        if started:
            try:
                await runtime.stop(delete=True)
            except BaseException as exc:
                errors.append(f"CONTAINER_CLEANUP_ERROR:{type(exc).__name__}:{exc}")
    if errors:
        return CompletionContainerResult("REVIEW", None, (), (), tuple(errors), agent_output)

    after = _tree_hash(output_workspace)
    unchanged = tuple(sorted(path for path, digest in before.items() if after.get(path) == digest))
    changed = tuple(sorted(path for path in after if before.get(path) != after[path]))
    if set(before) - set(after):
        errors.append("REPLAY_FILE_DELETED")
    modified = tuple(sorted(path for path in before if path in after and before[path] != after[path]))
    if modified:
        errors.append("REPLAY_FILE_MUTATED:" + ",".join(modified))
    forbidden = {"tests", "solution", "hidden_control", ".git"}
    if any(Path(path).parts and Path(path).parts[0] in forbidden for path in changed):
        errors.append("FORBIDDEN_PUBLIC_PATH")
    refs_by_path = agent_output.get("evidence_ref_ids_by_path", {})
    if not isinstance(refs_by_path, dict):
        refs_by_path = {}
    for path in changed:
        refs = refs_by_path.get(path)
        if not isinstance(refs, list) or not refs or any(str(ref) not in evidence_refs for ref in refs):
            errors.append(f"EVIDENCE_REF_UNKNOWN:{path}")
    return CompletionContainerResult(
        "READY" if not errors else "REVIEW",
        output_workspace if not errors else None,
        changed,
        unchanged,
        tuple(errors),
        agent_output,
    )


async def run_sufficiency_container(
    *,
    runtime: ContainerRuntime,
    workspace: Path,
    task_prompt: str,
    judge_runner: JudgeRunner,
) -> SufficiencyContainerResult:
    """让 judge 在只读容器内主动 inspect，而不是读取宿主机快照。"""

    errors: list[str] = []
    output: dict[str, Any] = {}
    started = False
    try:
        await runtime.start(read_only=True)
        started = True
        await runtime.upload_dir(workspace, "/home/user/workspace")
        chmod = await runtime.exec(
            "chmod -R a-w /home/user/workspace && find /home/user/workspace -type f -exec chmod a+r {} +",
            cwd="/",
            timeout_sec=30,
            user="root",
        )
        if getattr(chmod, "return_code", 1) != 0:
            errors.append("READ_ONLY_SETUP_FAILED")
        probe = await runtime.exec(
            "touch /home/user/workspace/.traceforge-write-probe",
            cwd="/",
            timeout_sec=10,
            user="user",
        )
        write_blocked = getattr(probe, "return_code", 1) != 0
        if not write_blocked:
            errors.append("JUDGE_WORKSPACE_WRITEABLE")
        output = await judge_runner(
            runtime,
            task_prompt
            + "\nInspect /home/user/workspace actively with read-only shell/file tools. "
            "Do not modify files. Return label sufficient|insufficient, confidence, reason, "
            "present and missing_critical as JSON.",
        )
    except BaseException as exc:
        errors.append(f"CONTAINER_JUDGE_ERROR:{type(exc).__name__}:{exc}")
        write_blocked = False
    finally:
        if started:
            try:
                await runtime.stop(delete=True)
            except BaseException as exc:
                errors.append(f"CONTAINER_CLEANUP_ERROR:{type(exc).__name__}:{exc}")
    label = str(output.get("label", "unknown")).lower()
    if label not in {"sufficient", "insufficient"}:
        errors.append("INVALID_SUFFICIENCY_LABEL")
        label = "unknown"
    try:
        confidence = float(output.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
        errors.append("INVALID_CONFIDENCE")
    if not 0.0 <= confidence <= 1.0:
        errors.append("INVALID_CONFIDENCE")
        confidence = 0.0
    missing = output.get("missing_critical", [])
    missing = tuple(str(item) for item in missing) if isinstance(missing, list) else ()
    return SufficiencyContainerResult(
        label,
        confidence,
        str(output.get("reason", "")),
        missing,
        write_blocked,
        tuple(errors),
        output,
    )


async def pytest_test_runner(
    runtime: ContainerRuntime,
    test_names: tuple[str, ...],
    *,
    test_file: str = "/tests/test_outputs.py",
    workspace: str = "/home/user/workspace",
    timeout_sec: int = 120,
) -> tuple[ContainerTestRun, ...]:
    """在 verifier AGS 沙盒中逐个执行 pytest；用于初始 RED calibration。"""
    if not test_file.startswith("/") or not workspace.startswith("/"):
        raise ContainerVerificationError("test_file/workspace 必须是绝对路径")
    runs: list[ContainerTestRun] = []
    for name in test_names:
        if not name.startswith("test_") or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in name):
            runs.append(ContainerTestRun(name, "INFRA_ERROR", error_code="INVALID_TEST_NAME"))
            continue
        command = (
            f"TRACEFORGE_WORKSPACE={shlex.quote(workspace)} python -m pytest -q "
            f"{shlex.quote(test_file)}::{shlex.quote(name)}"
        )
        try:
            result = await runtime.exec(command, cwd="/", timeout_sec=timeout_sec, user="root")
            code = getattr(result, "return_code", None)
            stdout = str(getattr(result, "stdout", "") or "")
            stderr = str(getattr(result, "stderr", "") or "")
            if code == 0:
                status = "PASS"
                error_code = None
            elif code == 5:
                status = "INFRA_ERROR"
                error_code = "NO_TESTS_COLLECTED"
            else:
                status = "FAIL"
                error_code = f"PYTEST_EXIT_{code}"
            runs.append(ContainerTestRun(name, status, stdout, stderr, error_code))
        except TimeoutError:
            runs.append(ContainerTestRun(name, "TIMEOUT", error_code="TIMEOUT"))
        except BaseException as exc:
            runs.append(ContainerTestRun(name, "INFRA_ERROR", error_code=f"{type(exc).__name__}:{exc}"))
    return tuple(runs)


async def run_verifier_red_calibration(
    *,
    runtime: ContainerRuntime,
    test_runner: TestRunner,
    missing_test_names: tuple[str, ...],
    protective_test_names: tuple[str, ...],
) -> RedCalibrationResult:
    """在初始 workspace 内执行论文 D 的 RED calibration。"""

    missing = await test_runner(runtime, missing_test_names)
    protective = await test_runner(runtime, protective_test_names)
    errors: list[str] = []
    if not missing_test_names or not protective_test_names:
        errors.append("RED_TEST_GROUP_EMPTY")
    if any(run.status in {"INFRA_ERROR", "ERROR", "TIMEOUT"} for run in (*missing, *protective)):
        errors.append("RED_INFRA_ERROR")
    if not missing or not all(run.status == "FAIL" for run in missing):
        errors.append("MISSING_CAPABILITY_DID_NOT_FAIL")
    if not protective or not all(run.status == "PASS" for run in protective):
        errors.append("PROTECTIVE_TEST_DID_NOT_PASS")
    return RedCalibrationResult(
        "READY" if not errors else "REVIEW",
        missing,
        protective,
        tuple(errors),
    )


__all__ = [
    "AGSRuntimeAdapter",
    "CompletionContainerResult",
    "ContainerRuntime",
    "ContainerTestRun",
    "ContainerVerificationError",
    "RedCalibrationResult",
    "SufficiencyContainerResult",
    "pytest_test_runner",
    "run_completion_container",
    "run_sufficiency_container",
    "run_verifier_red_calibration",
]
