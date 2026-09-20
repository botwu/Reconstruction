"""Hermes 角色 Agent 运行时。

模型直接配在 ``run_agent.AIAgent`` 上（base_url / api_key / provider / model），
与 harbor_ags hermes_harness 同一入口。不使用 ChatModel 工具循环。
Harbor ``LosslessHermesAgent`` 仍只用于 AGS 解题。重建角色的工具面可经
``SandboxedAgentRuntime`` 打到 AGS workspace。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MethodType
from typing import Any, Protocol

from traceforge.reconstruction.agents.roles import AgentRole
from traceforge.reconstruction.agents.session import (
    AgentSession,
    collect_workspace_writes,
    execute_tool,
    tool_schemas,
    workspace_tree_hash,
    _debug_agent_log,
)
from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    parse_json_object,
)
from traceforge.trajectory.privacy import omit_private_reasoning
from traceforge.reconstruction.tls import pin_process_tls

HermesFactory = Callable[..., Any]
# Hermes 在一轮对话内会使用进程级 cwd、认证环境变量和模块 dispatcher。
# 同进程的重建必须串行；批量并行由独立进程/沙盒承担。
_HERMES_PROCESS_LOCK = threading.RLock()
_MISSING_DISPATCH = object()
DEFAULT_HERMES_HOME = Path(
    "/mnt/afs_toolcall/wujian1/Projects/tokenhub_data_model_eval/R01/hermes-agent"
)


class HermesUnavailableError(RuntimeError):
    """找不到 Hermes，或无法从配置给 Agent 配模型。"""


def resolve_hermes_home(explicit: str | Path | None = None) -> Path:
    """解析 Hermes 根目录：``--hermes-home`` / ``HERMES_HOME`` / 本机默认路径。"""

    if explicit is not None and str(explicit).strip():
        home = Path(explicit).expanduser().resolve()
    else:
        env = os.environ.get("HERMES_HOME", "").strip()
        home = Path(env).expanduser().resolve() if env else DEFAULT_HERMES_HOME
    marker = home / "run_agent.py"
    if not marker.is_file():
        raise HermesUnavailableError(
            f"Hermes 根目录缺少 run_agent.py：{home}\n"
            "设置 HERMES_HOME 或 --hermes-home，指向含 run_agent.py 的 hermes-agent 目录。"
        )
    return home


def hermes_install_hint(home: Path) -> str:
    return f'.venv/bin/pip install -e "{home}"'


@dataclass
class AgentResult:
    role: str
    backend: str
    payload: dict[str, Any]
    turns: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    final_text: str | None = None
    completed: bool = False


def classify_hermes_failure(text: str | None) -> str | None:
    """把 Hermes 的连接/超时失败从 INVALID_JSON 里拆出来。"""

    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    first_line = lowered.splitlines()[0].strip()
    if first_line.startswith(("connection error", "apiconnectionerror")):
        return "MODEL_CONNECTION_ERROR"
    if first_line.startswith(("timed out", "timeout", "interrupted")):
        return "MODEL_TIMEOUT"
    if first_line.startswith("api call failed"):
        return "MODEL_API_FAILED"
    return None


class AgentRuntime(Protocol):
    model_name: str

    def run(
        self,
        *,
        role: AgentRole,
        instruction: str,
        session: AgentSession,
        output_root: Path,
    ) -> AgentResult: ...


class HermesNativeRuntime:
    """直接构造 ``AIAgent``，模型在创建时配好。"""

    backend = "hermes"

    def __init__(
        self,
        *,
        factory: HermesFactory,
        base_url: str,
        api_key: str,
        model_name: str,
        provider: str = "anthropic",
    ) -> None:
        if not model_name.strip():
            raise HermesUnavailableError("Hermes Agent 必须配置 model")
        if not api_key.strip():
            raise HermesUnavailableError("Hermes Agent 必须配置 api_key")
        if not base_url.strip():
            raise HermesUnavailableError("Hermes Agent 必须配置 base_url")
        self.factory = factory
        self.base_url = (
            base_url if provider == "anthropic" else openai_sdk_base_url(base_url)
        )
        self._api_key = api_key
        self.model_name = model_name
        self.provider = provider

    def run(
        self,
        *,
        role: AgentRole,
        instruction: str,
        session: AgentSession,
        output_root: Path,
    ) -> AgentResult:
        with _HERMES_PROCESS_LOCK:
            return self._run_locked(
                role=role,
                instruction=instruction,
                session=session,
                output_root=output_root,
            )

    def _run_locked(
        self,
        *,
        role: AgentRole,
        instruction: str,
        session: AgentSession,
        output_root: Path,
    ) -> AgentResult:
        session.allow_write = role.allow_write
        if role.name == "verifier":
            session.allow_tests = True
        existing_workspace = session.workspace
        sandboxed = session.sandbox is not None
        workdir = (
            Path(output_root) / "hermes_scratch"
            if sandboxed
            else (existing_workspace or (output_root / "hermes_workspace"))
        )
        workdir.mkdir(parents=True, exist_ok=True)
        # Hermes cwd is scratch when sandboxed. Stage replayed files there so
        # list_dir/read_file and any leftover native reads see the same tree
        # the sandbox already has. Writes still go through the role proxy.
        _stage_replay_tree(workdir, session)
        if not sandboxed:
            session.workspace = workdir.resolve()
        elif workdir.resolve() not in {item.resolve() for item in session.path_aliases}:
            # Hermes chdirs into scratch; map those absolute paths back to the
            # sandbox-relative workspace instead of treating them as unsafe.
            session.path_aliases.append(workdir.resolve())
        before = {} if sandboxed else workspace_tree_hash(workdir)
        cwd = os.getcwd()
        raw: dict[str, Any] = {}
        errors: list[str] = []
        final_text = None
        try:
            os.chdir(workdir)
            auth_context = (
                pin_anthropic_channel_env(self._api_key)
                if self.provider == "anthropic"
                else pin_openai_channel_env(self._api_key, self.base_url)
            )
            with auth_context, pin_hermes_timeout_env():
                agent = self.factory(
                    base_url=self.base_url,
                    api_key=self._api_key,
                    provider=self.provider,
                    api_mode=(
                        "anthropic_messages"
                        if self.provider == "anthropic"
                        else "chat_completions"
                    ),
                    model=self.model_name,
                    # Reconstruction proxy owns tools. Native file/terminal bypass checks.
                    enabled_toolsets=[],
                    max_iterations=role.max_iterations,
                    quiet_mode=True,
                    save_trajectories=False,
                    skip_context_files=True,
                    skip_memory=True,
                )
                if self.provider == "anthropic":
                    apply_anthropic_messages_client(
                        agent, base_url=self.base_url, api_key=self._api_key
                    )
                # Conversation loop prefers SSE even in quiet mode. Reconstruction
                # must not depend on apply() seeing `_anthropic_client`.
                agent._disable_streaming = True
                # Hermes native dispatch must use the role-scoped proxy.
                _bind_agent_tools(agent, role=role, session=session)
                with _scoped_hermes_dispatch(agent, role=role):
                    raw = agent.run_conversation(
                        instruction, system_message=role.identity, task_id=role.name
                    )
            if not isinstance(raw, dict):
                raise TypeError("Hermes result must be a dict")
            final_text = str(raw.get("final_response") or "")
            infra = classify_hermes_failure(final_text)
            if infra:
                errors.append(infra)
                payload = {}
            else:
                payload = parse_json_object(final_text) if final_text.strip() else {}
        except Exception as exc:
            errors.append(getattr(exc, "code", None) or type(exc).__name__)
            payload = {}
        finally:
            os.chdir(cwd)
        if not sandboxed:
            after = workspace_tree_hash(workdir)
            unexpected = _unexpected_workspace_changes(session, before, after)
            if unexpected:
                errors.extend(unexpected)
                payload = {}
        if session.policy_errors:
            errors.extend(session.policy_errors)
            payload = {}
        # #region agent log
        _debug_agent_log(
            "H1",
            "runtime.py:HermesNativeRuntime.run",
            "after_policy_gate",
            {
                "role": role.name,
                "backend": self.backend,
                "policy_errors": list(session.policy_errors),
                "error_codes": list(errors),
                "payload_keys": sorted(payload) if isinstance(payload, dict) else [],
                "payload_wiped": not bool(payload),
                "raw_completed": bool((raw or {}).get("completed")) if isinstance(raw, dict) else False,
                "final_text_prefix": (final_text or "")[:80],
            },
        )
        # #endregion
        turns = [
            {
                "messages": len(raw.get("messages") or []),
                "api_calls": raw.get("api_calls"),
                "completed": raw.get("completed"),
                "model": self.model_name,
                "provider": self.provider,
                "tool_events": len(session.tool_events),
                "policy_errors": list(session.policy_errors),
            }
        ]
        write_agent_trace(
            output_root,
            role=role,
            backend=self.backend,
            instruction=instruction,
            turns=turns,
            final_text=final_text,
            model_name=self.model_name,
            tool_events=session.tool_events,
        )
        return AgentResult(
            role=role.name,
            backend=self.backend,
            payload=payload,
            turns=turns,
            errors=errors,
            final_text=final_text,
            completed=bool(raw.get("completed")) and not errors and bool(payload),
        )


def build_hermes_runtime(
    *,
    model_name: str,
    config_path: str | Path | None = None,
    channel: str = "claude",
    factory: HermesFactory | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
    hermes_home: str | Path | None = None,
) -> HermesNativeRuntime:
    """从 config channel 取出连接信息，把模型配进 Hermes Agent。"""

    resolved_factory = factory or _load_hermes_factory(hermes_home)
    if resolved_factory is None:
        home = None
        try:
            home = resolve_hermes_home(hermes_home)
        except HermesUnavailableError:
            home = DEFAULT_HERMES_HOME
        raise HermesUnavailableError(
            "未找到 run_agent.AIAgent。重建只跑 Hermes Agent，不再使用 ChatModel 工具循环。\n"
            f"请安装本机 Hermes：{hermes_install_hint(home)}"
        )
    if base_url and api_key:
        url, key = _messages_base_url(base_url), api_key
    else:
        if config_path is None:
            raise HermesUnavailableError("必须提供 config.yaml，以便给 Hermes Agent 配置模型")
        url, key = load_channel_connection(config_path, channel)
    return HermesNativeRuntime(
        factory=resolved_factory,
        base_url=url,
        api_key=key,
        model_name=model_name,
        provider=provider or provider_for_channel(channel, model_name),
    )


def load_channel_connection(config_path: str | Path, channel: str) -> tuple[str, str]:
    """读取 channel 的 url/key，只留在内存，不写日志。"""

    try:
        from traceforge.reconstruction.model_gateway import (
            load_channel_connection as load_gateway_connection,
        )

        url, key = load_gateway_connection(config_path, channel)
    except ModelGatewayError as exc:
        raise HermesUnavailableError(str(exc)) from exc
    return _messages_base_url(url), key


def provider_for_channel(channel: str, model_name: str) -> str:
    if "/" in model_name:
        return model_name.split("/", 1)[0]
    if channel in {"claude", "anthropic"} or model_name.startswith("claude-"):
        return "anthropic"
    if channel in {"deepseek", "vol"} or model_name.startswith("vol"):
        return "deepseek"
    if channel in {"gemini", "gpt"}:
        return channel
    return channel or "unknown"


def resolve_rollout_model(
    explicit: str | None, *, channel: str, model_name: str
) -> str:
    """Harbor 的 provider/model 必须反映真实 channel，不能默认写成 anthropic。"""

    chosen = (explicit or model_name or "").strip()
    if not chosen:
        raise ValueError("必须提供 rollout model 或 model-name")
    if explicit and "/" in chosen:
        return chosen
    # model_name 是上游的完整 ID；其中的斜杠不是 Harbor provider 分隔符。
    return f"{provider_for_channel(channel, chosen)}/{chosen}"


def anthropic_sdk_base_url(url: str) -> str:
    """Tokenhub Anthropic SDK 的 base_url：主机根，不要带 ``/v1``。"""

    cleaned = url.rstrip("/")
    for suffix in ("/chat/completions", "/messages", "/v1"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip("/")
    return cleaned + "/"


@contextmanager
def pin_openai_channel_env(api_key: str, base_url: str) -> Iterator[None]:
    """Pin OpenAI-compatible credentials for non-Anthropic Hermes providers."""
    names = ("OPENAI_API_KEY", "OPENAI_BASE_URL")
    saved = {name: os.environ.get(name) for name in names}
    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["OPENAI_BASE_URL"] = base_url
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def pin_anthropic_channel_env(api_key: str) -> Iterator[None]:
    """对话期间钉死 Tokenhub channel 密钥，避免 Hermes ``.env`` 抢认证。"""

    names = ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    saved = {name: os.environ.get(name) for name in names}
    os.environ["ANTHROPIC_API_KEY"] = api_key
    os.environ.pop("ANTHROPIC_TOKEN", None)
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def pin_hermes_timeout_env() -> Iterator[float]:
    """把 Hermes 流式 / 非流式 stale 钉到重建超时，避免 180s × 多次空转。

    TokenHub 长时间不吐 token 是模型侧；管线必须在超时后真正关掉
    Anthropic 连接，而不是去重建缺 ``OPENAI_API_KEY`` 的 OpenAI 客户端。
    """

    timeout_seconds = _traceforge_model_timeout_seconds()
    names = (
        "HERMES_STREAM_STALE_TIMEOUT",
        "HERMES_API_CALL_STALE_TIMEOUT",
        "HERMES_STREAM_RETRIES",
    )
    saved = {name: os.environ.get(name) for name in names}
    os.environ["HERMES_STREAM_STALE_TIMEOUT"] = str(int(timeout_seconds))
    os.environ["HERMES_API_CALL_STALE_TIMEOUT"] = str(int(timeout_seconds))
    os.environ["HERMES_STREAM_RETRIES"] = "0"
    try:
        yield timeout_seconds
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _traceforge_model_timeout_seconds() -> float:
    """重建角色的模型窗口：默认可覆盖，且夹在 5–600s。"""

    try:
        timeout_seconds = float(os.environ.get("TRACEFORGE_MODEL_TIMEOUT_SECONDS", "120"))
    except ValueError:
        timeout_seconds = 120.0
    return max(5.0, min(timeout_seconds, 600.0))


def apply_anthropic_messages_client(agent: Any, *, base_url: str, api_key: str) -> None:
    """按 Tokenhub 官方写法绑定 ``Anthropic(base_url, api_key)``。

    Hermes 的 ``build_anthropic_client`` 会加 beta 头，401 后还会用
    ``hermes-agent/.env`` 刷新成另一把密钥。这里保持 provider=anthropic。

    Hermes 流式 stale 只关 OpenAI 连接池（``stale_stream_pool_cleanup``），
    Anthropic Messages 路径既杀不掉飞行中的 stream，重建还会因缺少
    ``OPENAI_API_KEY`` 失败。重建角色改走有界的非流式 Messages，并把 stale
    重建钉回 Tokenhub Anthropic 客户端。
    """

    if hasattr(agent, "api_key"):
        agent.api_key = api_key
    if hasattr(agent, "_anthropic_api_key"):
        agent._anthropic_api_key = api_key
    if hasattr(agent, "_anthropic_base_url"):
        agent._anthropic_base_url = anthropic_sdk_base_url(base_url)
    has_client_attr = hasattr(agent, "_anthropic_client")
    has_openai_replace = callable(getattr(agent, "_replace_primary_openai_client", None))
    if not has_client_attr and not has_openai_replace:
        return
    if has_client_attr:
        _bind_tokenhub_anthropic_client(agent, base_url=base_url, api_key=api_key)
    _install_tokenhub_anthropic_hooks(agent, base_url=base_url, api_key=api_key)


def _bind_tokenhub_anthropic_client(agent: Any, *, base_url: str, api_key: str) -> None:
    try:
        import anthropic
    except ImportError as exc:
        raise HermesUnavailableError(
            "缺少 anthropic SDK。请执行：uv pip install anthropic==0.87.0"
        ) from exc
    timeout_seconds = _traceforge_model_timeout_seconds()
    verify = pin_process_tls()
    http_client = None
    if verify:
        try:
            import httpx
        except ImportError:
            httpx = None  # type: ignore[assignment]
        if httpx is not None:
            http_client = httpx.Client(verify=verify, timeout=timeout_seconds)
    kwargs: dict[str, Any] = {
        "base_url": anthropic_sdk_base_url(base_url),
        "api_key": api_key,
        "timeout": timeout_seconds,
    }
    if http_client is not None:
        kwargs["http_client"] = http_client
    if hasattr(agent, "api_key"):
        agent.api_key = api_key
    if hasattr(agent, "_anthropic_api_key"):
        agent._anthropic_api_key = api_key
    if hasattr(agent, "_anthropic_base_url"):
        agent._anthropic_base_url = anthropic_sdk_base_url(base_url)
    agent._anthropic_client = anthropic.Anthropic(**kwargs)


def _install_tokenhub_anthropic_hooks(agent: Any, *, base_url: str, api_key: str) -> None:
    """让 Hermes stale / 凭证刷新继续走 Tokenhub，而不是 OpenAI 客户端。"""

    if getattr(agent, "_traceforge_tokenhub_hooks", False):
        agent._disable_streaming = True
        return
    timeout_seconds = _traceforge_model_timeout_seconds()

    def rebuild() -> None:
        old = getattr(agent, "_anthropic_client", None)
        if old is not None:
            closer = getattr(old, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        _bind_tokenhub_anthropic_client(agent, base_url=base_url, api_key=api_key)

    agent._rebuild_anthropic_client = rebuild
    original_replace = getattr(agent, "_replace_primary_openai_client", None)

    def replace(*, reason: str) -> bool:
        if getattr(agent, "api_mode", "chat_completions") == "anthropic_messages" or hasattr(
            agent, "_anthropic_client"
        ):
            try:
                rebuild()
                return True
            except Exception:
                return False
        if callable(original_replace):
            return bool(original_replace(reason=reason))
        return False

    agent._replace_primary_openai_client = replace
    agent._try_refresh_anthropic_client_credentials = lambda: False
    original_stale = getattr(agent, "_compute_non_stream_stale_timeout", None)

    def compute_stale(api_kwargs: Any) -> float:
        native = timeout_seconds
        if callable(original_stale):
            try:
                native = float(original_stale(api_kwargs))
            except Exception:
                native = timeout_seconds
        return min(native, timeout_seconds)

    agent._compute_non_stream_stale_timeout = compute_stale
    stream_fn = getattr(agent, "_interruptible_streaming_api_call", None)
    call_fn = getattr(agent, "_interruptible_api_call", None)
    if callable(stream_fn) and callable(call_fn):
        def nonstream_only(
            api_kwargs: Any, on_first_delta: Any = None, **kwargs: Any
        ) -> Any:
            return call_fn(api_kwargs)

        agent._interruptible_streaming_api_call = nonstream_only
    # Reconstruction 只要一份 JSON，不需要 Hermes SSE。流式 stale 只杀
    # OpenAI 连接，Tokenhub Anthropic stream 会继续空等。
    agent._disable_streaming = True
    agent._traceforge_tokenhub_hooks = True


def write_agent_trace(
    output_root: str | Path,
    *,
    role: AgentRole,
    backend: str,
    instruction: str,
    turns: list[dict[str, Any]],
    final_text: str | None,
    model_name: str,
    tool_events: list[dict[str, Any]] | None = None,
) -> Path:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    private = root / "private"
    private.mkdir(exist_ok=True)
    payload = omit_private_reasoning(
        {
            "schema_version": "traceforge.agent-trace.v1",
            "role": role.name,
            "backend": backend,
            "model": model_name,
            "identity_sha256": hashlib.sha256(role.identity.encode("utf-8")).hexdigest(),
            "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            "turns": omit_private_reasoning(turns),
            "tool_events": omit_private_reasoning(_redact_trace(list(tool_events or []))),
            "final_text": _omit_reasoning_text(final_text) if isinstance(final_text, str) else final_text,
            "credentials_embedded": False,
            "privacy": {"private_thinking_reasoning": "omitted"},
        }
    )
    path = private / "agent_trace.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _stage_replay_tree(workdir: Path, session: AgentSession) -> None:
    """Copy replayed files into Hermes cwd without inventing new paths."""

    from traceforge.reconstruction.agents.session import safe_relpath

    files = dict(session.replay_files)
    root = session.workspace
    if root is not None and root.is_dir():
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            try:
                files.setdefault(rel, path.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                continue
    for rel, content in files.items():
        if safe_relpath(rel) is None or not isinstance(content, str):
            continue
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(content, encoding="utf-8")


@contextmanager
def _scoped_hermes_dispatch(agent: Any, *, role: AgentRole) -> Iterator[None]:
    """接管 Hermes 顺序分发，并在成功或异常退出后精确恢复原模块状态。"""

    try:
        module = importlib.import_module("run_agent")
    except ModuleNotFoundError as exc:
        if exc.name != "run_agent":
            raise
        # 注入的测试 factory 可以只调用实例代理，不依赖 Hermes 安装。
        yield
        return
    previous = getattr(module, "handle_function_call", _MISSING_DISPATCH)
    owner_session_id = getattr(agent, "session_id", None)

    def dispatch(
        function_name: str,
        function_args: dict,
        task_id: str | None = None,
        tool_call_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        caller_session_id = kwargs.get("session_id")
        if owner_session_id and caller_session_id and caller_session_id != owner_session_id:
            return "error: HERMES_SESSION_MISMATCH"
        # 包括未知工具在内的所有调用都经过角色 allowlist，不能回退到原生工具。
        return agent._invoke_tool(
            function_name,
            function_args,
            task_id or role.name,
            tool_call_id=tool_call_id,
            messages=kwargs.get("messages"),
            skip_tool_request_middleware=True,
        )

    module.handle_function_call = dispatch
    try:
        yield
    finally:
        if previous is _MISSING_DISPATCH:
            delattr(module, "handle_function_call")
        else:
            module.handle_function_call = previous


def is_fatal_tool_result(function_name: str, result: str) -> bool:
    """True when a tool error should wipe the Hermes JSON payload.

    Rejected PARTIAL / listing / log writes stay tool errors only: the write
    did not land, the agent may still finish, and candidate gates still apply.
    """

    if not result.startswith("error:"):
        return False
    return (
        "PROTECTED_FILE_OVERWRITE" in result
        or "DUPLICATE_CONFLICTING_PATH" in result
        or "DUPLICATE_PATH" in result
        or "unknown evidence_ref_id" in result
        or "evidence_ref_ids required" in result
        or "cannot write files" in result
        or (
            function_name == "write_test"
            and "unsafe path" in result
        )
    )


def _bind_agent_tools(agent: Any, *, role: AgentRole, session: AgentSession) -> None:
    """固定角色工具 schema，并接管 Hermes 的并发调用入口。

    并发 worker 调用实例 ``_invoke_tool``；顺序执行器使用模块 dispatcher，
    由 ``_scoped_hermes_dispatch`` 转入同一代理。两条路径共享权限和审计。
    """
    allowed = frozenset(role.tools)
    schemas = tool_schemas(role.tools)
    agent.tools = schemas
    agent.valid_tool_names = {item["function"]["name"] for item in schemas}
    # Hermes rebuilds agent.tools from MCP/registry between turns. That wipes
    # the reconstruction proxy (list_evidence / write_file / write_test).
    agent._skip_mcp_refresh = True
    agent.enabled_toolsets = []

    def invoke(
        _agent: Any,
        function_name: str,
        function_args: dict,
        effective_task_id: str,
        tool_call_id: str | None = None,
        messages: list | None = None,
        pre_tool_block_checked: bool = False,
        skip_tool_request_middleware: bool = False,
        tool_request_middleware_trace: list[dict[str, Any]] | None = None,
    ) -> str:
        if function_name not in allowed:
            message = f"error: tool is not enabled for role {role.name}: {function_name}"
            session.policy_errors.append("TOOL_NOT_ALLOWED:" + function_name)
            return message
        with session.lock:
            result = execute_tool(function_name, function_args, session)
            # Keep a private, replayable tool trace without persisting secrets or
            # unbounded tool output. The complete evidence remains in the session
            # source; this record proves the actual arguments/result used by the
            # reconstruction agent.
            safe_args = _redact_trace(function_args)
            session.tool_events.append(
                {
                    "name": function_name,
                    "tool_call_id": tool_call_id,
                    "ok": not result.startswith("error:"),
                    "arguments": safe_args,
                    "result_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
                    "result_preview": _redact_text(result[:512]),
                }
            )
            fatal = is_fatal_tool_result(function_name, result)
            if fatal:
                session.policy_errors.append(result.removeprefix("error:").strip())
            # #region agent log
            if result.startswith("error:"):
                _debug_agent_log(
                    "H1",
                    "runtime.py:invoke",
                    "tool_error_fatal_decision",
                    {
                        "function_name": function_name,
                        "fatal": fatal,
                        "recorded": bool(result.startswith("error:") and fatal),
                        "result_code": result.removeprefix("error:").strip()[:120],
                    },
                )
            # #endregion
            return result

    agent._invoke_tool = MethodType(invoke, agent)
    agent._traceforge_tool_names = set(role.tools)


def _redact_trace(value: Any) -> Any:
    """Redact credentials while retaining actual tool argument structure."""
    secret = ("key", "token", "secret", "password", "authorization", "credential")
    if isinstance(value, dict):
        return {
            str(k): ("<redacted>" if any(x in str(k).lower() for x in secret) else _redact_trace(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_trace(item) for item in value]
    if isinstance(value, str) and len(value) > 4096:
        return value[:4096] + "...[truncated]"
    return value


def _redact_text(value: str) -> str:
    return re.sub(
        r"(?i)(sk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|token|password)\s*[=:]\s*)[^\s,;]+",
        lambda match: "<redacted>" if match.group(0).lower().startswith("sk-") else match.group(1) + "<redacted>",
        value,
    )


def _omit_reasoning_text(value: str) -> str:
    cleaned = re.sub(r"(?is)<thinking>.*?</thinking>", "", value)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return _redact_text(cleaned)
    return _redact_text(json.dumps(omit_private_reasoning(parsed), ensure_ascii=False))


def _unexpected_workspace_changes(
    session: AgentSession, before: dict[str, str], after: dict[str, str]
) -> list[str]:
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    authorized = {str(item.get("path")) for item in session.writes if item.get("path")}
    unexpected = sorted(changed - authorized)
    errors = ["UNAUTHORIZED_WORKSPACE_MUTATION:" + path for path in unexpected]
    protected_changed = sorted(set(unexpected) & set(session.protected_paths))
    errors.extend("PROTECTED_WORKSPACE_MUTATION:" + path for path in protected_changed)
    return errors


def _load_hermes_factory(hermes_home: str | Path | None = None) -> HermesFactory:
    home = resolve_hermes_home(hermes_home)
    root = str(home)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        module = importlib.import_module("run_agent")
    except ImportError as exc:
        raise HermesUnavailableError(
            f"无法 import run_agent（Hermes 根目录：{home}）。\n"
            f"请在 TraceRconstruction 中执行：{hermes_install_hint(home)}"
        ) from exc
    factory = getattr(module, "AIAgent", None)
    if not callable(factory):
        raise HermesUnavailableError(f"{home}/run_agent.py 没有 AIAgent")
    return factory


def openai_sdk_base_url(url: str) -> str:
    """TokenHub 根 URL 需要 /v1；显式的兼容 API 路径保持不变。"""
    from urllib.parse import urlsplit, urlunsplit
    cleaned = _messages_base_url(url)
    parts = urlsplit(cleaned)
    if not parts.path or parts.path == "/":
        return urlunsplit((parts.scheme, parts.netloc, "/v1", parts.query, parts.fragment))
    return cleaned


def _messages_base_url(url: str) -> str:
    cleaned = url.rstrip("/")
    suffix = "/chat/completions"
    if cleaned.endswith(suffix):
        cleaned = cleaned[: -len(suffix)]
    return cleaned


class SandboxedAgentRuntime:
    """Hermes 仍在宿主机推理；文件和 pytest 打到注入的 ContainerRuntime。"""

    backend = "hermes-sandbox"

    def __init__(
        self,
        inner: AgentRuntime,
        runtime_factory: Callable[[], Any],
    ) -> None:
        self.inner = inner
        self.runtime_factory = runtime_factory
        self.model_name = inner.model_name

    def run(
        self,
        *,
        role: AgentRole,
        instruction: str,
        session: AgentSession,
        output_root: Path,
    ) -> AgentResult:
        if role.name == "intent":
            # #region agent log
            _debug_agent_log(
                "H6",
                "runtime.py:SandboxedAgentRuntime.run",
                "role_execution_plane",
                {
                    "role": role.name,
                    "plane": "host_reason_only",
                    "ags_started": False,
                    "sandbox_bound": False,
                    "inner_backend": getattr(self.inner, "backend", None),
                },
            )
            # #endregion
            return self.inner.run(
                role=role,
                instruction=instruction,
                session=session,
                output_root=output_root,
            )

        from traceforge.reconstruction.agents.sandbox import prepare_role_sandbox, run_coro

        runtime = None
        started = False
        try:
            try:
                runtime = self.runtime_factory()
                run_coro(
                    prepare_role_sandbox(
                        role=role,
                        runtime=runtime,
                        session=session,
                        staging_root=Path(output_root),
                    )
                )
            except Exception as exc:
                init_error = f"SANDBOX_INIT:{type(exc).__name__}"
                session.policy_errors.append(init_error)
                if runtime is not None:
                    try:
                        run_coro(runtime.stop(delete=True))
                        session.sandbox_stopped = True
                    except Exception as cleanup_exc:
                        session.sandbox_cleanup_error = f"{type(cleanup_exc).__name__}:{cleanup_exc}"
                return AgentResult(
                    role=role.name,
                    backend=self.backend,
                    payload={},
                    errors=[init_error],
                    final_text=None,
                    completed=False,
                )
            started = True
            # #region agent log
            _debug_agent_log(
                "H6",
                "runtime.py:SandboxedAgentRuntime.run",
                "role_execution_plane",
                {
                    "role": role.name,
                    "plane": "host_reason_plus_ags_files",
                    "ags_started": True,
                    "sandbox_bound": session.sandbox is not None,
                    "remote_root": getattr(session.sandbox, "remote_root", None),
                    "allow_exec": getattr(session.sandbox, "allow_exec", None),
                    "allow_tests": getattr(session.sandbox, "allow_tests", None),
                    "inner_backend": getattr(self.inner, "backend", None),
                },
            )
            # #endregion
            try:
                result = self.inner.run(
                    role=role,
                    instruction=instruction,
                    session=session,
                    output_root=output_root,
                )
                result.backend = self.backend
                # #region agent log
                _debug_agent_log(
                    "H2",
                    "runtime.py:SandboxedAgentRuntime.run",
                    "backend_stamped",
                    {
                        "role": role.name,
                        "inner_backend": getattr(self.inner, "backend", None),
                        "stamped_backend": result.backend,
                        "completed": result.completed,
                        "error_count": len(result.errors),
                    },
                )
                # #endregion
            except BaseException as exc:
                result = AgentResult(
                    role=role.name,
                    backend=self.backend,
                    payload={},
                    errors=[f"AGENT_RUNTIME_ERROR:{type(exc).__name__}:{exc}"],
                    final_text=None,
                    completed=False,
                )
            return result
        finally:
            if started:
                try:
                    run_coro(runtime.stop(delete=True))
                    session.sandbox_stopped = True
                except Exception as exc:
                    session.sandbox_cleanup_error = f"{type(exc).__name__}:{exc}"
                    session.policy_errors.append(f"CONTAINER_CLEANUP_ERROR:{type(exc).__name__}")
                    # The result is returned before finally runs; mutate its
                    # error list so callers cannot treat an unclean sandbox as
                    # a successful agent turn.
                    if "result" in locals():
                        result.errors.append("CONTAINER_CLEANUP_ERROR:" + type(exc).__name__)
                        result.completed = False


def merge_completion_files(payload: dict[str, Any], session: AgentSession) -> dict[str, Any]:
    """Hermes 写下的新文件补进第一个 candidate，避免只写盘不填 JSON。"""

    writes = collect_workspace_writes(session)
    if not writes:
        return payload
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        payload = dict(payload)
        payload["candidates"] = [
            {
                "files": writes,
                "dependencies": [],
                "runtime_constraints": [],
                "uncertainties": [],
                "decision": payload.get("decision", "READY"),
            }
        ]
        return payload
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    files = first.get("files")
    errors: list[str] = []
    if files is None:
        files = []
    if not isinstance(files, list):
        errors.append("MERGE_FILES_NOT_ARRAY")
        files = []
    by_path: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            errors.append("MERGE_FILE_NOT_OBJECT")
            continue
        path = item["path"]
        if path in by_path:
            errors.append(f"DUPLICATE_CANDIDATE_PATH:{path}")
        by_path[path] = item
    merged = list(files)
    for item in writes:
        path = item["path"]
        existing = by_path.get(path)
        if existing is not None:
            if existing.get("content") != item.get("content"):
                # Sandbox write_file is the tree Sufficiency/Verifier will see.
                existing["content"] = item.get("content")
                if item.get("provenance"):
                    existing["provenance"] = item.get("provenance")
                if item.get("evidence_ref_ids"):
                    existing["evidence_ref_ids"] = item.get("evidence_ref_ids")
            continue
        merged.append(item)
        by_path[path] = item
    updated = dict(payload)
    rows = [dict(first)]
    rows[0]["files"] = merged
    updated["candidates"] = rows + [item for item in candidates[1:]]
    if errors:
        updated["_merge_errors"] = errors
    return updated
