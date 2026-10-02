"""Hermes 角色 Agent 运行时。

模型直接配在 ``run_agent.AIAgent`` 上（base_url / api_key / provider / model），
与 harbor_ags hermes_harness 同一入口。不使用 ChatModel 工具循环。
Harbor ``LosslessHermesAgent`` 仍只用于 AGS 解题。重建角色的工具面可经
``SandboxedAgentRuntime`` 打到 AGS workspace。
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, Protocol

from traceforge.reconstruction.agents.roles import AgentRole
from traceforge.reconstruction.agents.session import (
    AgentSession,
    collect_workspace_writes,
    execute_tool,
    tool_schemas,
    workspace_tree_hash,
)
from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    iter_config_items,
    parse_json_object,
)
from traceforge.reconstruction.session_source import source_session_message_indices
from traceforge.reconstruction.tls import pin_process_tls
from traceforge.trajectory.privacy import omit_private_reasoning

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
        api_mode: str | None = None,
        api_max_retries: int | None = None,
        agent_context_length: int | None = None,
    ) -> None:
        if not model_name.strip():
            raise HermesUnavailableError("Hermes Agent 必须配置 model")
        if not api_key.strip():
            raise HermesUnavailableError("Hermes Agent 必须配置 api_key")
        if not base_url.strip():
            raise HermesUnavailableError("Hermes Agent 必须配置 base_url")
        if api_max_retries is not None and (type(api_max_retries) is not int or api_max_retries < 1):
            raise HermesUnavailableError("Agent API 重试预算必须为正整数")
        if agent_context_length is not None and (
            type(agent_context_length) is not int or agent_context_length < 1
        ):
            raise HermesUnavailableError("agent_context_length 必须为正整数")
        self.api_max_retries = api_max_retries
        self.agent_context_length = agent_context_length
        self.factory = factory
        self.base_url = (
            base_url if provider == "anthropic" else openai_sdk_base_url(base_url)
        )
        self._api_key = api_key
        self.model_name = model_name
        self.provider = provider
        self.api_mode = api_mode or (
            "anthropic_messages" if provider == "anthropic" else "chat_completions"
        )
        if self.api_mode not in {"anthropic_messages", "chat_completions", "codex_responses"}:
            raise HermesUnavailableError(f"不支持的 Agent API 协议：{self.api_mode}")
        if (provider == "anthropic") != (self.api_mode == "anthropic_messages"):
            raise HermesUnavailableError("Agent API 协议与 Anthropic provider 不一致")

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
        turns: list[dict[str, Any]] = []
        payload: dict[str, Any] = {}
        try:
            os.chdir(workdir)
            auth_context = (
                pin_anthropic_channel_env(self._api_key)
                if self.provider == "anthropic"
                else pin_openai_channel_env(self._api_key, self.base_url)
            )
            with auth_context, pin_hermes_timeout_env(role.request_timeout_seconds) as request_timeout:
                agent = self.factory(
                    base_url=self.base_url,
                    api_key=self._api_key,
                    # gpt 等配置通道名不是 Hermes provider；兼容 API 使用其原生 custom 路由。
                    provider="anthropic" if self.provider == "anthropic" else "custom",
                    api_mode=self.api_mode,
                    model=self.model_name,
                    # Reconstruction proxy owns tools. Native file/terminal bypass checks.
                    enabled_toolsets=[],
                    max_iterations=_traceforge_agent_max_iterations(role),
                    quiet_mode=True,
                    save_trajectories=False,
                    skip_context_files=True,
                    skip_memory=True,
                    **({"max_tokens": role.max_output_tokens} if role.max_output_tokens is not None else {}),
                )
                if self.api_max_retries is not None:
                    # 复用 Hermes 每次模型请求的退避，不重放已经完成的工具调用。
                    agent._api_max_retries = self.api_max_retries
                if self.provider == "anthropic":
                    apply_anthropic_messages_client(
                        agent, base_url=self.base_url, api_key=self._api_key
                    )
                compressor = getattr(agent, "context_compressor", None)
                if self.agent_context_length is not None:
                    if not callable(getattr(compressor, "update_model", None)):
                        raise HermesUnavailableError("Hermes 缺少可配置上下文的原生压缩器")
                    # 网关别名可能缺少窗口元数据；沿用原生预算更新，不能提前摘要完整轨迹。
                    compressor.update_model(
                        model=compressor.model,
                        context_length=self.agent_context_length,
                        base_url=compressor.base_url,
                        api_key=compressor.api_key,
                        provider=compressor.provider,
                        api_mode=compressor.api_mode,
                    )
                    agent._config_context_length = self.agent_context_length
                if compressor is not None:
                    # 摘要请求失败时保留消息，由原生有界溢出处理返回错误，不能静默丢弃历史。
                    compressor.abort_on_summary_failure = True
                # Conversation loop prefers SSE even in quiet mode. Reconstruction
                # must not depend on apply() seeing `_anthropic_client`.
                agent._disable_streaming = True
                # Hermes native dispatch must use the role-scoped proxy.
                trace_path = Path(output_root) / "private" / "tool_events.jsonl"
                _bind_agent_tools(agent, role=role, session=session, trace_path=trace_path)
                with pin_hermes_compression(
                    agent, context_length=self.agent_context_length, timeout_seconds=request_timeout,
                ), _scoped_hermes_dispatch(agent, role=role):
                    current_instruction = instruction
                    history = copy.deepcopy(session.conversation.messages) if session.conversation is not None else None
                    try:
                        source_session = json.loads(session.session_context or "null")
                    except json.JSONDecodeError:
                        source_session = None
                    if not isinstance(source_session, dict) or "messages" not in source_session:
                        source_session = None
                    for attempt in range(2):
                        protection = protect_source_history(
                            compressor, [*(history or []), {"role": "user", "content": current_instruction}],
                            source_session,
                        )
                        kwargs = {"conversation_history": history} if history else {}
                        raw = agent.run_conversation(
                            current_instruction, system_message=role.identity,
                            task_id=role.name, **kwargs,
                        )
                        if not isinstance(raw, dict):
                            raise TypeError("Hermes result must be a dict")
                        if session.conversation is not None and isinstance(raw.get("messages"), list) and raw["messages"]:
                            session.conversation.messages = copy.deepcopy(raw["messages"])
                        final_text = str(raw.get("final_response") or "")
                        turn = {
                            "messages": len(raw.get("messages") or []),
                            "api_calls": raw.get("api_calls"),
                            "api_max_retries": getattr(agent, "_api_max_retries", None),
                            "completed": raw.get("completed"),
                            "model": self.model_name,
                            "provider": self.provider,
                            "tool_events": len(session.tool_events),
                            "policy_errors": list(session.policy_errors),
                            "request_timeout_seconds": request_timeout,
                            "max_output_tokens": role.max_output_tokens,
                            "agent_context_length": self.agent_context_length,
                            "resolved_context_length": getattr(compressor, "context_length", None),
                            "compression_threshold_tokens": getattr(compressor, "threshold_tokens", None),
                            "compression_threshold_percent": getattr(compressor, "threshold_percent", None),
                            "compression_count": getattr(compressor, "compression_count", None),
                            "aux_compression_context_length": getattr(
                                agent, "_aux_compression_context_length_config", None),
                            "compression_request_timeout_seconds": request_timeout if compressor else None,
                            "continued_messages": len(history or []),
                            "source_history_protection": {
                                **protection,
                                "returned_source_message_indices": list(source_session_message_indices(
                                    raw.get("messages") or [], source_session,
                                )) if source_session is not None else [],
                            },
                            "native_usage": {
                                "scope": "Hermes 原生会话累计及末次主请求字段，不含辅助摘要请求用量",
                                **{key: raw[key] for key in (
                                    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                                    "reasoning_tokens", "prompt_tokens", "completion_tokens",
                                    "total_tokens", "last_prompt_tokens",
                                ) if key in raw},
                            },
                        }
                        turns.append(turn)
                        if attempt:
                            turn["final_response"] = final_text
                            turn["response_sha256"] = hashlib.sha256(final_text.encode()).hexdigest()
                        if raw.get("error") and not raw.get("completed"):
                            detail = str(raw["error"]).replace(self._api_key, "[credential removed]")[:2000]
                            turn["error_detail"] = detail
                            final_text = final_text or detail
                            code = ("MODEL_RATE_LIMIT" if "rate limit" in detail.lower()
                                    or "ratelimit" in detail.lower()
                                    else classify_hermes_failure(final_text) or "MODEL_API_FAILED")
                            errors.append(code)
                            break
                        infra = classify_hermes_failure(final_text)
                        if infra:
                            errors.append(infra)
                            break
                        if role.result_schema == "text/plain":
                            payload = {"answer": final_text} if final_text.strip() else {}
                            break
                        try:
                            payload = parse_json_object(final_text) if final_text.strip() else {}
                            break
                        except ModelGatewayError as exc:
                            turn["output_error"] = exc.code
                            turn["final_response"] = final_text
                            turn["response_sha256"] = hashlib.sha256(final_text.encode()).hexdigest()
                            if (
                                exc.code != "INVALID_JSON" or attempt
                                or not raw.get("completed") or session.policy_errors
                            ):
                                raise
                            previous = raw.get("messages")
                            history = list(previous) if isinstance(previous, list) and previous else [
                                {"role": "user", "content": instruction},
                                {"role": "assistant", "content": final_text},
                            ]
                            current_instruction = (
                                "上一条完整角色结果存在 JSON 语法错误（INVALID_JSON）。"
                                "请仅修正该结果的 JSON 语法，保留全部字段、义务、证据和值；"
                                "不要重新执行任务或调用工具，不要删掉出错字段，也不要只返回嵌套片段。"
                                "返回完整的根 JSON 对象，不加说明或 Markdown。错误位置：" + str(exc)
                            )
                            # Hermes 仅在 api_calls < max_iterations 时标记正常结束。
                            # 无工具的格式补正留一个结束余量；外层仍只允许补正一次。
                            agent.max_iterations = 2
                            _bind_agent_tools(agent, role=replace(role, tools=()), session=session,
                                              trace_path=trace_path)
        except Exception as exc:
            errors.append(getattr(exc, "code", None) or type(exc).__name__)
            payload = {}
        finally:
            os.chdir(cwd)
            if session.conversation is not None:
                history_path = Path(output_root) / "private" / "conversation.json"
                history_path.parent.mkdir(parents=True, exist_ok=True)
                history_path.write_text(json.dumps(session.conversation.messages, ensure_ascii=False) + "\n")
        if not sandboxed:
            after = workspace_tree_hash(workdir)
            unexpected = _unexpected_workspace_changes(session, before, after)
            if unexpected:
                errors.extend(unexpected)
                payload = {}
        if session.policy_errors:
            errors.extend(session.policy_errors)
            payload = {}
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
    api_mode: str | None = None,
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
    settings: dict[str, Any] = {}
    if base_url and api_key:
        url, key = _messages_base_url(base_url), api_key
    else:
        if config_path is None:
            raise HermesUnavailableError("必须提供 config.yaml，以便给 Hermes Agent 配置模型")
        url, key = load_channel_connection(config_path, channel)
        settings = dict(iter_config_items(config_path)).get(channel, {})
        api_mode = api_mode or settings.get("agent_api_mode")
    return HermesNativeRuntime(
        factory=resolved_factory,
        base_url=url,
        api_key=key,
        model_name=model_name,
        provider=provider or provider_for_channel(channel, model_name),
        api_mode=api_mode,
        api_max_retries=settings.get("agent_api_max_retries"),
        agent_context_length=settings.get("agent_context_length"),
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
    # 仅识别已知 provider；网关模型自身的路由斜杠不能充当 provider 分隔符。
    prefix, separator, _ = model_name.partition("/")
    if separator and prefix in {"anthropic", "openai", "deepseek", "vol", "bailian", "google", "gemini", "gpt"}:
        return prefix
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
def pin_hermes_timeout_env(default_timeout: float = 120.0) -> Iterator[float]:
    """把 Hermes 流式 / 非流式 stale 钉到重建超时，避免 180s × 多次空转。

    TokenHub 长时间不吐 token 是模型侧；管线必须在超时后真正关掉
    Anthropic 连接，而不是去重建缺 ``OPENAI_API_KEY`` 的 OpenAI 客户端。
    """

    timeout_seconds = _traceforge_model_timeout_seconds(default_timeout)
    names = (
        "TRACEFORGE_MODEL_TIMEOUT_SECONDS",
        "HERMES_STREAM_STALE_TIMEOUT",
        "HERMES_API_CALL_STALE_TIMEOUT",
        "HERMES_STREAM_RETRIES",
    )
    saved = {name: os.environ.get(name) for name in names}
    os.environ["TRACEFORGE_MODEL_TIMEOUT_SECONDS"] = str(timeout_seconds)
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


def protect_source_history(
    compressor: Any, messages: list[dict[str, Any]], raw_session: dict[str, Any] | None,
) -> dict[str, Any]:
    """仅在输入逐值包含原轨迹时扩大原生保护前缀，不替换或生成摘要。"""
    indices = source_session_message_indices(messages, raw_session) if raw_session is not None else ()
    receipt: dict[str, Any] = {
        "status": "SOURCE_NOT_PRESENT" if raw_session is not None else "SOURCE_CONTEXT_UNKNOWN",
        "source_message_indices": list(indices),
        "source_prefix_end": max(indices) + 1 if indices else None,
        "native_protect_first_n": getattr(compressor, "protect_first_n", None),
    }
    if indices:
        if compressor is None or not hasattr(compressor, "protect_first_n"):
            receipt["status"] = "NATIVE_COMPRESSOR_UNAVAILABLE"
        else:
            # 原生 protect_first_n 不含首条 system；Hermes 可能在无 system 的输入前补一条。
            system_head = int(bool(messages) and messages[0].get("role") == "system")
            compressor.protect_first_n = max(
                compressor.protect_first_n, max(indices) + 1 - system_head,
            )
            receipt.update(status="PROTECTED", native_protect_first_n=compressor.protect_first_n)
    return receipt


@contextmanager
def pin_hermes_compression(
    agent: Any, *, context_length: int | None, timeout_seconds: float,
) -> Iterator[None]:
    """在现有进程锁内绑定摘要预算，退出时恢复原生辅助超时读取。"""
    compressor = getattr(agent, "context_compressor", None)
    if compressor is None:
        yield
        return
    auxiliary = importlib.import_module("agent.auxiliary_client")
    if context_length is not None:
        client, model = auxiliary.get_text_auxiliary_client(
            "compression", main_runtime={
                key: getattr(compressor, key)
                for key in ("model", "provider", "base_url", "api_key", "api_mode")
            },
        )
        # 辅助模型单独解析窗口；只有同模型、同端点才能继承显式主窗口。
        if (client is not None and model == compressor.model
                and str(getattr(client, "base_url", "")).rstrip("/")
                == compressor.base_url.rstrip("/")):
            agent._aux_compression_context_length_config = context_length
    original_timeout = auxiliary._get_task_timeout

    def task_timeout(task: str, *args: Any, **kwargs: Any) -> float:
        if task == "compression":
            return timeout_seconds
        return original_timeout(task, *args, **kwargs)

    auxiliary._get_task_timeout = task_timeout
    try:
        yield
    finally:
        auxiliary._get_task_timeout = original_timeout


def _traceforge_model_timeout_seconds(default: float = 120.0) -> float:
    """重建角色的模型窗口：默认可覆盖，且夹在 5–600s。"""

    try:
        timeout_seconds = float(os.environ.get("TRACEFORGE_MODEL_TIMEOUT_SECONDS", default))
    except ValueError:
        timeout_seconds = default
    return max(5.0, min(timeout_seconds, 600.0))


def _traceforge_agent_max_iterations(role: AgentRole) -> int:
    """显式预算覆盖角色默认值，避免调高预算后仍被默认值截断。"""

    raw = os.environ.get("TRACEFORGE_AGENT_MAX_ITERATIONS", "").strip()
    if not raw:
        return role.max_iterations
    try:
        limit = int(raw)
    except ValueError:
        return role.max_iterations
    return max(1, limit)


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
        "max_retries": 0,
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
    # 原生恢复会绕过此客户端、再完整重试一轮；只由会话循环负责一次重试。
    agent._try_recover_primary_transport = lambda *args, **kwargs: False
    agent._api_max_retries = 2
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
            # 摘要绑定传入 Hermes 的原始字节；落盘副本仅过滤私有思考。
            "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            "instruction": _omit_reasoning_text(instruction),
            "turns": turns,
            "tool_events": tool_events or [],
            "final_text": _omit_reasoning_text(final_text) if isinstance(final_text, str) else final_text,
            "credentials_embedded": False,
            "privacy": {
                "private_thinking_reasoning": "omitted",
                "instruction_sha256_basis": "original_utf8_before_privacy_filtering",
            },
        }
    )
    path = private / "agent_trace.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_tool_results(
    output_root: str | Path, tool_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将当前执行摘要与完整工具回执逐条绑定，不从预览猜测实际读取内容。"""
    if not tool_events:
        return []
    path = Path(output_root) / "private/tool_events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"工具完整回执不可读：{path}（{exc}）") from exc
    finished = []
    for line_number, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"工具完整回执 JSON 损坏：{path}:{line_number}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"工具完整回执不是对象：{path}:{line_number}")
        if record.get("status") == "FINISHED":
            finished.append(record)
    if len(finished) != len(tool_events):
        raise ValueError(
            f"工具完整回执数量不匹配：{path}，执行 {len(tool_events)} 条，完成 {len(finished)} 条")
    keys = ("tool_call_id", "name", "arguments", "result_sha256", "ok")
    results = []
    for index, (event, record) in enumerate(zip(tool_events, finished, strict=True), 1):
        if (not isinstance(event, dict)
                or any(key not in event or key not in record for key in keys)):
            raise ValueError(f"工具完整回执缺少执行绑定字段：{path}，第 {index} 条")
        result = record.get("result")
        if not isinstance(result, str):
            raise ValueError(f"工具完整回执没有原始正文：{path}，第 {index} 条")
        if hashlib.sha256(result.encode("utf-8")).hexdigest() != record["result_sha256"]:
            raise ValueError(f"工具完整回执正文哈希不匹配：{path}，第 {index} 条")
        if any(event[key] != record[key] for key in keys):
            raise ValueError(
                f"工具完整回执与当前执行不匹配：{path}，第 {index} 条 {event['tool_call_id']}")
        results.append({**copy.deepcopy(event), "result": result})
    return results


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
    """角色越权仍终止；写入前的内容校验失败允许纠正，最终候选继续完整校验。"""

    if not result.startswith("error:"):
        return False
    return (
        "cannot write files" in result
        or (
            function_name == "write_test"
            and "unsafe path" in result
        )
    )


def _bind_agent_tools(
    agent: Any, *, role: AgentRole, session: AgentSession, trace_path: Path | None = None,
) -> None:
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

    def record(event: dict[str, Any]) -> None:
        if trace_path is not None:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            with trace_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": time.time(), **event}, ensure_ascii=False) + "\n")

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
            record({"status": "STARTED", "name": function_name,
                    "tool_call_id": tool_call_id, "arguments": function_args})
            result = execute_tool(function_name, function_args, session)
            # 普通工具只保留预览；pytest 历史会被测试重写清空，必须在私有轨迹中
            # 保留完整结果及执行时源码。当前验收仍只读取 session.pytest_runs。
            event = {
                "name": function_name,
                "tool_call_id": tool_call_id,
                "ok": not result.startswith("error:"),
                "arguments": copy.deepcopy(function_args),
                "result_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
                "result_preview": result[:512],
            }
            if function_name == "run_pytest":
                source = session.test_outputs_py
                event.update({
                    "result": result,
                    "test_outputs_py": source,
                    "test_sha256": (
                        hashlib.sha256(source.encode("utf-8")).hexdigest()
                        if source is not None else None
                    ),
                })
            session.tool_events.append(event)
            record({"status": "FINISHED", **event, "result": result})
            fatal = is_fatal_tool_result(function_name, result)
            if fatal:
                session.policy_errors.append(result.removeprefix("error:").strip())
            return result

    agent._invoke_tool = MethodType(invoke, agent)
    agent._traceforge_tool_names = set(role.tools)


def _omit_reasoning_text(value: str) -> str:
    cleaned = re.sub(r"(?is)<thinking>.*?</thinking>", "", value)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return cleaned
    visible = omit_private_reasoning(parsed)
    return cleaned if visible == parsed else json.dumps(visible, ensure_ascii=False)


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


def _execute_final_verifier(role: AgentRole, session: AgentSession,
                            result: AgentResult, output_root: Path) -> None:
    """候选交付后补齐本轮真实执行，不依赖模型记住重建沙箱内的测试状态。"""
    if (role.result_schema != "traceforge.verifier-candidate.v1" or not result.completed
            or result.errors or result.payload.get("status") != "READY"):
        return
    groups = [result.payload.get(key) for key in ("missing_capability_tests", "protective_tests")]
    if any(not isinstance(group, list) or not group
           or any(not isinstance(name, str) or not name for name in group) for group in groups):
        return
    source = session.test_outputs_py or result.payload.get("test_outputs_py")
    if not isinstance(source, str) or not source.strip():
        return
    names = list(dict.fromkeys(name for group in groups for name in group))
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    executed = {run.get("name") for run in session.pytest_runs
                if run.get("test_sha256") == digest and run.get("input_unchanged") is True
                and run.get("status") in {"PASS", "FAIL"}}
    if set(names) <= executed:
        return
    runner = SimpleNamespace()
    _bind_agent_tools(runner, role=role, session=session,
                      trace_path=Path(output_root) / "private/tool_events.jsonl")
    written = runner._invoke_tool("write_test", {"content": source}, role.name,
                                  tool_call_id="pipeline-final-write-test")
    if written.startswith("error:"):
        result.errors.append(written)
        return
    runner._invoke_tool("run_pytest", {"names": names}, role.name,
                        tool_call_id="pipeline-final-run-pytest")


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
        # 文件语义审查只读已绑定的宿主快照；不能误建未上传证据的空沙箱。
        if role.name in {"intent", "session_tasks", "file_artifact_review"}:
            return self.inner.run(
                role=role,
                instruction=instruction,
                session=session,
                output_root=output_root,
            )

        from traceforge.reconstruction.agents.sandbox import prepare_role_sandbox, run_coro

        runtime = None
        result: AgentResult | None = None
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
                result = AgentResult(
                    role=role.name,
                    backend=self.backend,
                    payload={},
                    errors=[init_error],
                    final_text=None,
                    completed=False,
                )
                return result
            try:
                # 新沙箱不能继承上一轮执行回执；测试正文可复用，仍需实际重跑。
                session.pytest_runs.clear()
                result = self.inner.run(
                    role=role,
                    instruction=instruction,
                    session=session,
                    output_root=output_root,
                )
                result.backend = self.backend
                _execute_final_verifier(role, session, result, Path(output_root))
            except Exception as exc:
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
            if runtime is not None:
                try:
                    run_coro(runtime.stop(delete=True))
                    session.sandbox_stopped = True
                except Exception as exc:
                    session.sandbox_cleanup_error = f"{type(exc).__name__}:{exc}"
                    session.policy_errors.append(f"CONTAINER_CLEANUP_ERROR:{type(exc).__name__}")
                    # 准备中断也要清理；已产生的返回值必须携带清理失败。
                    if result is not None:
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
            # 采集修复声明与实际工具写入绑定，JSON 不能替换或丢弃其审计来源。
            for key in ("capture_repairs", "dependency_source"):
                if key in item:
                    existing[key] = item[key]
                else:
                    existing.pop(key, None)
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
