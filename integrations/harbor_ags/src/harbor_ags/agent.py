"""使用预装 Hermes 和冻结 Harness 的 Harbor Agent。"""

from __future__ import annotations

import hashlib
import json
import shlex
import time
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .environment import AGSPrebuiltEnvironment
from .exceptions import HarnessDriftError, TrajectoryCaptureError
from .input_contract import (
    TASK_BUNDLE_INPUT_PROFILE,
    instruction_components,
    render_runtime_appendix,
)

_PACKAGE_DIR = Path(__file__).resolve().parent
_ANTHROPIC_VERSION = "0.86.0"
_ANTHROPIC_WHEEL = "anthropic-0.86.0-py3-none-any.whl"
_ANTHROPIC_WHEEL_SHA256 = "9d2bbd339446acce98858c5627d33056efe01f70435b22b63546fe7edae0cd57"
_DOCSTRING_PARSER_VERSION = "0.17.0"
_DOCSTRING_PARSER_WHEEL = "docstring_parser-0.17.0-py3-none-any.whl"
_DOCSTRING_PARSER_WHEEL_SHA256 = "cf2569abd23dce8099b300f9b4fa8191e9582dda731fd533daf54c4551658708"
_REMOTE_VENDOR_DIR = "/tmp/harbor_ags_runtime/vendor"
_REMOTE_ANTHROPIC_WHEEL = f"{_REMOTE_VENDOR_DIR}/{_ANTHROPIC_WHEEL}"
_REMOTE_DOCSTRING_PARSER_WHEEL = f"{_REMOTE_VENDOR_DIR}/{_DOCSTRING_PARSER_WHEEL}"
_REMOTE_SITE_PACKAGES = f"{_REMOTE_VENDOR_DIR}/site-packages"
_VENDORED_WHEELS = (
    (_ANTHROPIC_WHEEL, _ANTHROPIC_WHEEL_SHA256, _REMOTE_ANTHROPIC_WHEEL),
    (
        _DOCSTRING_PARSER_WHEEL,
        _DOCSTRING_PARSER_WHEEL_SHA256,
        _REMOTE_DOCSTRING_PARSER_WHEEL,
    ),
)
_SANDBOX_MODEL_ENV_UNSET = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "TOKENHUB_KEY",
    "TOKENHUB_BASE_URL",
    "OPENAI_API_KEY",
)


class LosslessHermesAgent(BaseInstalledAgent):
    """在 AGS 预置模板中运行 Hermes，并严格生成两层轨迹。"""

    SUPPORTS_ATIF = True

    def __init__(
        self,
        *args: Any,
        expected_commit: str | None = None,
        hermes_executable: str = "/home/user/.local/bin/hermes",
        hermes_source_dir: str = "/home/user/.hermes/hermes-agent",
        workspace: str = "/home/user/workspace",
        capture_port: int = 8788,
        capture_timeout_sec: float = 300.0,
        max_iterations: int = 30,
        toolsets: str = "file,terminal",
        input_profile: str = TASK_BUNDLE_INPUT_PROFILE,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.expected_commit = expected_commit
        self.hermes_executable = hermes_executable
        self.hermes_source_dir = hermes_source_dir
        self.workspace = workspace
        self.capture_port = int(capture_port)
        self.capture_timeout_sec = float(capture_timeout_sec)
        if self.capture_timeout_sec <= 0:
            raise ValueError("capture_timeout_sec must be positive")
        self.max_iterations = int(max_iterations)
        self.toolsets = toolsets
        if input_profile != TASK_BUNDLE_INPUT_PROFILE:
            raise ValueError(f"不支持的 input_profile: {input_profile}")
        self.input_profile = input_profile
        self._observed_commit: str | None = None
        self._capture_pid: int | None = None
        self._run_error: BaseException | None = None
        # Harbor 只会在 AgentContext 仍为空时调用 populate_context_post_run()。
        # 因此运行阶段把元数据暂存在实例上，待 /logs/agent 下载后一次性填充。
        self._run_metadata: dict[str, Any] = {}

    @staticmethod
    @override
    def name() -> str:
        return "lossless-hermes"

    @override
    def get_version_command(self) -> str | None:
        return f"{shlex.quote(self.hermes_executable)} version"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """认证预装 Hermes；该集成禁止运行时在线安装。"""
        executable = shlex.quote(self.hermes_executable)
        source_dir = shlex.quote(self.hermes_source_dir)
        result = await self.exec_as_agent(
            environment,
            command=(
                f"test -x {executable} && test -d {source_dir} && "
                f'test "$(sha256sum {shlex.quote(_REMOTE_ANTHROPIC_WHEEL)} '
                f"| cut -d' ' -f1)\" = {shlex.quote(_ANTHROPIC_WHEEL_SHA256)} && "
                f'test "$(sha256sum {shlex.quote(_REMOTE_DOCSTRING_PARSER_WHEEL)} '
                f"| cut -d' ' -f1)\" = {shlex.quote(_DOCSTRING_PARSER_WHEEL_SHA256)} && "
                f"{executable} version && git -C {source_dir} rev-parse HEAD && "
                f"cd {source_dir} && python3 -c '"
                "import importlib.metadata as m; import anthropic; "
                "from run_agent import AIAgent; "
                "from agent.anthropic_adapter import build_anthropic_client; "
                f'assert m.version("anthropic") == "{_ANTHROPIC_VERSION}"; '
                'assert m.version("docstring-parser") == '
                f'"{_DOCSTRING_PARSER_VERSION}"; '
                f'assert str(anthropic.__file__).startswith("{_REMOTE_SITE_PACKAGES}/"); '
                'build_anthropic_client("probe-only", "http://127.0.0.1:9", timeout=1); '
                'print("anthropic=" + m.version("anthropic"))\''
            ),
            env={"PYTHONPATH": _REMOTE_SITE_PACKAGES},
            timeout_sec=30,
        )
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        commits = [
            line for line in lines if len(line) == 40 and all(c in "0123456789abcdef" for c in line)
        ]
        self._observed_commit = commits[-1] if commits else None
        if self.expected_commit and self._observed_commit != self.expected_commit:
            raise HarnessDriftError(
                "Hermes commit 不匹配："
                f"expected={self.expected_commit} observed={self._observed_commit}"
            )

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=(
                "mkdir -p /tmp/harbor_ags_runtime/harbor_ags "
                f"{shlex.quote(_REMOTE_VENDOR_DIR)} "
                "/logs/agent /logs/artifacts/traceforge "
                f"{shlex.quote(self.workspace)} && "
                "chown -R user:user /tmp/harbor_ags_runtime /logs "
                f"{shlex.quote(self.workspace)}"
            ),
            timeout_sec=20,
        )

        for filename in ("__init__.py", "exceptions.py", "capture.py"):
            source = _PACKAGE_DIR / filename
            if not source.is_file():
                raise FileNotFoundError(f"缺少 sandbox runtime 文件：{source}")
            await environment.upload_file(
                source,
                f"/tmp/harbor_ags_runtime/harbor_ags/{filename}",
            )

        resource = files("harbor_ags.resources").joinpath("hermes_harness.py")
        with as_file(resource) as harness_path:
            await environment.upload_file(
                harness_path,
                "/tmp/harbor_ags_runtime/hermes_harness.py",
            )

        for filename, expected_hash, remote_path in _VENDORED_WHEELS:
            wheel_resource = files("harbor_ags.resources").joinpath(filename)
            with as_file(wheel_resource) as wheel_path:
                wheel_bytes = wheel_path.read_bytes()
                observed_hash = hashlib.sha256(wheel_bytes).hexdigest()
                if observed_hash != expected_hash:
                    raise HarnessDriftError(
                        f"运行依赖 wheel 哈希不匹配 ({filename})："
                        f"expected={expected_hash} observed={observed_hash}"
                    )
                await environment.upload_file(wheel_path, remote_path)

        extract = await self.exec_as_root(
            environment,
            command=(
                f'test "$(sha256sum {shlex.quote(_REMOTE_ANTHROPIC_WHEEL)} '
                f"| cut -d' ' -f1)\" = {shlex.quote(_ANTHROPIC_WHEEL_SHA256)} && "
                f'test "$(sha256sum {shlex.quote(_REMOTE_DOCSTRING_PARSER_WHEEL)} '
                f"| cut -d' ' -f1)\" = {shlex.quote(_DOCSTRING_PARSER_WHEEL_SHA256)} && "
                f"mkdir -p {shlex.quote(_REMOTE_SITE_PACKAGES)} && "
                f"python3 -m zipfile -e {shlex.quote(_REMOTE_ANTHROPIC_WHEEL)} "
                f"{shlex.quote(_REMOTE_SITE_PACKAGES)} && "
                f"python3 -m zipfile -e {shlex.quote(_REMOTE_DOCSTRING_PARSER_WHEEL)} "
                f"{shlex.quote(_REMOTE_SITE_PACKAGES)}"
            ),
            timeout_sec=30,
        )
        if extract.return_code != 0:
            raise HarnessDriftError("Anthropic wheel 离线展开失败")

        # install() 只认证预装 Hermes 和已上传的哈希锁定 wheel；不会联网安装。
        await super().setup(environment)

    def _model_settings(self) -> tuple[str, str, str]:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("model_name 必须为 provider/model")
        provider, model = self.model_name.split("/", 1)
        # Hermes 使用 Anthropic Messages wire protocol；审计来源必须取自
        # model_name 的 provider 前缀，不能一律写成 anthropic。
        if not provider.strip() or not model.strip():
            raise ValueError("model_name 必须是受支持 provider/model")
        upstream = self._get_env("ANTHROPIC_BASE_URL", "TOKENHUB_BASE_URL")
        api_key = self._get_env(
            "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "TOKENHUB_KEY"
        )
        if not upstream:
            raise ValueError("缺少 ANTHROPIC_BASE_URL")
        if not api_key:
            raise ValueError("缺少 ANTHROPIC_API_KEY")
        return model, upstream.rstrip("/"), api_key

    async def _start_capture(
        self,
        environment: BaseEnvironment,
        *,
        upstream: str,
        api_key: str,
    ) -> None:
        env = {
            "PYTHONPATH": "/tmp/harbor_ags_runtime",
            "CAPTURE_LISTEN_HOST": "127.0.0.1",
            "CAPTURE_LISTEN_PORT": str(self.capture_port),
            "CAPTURE_UPSTREAM_BASE_URL": upstream,
            "CAPTURE_UPSTREAM_API_KEY": api_key,
            "CAPTURE_EVIDENCE_DIR": "/logs/agent/capture",
            "CAPTURE_TIMEOUT_SECONDS": str(self.capture_timeout_sec),
        }
        if not isinstance(environment, AGSPrebuiltEnvironment):
            raise TypeError("LosslessHermesAgent 只能运行在 AGSPrebuiltEnvironment")
        await self.exec_as_agent(
            environment,
            command="mkdir -p /logs/agent/capture",
            timeout_sec=10,
        )
        try:
            self._capture_pid = await environment.start_background_process(
                command=(
                    "exec python3 -m harbor_ags.capture "
                    ">/logs/agent/capture.stdout.log "
                    "2>/logs/agent/capture.stderr.log </dev/null"
                ),
                env=env,
                cwd=self.workspace,
                user=environment.default_user,
            )
            await self.exec_as_agent(
                environment,
                command=f"printf '%s\\n' {self._capture_pid} >/logs/agent/capture.pid",
                timeout_sec=10,
            )
            health_url = f"http://127.0.0.1:{self.capture_port}/healthz"
            result = await self.exec_as_agent(
                environment,
                command=(
                    "for i in $(seq 1 30); do "
                    f"curl -fsS --max-time 2 {shlex.quote(health_url)} >/dev/null && exit 0; "
                    "sleep 0.2; done; "
                    "cat /logs/agent/capture.stderr.log >&2; exit 1"
                ),
                timeout_sec=15,
            )
            if result.return_code != 0:
                raise TrajectoryCaptureError("Anthropic CaptureProxy 未就绪")
        except BaseException as exc:
            if self._capture_pid is not None:
                try:
                    await self._stop_capture(environment)
                except Exception:
                    self.logger.exception("CaptureProxy 启动失败后的清理也失败")
            if isinstance(exc, TrajectoryCaptureError):
                raise
            raise TrajectoryCaptureError(
                f"Anthropic CaptureProxy 启动失败：{type(exc).__name__}: {exc}"
            ) from exc

    async def _stop_capture(self, environment: BaseEnvironment) -> None:
        pid = self._capture_pid
        if pid is None:
            return
        result = await environment.exec(
            command=(
                f"pid={pid}; kill -TERM $pid 2>/dev/null || true; "
                "for i in $(seq 1 30); do kill -0 $pid 2>/dev/null || exit 0; sleep 0.1; done; "
                "kill -KILL $pid 2>/dev/null || true; exit 1"
            ),
            timeout_sec=10,
        )
        self._capture_pid = None
        if result.return_code != 0:
            raise TrajectoryCaptureError("CaptureProxy 未能正常停止")

    @with_prompt_template
    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        self._run_error = None
        try:
            await self._run(instruction, environment, context)
        except BaseException as exc:
            self._run_error = exc
            raise

    async def _run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext,
    ) -> None:
        model, upstream, api_key = self._model_settings()
        expected_appendix = (
            render_runtime_appendix(
                workspace_root=self.workspace,
                toolsets=self.toolsets,
            )
            if self.input_profile == TASK_BUNDLE_INPUT_PROFILE
            else None
        )
        try:
            input_components = instruction_components(
                instruction,
                expected_runtime_appendix=expected_appendix,
            )
        except ValueError as exc:
            raise HarnessDriftError(f"最终 Agent 输入不符合 profile: {exc}") from exc
        await self._upload_config_text(
            environment,
            content=instruction,
            remote_path="/tmp/harbor_ags_runtime/instruction.md",
            filename="instruction.md",
        )
        await self._upload_config_text(
            environment,
            content=json.dumps(input_components, ensure_ascii=False, indent=2) + "\n",
            remote_path="/tmp/harbor_ags_runtime/input-components.json",
            filename="input-components.json",
        )
        started = time.time()
        capture_started = False
        primary_error: BaseException | None = None
        try:
            await self._start_capture(
                environment,
                upstream=upstream,
                api_key=api_key,
            )
            capture_started = True
            harness_env = {
                "PYTHONPATH": f"/tmp/harbor_ags_runtime:{_REMOTE_SITE_PACKAGES}",
                # Messages wire protocol，不是审计来源；审计 provider 见 _run_metadata。
                "HERMES_LLM_PROVIDER": "anthropic",
                "HERMES_LLM_BASE_URL": f"http://127.0.0.1:{self.capture_port}",
                "HERMES_LLM_API_KEY": "capture-proxy-local",
                "HERMES_LLM_MODEL": model,
                "HERMES_REDACT_SECRETS": "false",
                "HERMES_TOOLSETS": self.toolsets,
                "HERMES_MAX_ITER": str(self.max_iterations),
                "HERMES_WORKSPACE": self.workspace,
                "HERMES_INSTRUCTION_FILE": "/tmp/harbor_ags_runtime/instruction.md",
                "HERMES_INPUT_COMPONENTS_FILE": (
                    "/tmp/harbor_ags_runtime/input-components.json"
                ),
                "HERMES_HARNESS_OUT": "/logs/agent",
                "HERMES_TASK_ID": str(self.context_id or self.session_id or "harbor-smoke"),
                "HERMES_HOME": "/home/user/.hermes",
            }
            unset_flags = " ".join(
                f"-u {shlex.quote(name)}" for name in _SANDBOX_MODEL_ENV_UNSET
            )
            result = await self.exec_as_agent(
                environment,
                command=(
                    f"env {unset_flags} "
                    "python3 /tmp/harbor_ags_runtime/hermes_harness.py "
                    ">/logs/agent/hermes.stdout.log "
                    "2>/logs/agent/hermes.stderr.log"
                ),
                env=harness_env,
                cwd=self.hermes_source_dir,
            )
            if result.return_code != 0:
                raise RuntimeError(f"Hermes harness 退出码 {result.return_code}")
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if capture_started:
                try:
                    await self._stop_capture(environment)
                except Exception:
                    if primary_error is None:
                        raise
                    self.logger.exception("主执行已失败，且 CaptureProxy 清理失败")

        self._run_metadata = {
            "backend": "harbor_ags",
            "provider": self.model_name.split("/", 1)[0],
            "model": model,
            "endpoint": upstream,
            "session_id": str(self.context_id or self.session_id or ""),
            "hermes_version": self.version(),
            "hermes_commit": self._observed_commit,
            "input_profile": self.input_profile,
            "rendered_user_prompt_sha256": input_components["hashes"][
                "rendered_user_prompt_sha256"
            ],
            "elapsed_s": round(time.time() - started, 3),
        }

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        """下载日志后生成无损视图、ATIF 和对账报告。"""
        # Harbor 在主执行失败后的 recovery 路径会再调用一次该 hook。先把
        # context 变为非空，既能保留首次基础设施异常，也不会影响其他 step
        # 的独立 AgentContext。
        existing_metadata = dict(context.metadata or {})
        state = existing_metadata.get("_harbor_ags_evidence")
        if isinstance(state, dict) and state.get("status") in {
            "running",
            "complete",
            "failed",
        }:
            return
        existing_metadata["_harbor_ags_evidence"] = {"status": "running"}
        context.metadata = existing_metadata
        try:
            from .evidence import atif_prompt_tokens, normalize_usage, write_evidence_bundle

            bundle = write_evidence_bundle(self.logs_dir, metadata=self._run_metadata)
            usage = normalize_usage(bundle.get("usage"))
            context.n_input_tokens = atif_prompt_tokens(usage)
            context.n_cache_tokens = usage.get("cache_read_input_tokens")
            context.n_output_tokens = usage.get("output_tokens")
            metadata = dict(context.metadata or {})
            metadata.update(self._run_metadata)
            metadata["trajectory_full"] = "trajectory.full.json"
            metadata["trajectory_atif"] = "trajectory.json"
            metadata["reconciliation"] = "reconciliation.json"
            metadata["projection_report"] = "projection_report.json"
            metadata["_harbor_ags_evidence"] = {"status": "complete"}
            context.metadata = metadata
        except Exception as exc:
            metadata = dict(context.metadata or {})
            metadata["_harbor_ags_evidence"] = {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            context.metadata = metadata
            if self._run_error is not None:
                # Harbor 会在原异常的处理路径中调用此 hook，返回以保留首因。
                metadata["_harbor_ags_evidence"]["primary_error_type"] = type(self._run_error).__name__
                return
            failure = TrajectoryCaptureError(f"轨迹生成失败：{type(exc).__name__}: {exc}")
            raise failure from exc
