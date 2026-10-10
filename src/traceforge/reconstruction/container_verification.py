"""Terminal-Universe 的容器内 completion、sufficiency 和 RED 校准协议。

该模块故意只依赖一个很窄的异步 runtime 接口。`harbor_ags.environment
AGSPrebuiltEnvironment` 可以通过 adapter 实现该接口，单测则使用 fake runtime。
这样模型不会收到宿主机 workspace 快照，所有读写和测试都发生在沙盒内。
"""

from __future__ import annotations

import hashlib
import os
import shlex
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from traceforge.reconstruction.model_gateway import load_e2b_api_key
from traceforge.reconstruction.tls import pin_process_tls

READ_ONLY_WORKSPACE_COMMAND = "chmod -R a+rX,a-w /home/user/workspace"


class ContainerVerificationError(RuntimeError):
    """容器阶段违反了可审计边界。"""


class SandboxUnavailableError(RuntimeError):
    """--sandbox 缺少凭据或无法构造 AGS runtime。"""


def resolve_sandbox_api_key() -> str | None:
    for name in ("AGS_API_KEY", "E2B_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _ensure_harbor_import(harbor_root: Path) -> None:
    src = harbor_root / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    for site in (
        harbor_root / ".venv/lib/python3.12/site-packages",
        harbor_root / ".venv/lib64/python3.12/site-packages",
    ):
        if site.is_dir() and str(site) not in sys.path:
            sys.path.insert(0, str(site))


def build_ags_runtime_factory(
    *,
    harbor_root: str | Path,
    output_root: str | Path,
    config_path: str | Path | None = None,
) -> Callable[[], "AGSRuntimeAdapter"]:
    """Construct one AGS sandbox per role. Missing keys fail before any Agent starts."""

    pin_process_tls()
    api_key = resolve_sandbox_api_key()
    if not api_key:
        candidate = Path(config_path) if config_path is not None else Path(__file__).resolve().parents[3] / "config.yaml"
        if candidate.is_file():
            try:
                api_key = load_e2b_api_key(candidate)
            except Exception as exc:
                raise SandboxUnavailableError(f"无法读取 AGS 配置：{type(exc).__name__}") from exc
    if not api_key:
        raise SandboxUnavailableError("--sandbox 需要 AGS_API_KEY 或 E2B_API_KEY")
    root = Path(harbor_root)
    dest = Path(output_root)
    template = os.environ.get("AGS_TEMPLATE_ID", "").strip() or os.environ.get(
        "ROLLOUT_E2B_TEMPLATE", "node-python-hermes"
    ).strip()
    domain = os.environ.get("AGS_DOMAIN", "").strip() or os.environ.get(
        "E2B_DOMAIN", "ap-beijing.tencentags.com"
    ).strip()

    def factory() -> AGSRuntimeAdapter:
        _ensure_harbor_import(root)
        try:
            from harbor.models.task.config import EnvironmentConfig
            from harbor.models.trial.paths import TrialPaths
            from harbor_ags.environment import AGSPrebuiltEnvironment
        except ImportError as exc:
            raise SandboxUnavailableError(f"无法导入 AGS 环境：{exc}") from exc
        stamp = uuid.uuid4().hex
        env_dir = dest / "ags_environment" / stamp
        trial_dir = dest / "ags_trial" / stamp
        env_dir.mkdir(parents=True, exist_ok=True)
        # AGSPrebuiltEnvironment validates the public source before start.
        # The role runtime uploads the actual workspace after construction.
        (env_dir.parent / "workspace").mkdir(parents=True, exist_ok=True)
        trial_dir.mkdir(parents=True, exist_ok=True)
        paths = TrialPaths(trial_dir)
        paths.mkdir()
        environment = AGSPrebuiltEnvironment(
            environment_dir=env_dir,
            environment_name="reconstruct",
            session_id=f"reconstruct-{stamp}__env",
            trial_paths=paths,
            task_env_config=EnvironmentConfig(),
            template=template,
            domain=domain,
            api_key=api_key,
            # Hermes rollout is one long-running command. The Harbor adapter
            # defaults to a 120s transfer timeout, which kills a valid model
            # call before hermes-result.json is written and surfaces as a
            # misleading TrajectoryCaptureError.
            transfer_timeout_sec=900,
            request_timeout_sec=900,
        )
        return AGSRuntimeAdapter(environment)

    return factory


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
    input_sha256: str | None = None
    input_unchanged: bool | None = None


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
            READ_ONLY_WORKSPACE_COMMAND,
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


def _workspace_digest_command(workspace: str) -> str:
    script = (
        "import hashlib,json,os,sys; from pathlib import Path; "
        "root=Path(sys.argv[1]); "
        "rows=[(p.relative_to(root).as_posix(), "
        "('link:'+os.readlink(p)) if p.is_symlink() else "
        "(hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'dir')) "
        "for p in sorted(root.rglob('*'))]; "
        "print(hashlib.sha256(json.dumps(rows).encode()).hexdigest())"
    )
    return f"python3 -c {shlex.quote(script)} {shlex.quote(workspace)}"


_PYTEST_RUNNER = (
    "import sys; "
    "sys.path.insert(0, sys.argv[1]); "
    "import pytest; "
    "raise SystemExit(pytest.main(sys.argv[2:]))"
)


def _pytest_infrastructure_failure(stdout: str, stderr: str) -> bool:
    text = (stdout + "\n" + stderr).lower()
    return any(
        marker in text
        for marker in (
            "error collecting",
            "importerror while importing",
            "importerror:",
            "modulenotfounderror:",
            "no module named",
            "cannot import name",
            "pytest: command not found",
        )
    )


async def pytest_test_runner(
    runtime: ContainerRuntime,
    test_names: tuple[str, ...],
    *,
    test_file: str = "/tests/test_outputs.py",
    workspace: str = "/home/user/workspace",
    pytest_site: str = "/tests/site-packages",
    timeout_sec: int = 120,
) -> tuple[ContainerTestRun, ...]:
    """在独立副本中运行每个测试；原始输入与副本都不得被测试改写。"""
    if not test_file.startswith("/") or not workspace.startswith("/"):
        raise ContainerVerificationError("test_file/workspace 必须是绝对路径")
    runs: list[ContainerTestRun] = []
    for name in test_names:
        if not name.startswith("test_") or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in name):
            runs.append(ContainerTestRun(name, "INFRA_ERROR", error_code="INVALID_TEST_NAME"))
            continue
        disposable = "/tmp/traceforge-verifier-" + uuid.uuid4().hex
        before_digest: str | None = None
        try:
            before = await runtime.exec(_workspace_digest_command(workspace), cwd="/", timeout_sec=30, user="user")
            before_digest = str(getattr(before, "stdout", "")).strip()
            if getattr(before, "return_code", 1) != 0 or len(before_digest) != 64:
                runs.append(ContainerTestRun(name, "INFRA_ERROR", error_code="VERIFIER_INPUT_HASH_FAILED"))
                continue
            # AGS 验证镜像不预装 pytest；验证角色把锁定的离线发行包放在
            # /tests/site-packages。隔离 Python 通过参数注入该路径，不受
            # PYTHONPATH 或插件自动加载影响。
            command = (
                f"cp -a {shlex.quote(workspace)} {shlex.quote(disposable)} && "
                f"TRACEFORGE_WORKSPACE={shlex.quote(disposable)} "
                f"PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "
                f"python3 -I -c {shlex.quote(_PYTEST_RUNNER)} "
                f"{shlex.quote(pytest_site)} "
                f"{shlex.quote(test_file)}::{shlex.quote(name)} "
                "-p no:cacheprovider -q"
            )
            result = await runtime.exec(command, cwd="/", timeout_sec=timeout_sec, user="user")
            after = await runtime.exec(_workspace_digest_command(workspace), cwd="/", timeout_sec=30, user="user")
            copy_after = await runtime.exec(_workspace_digest_command(disposable), cwd="/", timeout_sec=30, user="user")
            unchanged = all(
                getattr(item, "return_code", 1) == 0
                and str(getattr(item, "stdout", "")).strip() == before_digest
                for item in (after, copy_after)
            )
            stdout = str(getattr(result, "stdout", "") or "")
            stderr = str(getattr(result, "stderr", "") or "")
            if not unchanged:
                runs.append(ContainerTestRun(name, "INFRA_ERROR", stdout, stderr, "VERIFIER_INPUT_MUTATED", before_digest, False))
                continue
            code = getattr(result, "return_code", None)
            if "PermissionError:" in stdout + stderr or "Read-only file system" in stdout + stderr:
                runs.append(ContainerTestRun(name, "INFRA_ERROR", stdout, stderr, "VERIFIER_INPUT_WRITE_DENIED", before_digest, True))
                continue
            if code == 0:
                status, error_code = "PASS", None
            elif code == 1 and _pytest_infrastructure_failure(stdout, stderr):
                status, error_code = "INFRA_ERROR", "PYTEST_IMPORT_ERROR"
            elif code == 1:
                status, error_code = "FAIL", "PYTEST_EXIT_1"
            else:
                status = "INFRA_ERROR"
                error_code = "NO_TESTS_COLLECTED" if code == 5 else f"PYTEST_EXIT_{code}"
            runs.append(ContainerTestRun(name, status, stdout, stderr, error_code, before_digest, True))
        except TimeoutError:
            runs.append(ContainerTestRun(name, "TIMEOUT", error_code="TIMEOUT", input_sha256=before_digest))
        except Exception as exc:
            runs.append(ContainerTestRun(name, "INFRA_ERROR", error_code=f"{type(exc).__name__}:{exc}", input_sha256=before_digest))
        finally:
            try:
                # The copy is owned by the test user. Restore writable directory
                # permissions before removing a copy of the read-only input.
                await runtime.exec(f"if [ -d {shlex.quote(disposable)} ]; then chmod -R u+w {shlex.quote(disposable)}; rm -rf {shlex.quote(disposable)}; fi", cwd="/", timeout_sec=30, user="user")
            except Exception:
                pass
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
    "SandboxUnavailableError",
    "SufficiencyContainerResult",
    "build_ags_runtime_factory",
    "pytest_test_runner",
    "resolve_sandbox_api_key",
    "run_completion_container",
    "run_sufficiency_container",
    "run_verifier_red_calibration",
]
