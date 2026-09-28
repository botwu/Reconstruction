"""角色 Agent 的会话状态与受控工具。所有路径均相对隔离 workspace。"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

MAX_TOOL_RESULT_CHARS = 8000


@dataclass
class AgentSession:
    workspace: Path | None = None
    user_texts: list[str] = field(default_factory=list)
    user_records: list[dict[str, Any]] = field(default_factory=list)
    # 完整 raw session 只通过分页工具提供，避免把超大 capture 拼进初始 prompt。
    session_context: str | None = None
    tool_names: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    replay_files: dict[str, str] = field(default_factory=dict)
    protected_paths: set[str] = field(default_factory=set)
    partial_files: dict[str, str] = field(default_factory=dict)
    listing_names: set[str] = field(default_factory=set)
    body_paths: set[str] = field(default_factory=set)
    required_paths: set[str] = field(default_factory=set)
    web_search_handler: Any = None
    allow_write: bool = False
    allow_tests: bool = False
    allow_exec: bool = False
    allow_environment_probe: bool = False
    sandbox: Any = None
    path_aliases: list[Path] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    test_outputs_py: str | None = None
    pytest_runs: list[dict[str, Any]] = field(default_factory=list)
    environment_probes: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    policy_errors: list[str] = field(default_factory=list)
    sandbox_started: bool = False
    sandbox_stopped: bool = False
    sandbox_cleanup_error: str | None = None
    read_only_probe_blocked: bool | None = None
    read_only_probe: dict[str, Any] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if self.user_records:
            self.user_texts = [str(item.get("text") or "") for item in self.user_records]
        elif self.user_texts:
            self.user_records = [
                {"id": f"user:{index}", "message_index": index, "text": text}
                for index, text in enumerate(self.user_texts)
            ]


WORKSPACE_REMOTE_PREFIXES = (
    "/home/user/workspace/",
    "/home/user/workspace",
)


def safe_relpath(value: str) -> str | None:
    # Do not lstrip("./"): that turns /etc/file and ../file into accepted paths.
    raw = str(value or "").replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or "\x00" in raw or path.is_absolute() or ".." in path.parts:
        return None
    if not path.parts or (path.parts and ":" in path.parts[0]):
        return None
    return path.as_posix()


_HERMES_TREE_MARKERS = ("/hermes_scratch/", "/hermes_workspace/")


def _relpath_from_hermes_tree(raw: str) -> str | None:
    """Hermes cwd 泄漏时，把宿主机绝对路径收成工作区相对路径。"""

    normalized = str(raw or "").replace("\\", "/")
    for marker in _HERMES_TREE_MARKERS:
        index = normalized.find(marker)
        if index == -1:
            continue
        return safe_relpath(normalized[index + len(marker) :])
    for suffix in ("/hermes_scratch", "/hermes_workspace"):
        if normalized.endswith(suffix):
            return ""
    return None


def workspace_relpath(session: AgentSession, value: str) -> str | None:
    """Accept a relative path, or an absolute path that stays inside the workspace."""

    raw = str(value or "").replace("\\", "/").strip()
    if not raw or raw in {".", "./"}:
        return ""
    relative = safe_relpath(raw)
    if relative is not None:
        return relative
    hosted = _relpath_from_hermes_tree(raw)
    if hosted is not None:
        return hosted
    for prefix in WORKSPACE_REMOTE_PREFIXES:
        if raw == prefix.rstrip("/"):
            return ""
        if raw.startswith("/home/user/workspace/"):
            return safe_relpath(raw[len("/home/user/workspace/") :])
    if ":" in PurePosixPath(raw).parts[0]:
        return None
    roots: list[Path] = []
    if session.workspace is not None:
        roots.append(session.workspace)
    roots.extend(session.path_aliases)
    if not roots:
        return None
    try:
        candidate = Path(raw)
        for root in roots:
            resolved_root = root.resolve()
            resolved = candidate.resolve() if candidate.is_absolute() else (resolved_root / raw).resolve()
            if not _is_relative_to(resolved, resolved_root):
                continue
            rel = resolved.relative_to(resolved_root).as_posix()
            return "" if rel == "." else rel
    except OSError:
        return None
    return None


def tool_schemas(names: tuple[str, ...]) -> list[dict[str, Any]]:
    """OpenAI schemas consumed directly by Hermes' native conversation loop."""
    text = {"type": "string"}
    page = {
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Character offset; use to retrieve the next page without losing content.",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_TOOL_RESULT_CHARS},
    }
    specs = {
        "list_user_texts": (
            "List tagged user texts with stable ids (user:<message_index>) and original message_index.",
            {},
            [],
        ),
        "read_session_context": (
            "按 offset/limit 只读分页查看完整会话，必要时用于消解任务指代；上下文不能新增用户义务。",
            {**page},
            [],
        ),
        "read_session_message": (
            "按原始 message_index 只读查看一条会话消息，可用 offset/limit 续读；保留当前任务边界。",
            {
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Original session message_index.",
                },
                **page,
            },
            ["index"],
        ),
        "read_user_text": (
            "Read one user text by stable id or original message_index; paginate with offset and limit.",
            {
                "id": {"type": "string", "description": "Stable user-text id such as user:129."},
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Original session message_index, not a local 0-based tool index.",
                },
                **page,
            },
            [],
        ),
        "list_tool_names": ("List tools observed in the source session.", {}, []),
        "list_dir": ("List workspace files under a relative path.", {"path": text}, []),
        "read_file": (
            "Read a workspace file; paginate with offset and limit.",
            {"path": text, **page},
            ["path"],
        ),
        "list_evidence": ("List the evidence available to this session.", {}, []),
        "read_evidence": (
            "Read an evidence record as JSON text; paginate with offset and limit.",
            {"id": text, **page},
            ["id"],
        ),
        "write_file": (
            "Write evidence-backed initial context. COMPLETE files and paths outside workspace are forbidden.",
            {
                "path": text,
                "content": text,
                "evidence_ref_ids": {"type": "array", "items": text, "minItems": 1},
            },
            ["path", "content", "evidence_ref_ids"],
        ),
        "web_search": (
            "Optional. Search typical project layout, dependency names, and common filenames. "
            "Do not write retrieved source into workspace paths.",
            {"query": text},
            ["query"],
        ),
        "write_test": (
            "Write the hidden pytest file test_outputs.py. Never write this into the public workspace.",
            {"content": text},
            ["content"],
        ),
        "run_pytest": (
            "Run named pytest functions in the sandbox only. Never execute generated tests on the host.",
            {"names": {"type": "array", "items": text, "minItems": 1}},
            ["names"],
        ),
        "run_environment_probe": (
            "在只读工作区的沙盒中运行环境探针；reset 和 task_conflict 在独立临时目录重复运行。"
            "Python 当前目录已是沙盒 workspace 根目录，读取文件请用相对路径，"
            "或 os.environ['TRACEFORGE_WORKSPACE']；宿主机的产物绝对路径在沙盒中不可用。"
            "临时写入仅使用 os.environ['TRACEFORGE_PROBE_SCRATCH']，不得修改 workspace。"
            "必要条件不满足时必须 assert、raise 或非零退出；只打印错误或捕获异常后正常退出"
            "无法证明能力通过。只验证指定能力，不证明任务可解，也不得修复或求解任务。",
            {
                "python_code": text,
                "purpose": {
                    "type": "string",
                    "enum": ["load", "reset", "dependency", "task_conflict"],
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
            },
            ["python_code", "purpose", "timeout_seconds"],
        ),
    }
    unknown = set(names) - specs.keys()
    if unknown:
        raise ValueError(f"Unsupported reconstruction tools: {sorted(unknown)}")
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": specs[name][0],
                "parameters": {
                    "type": "object",
                    "properties": specs[name][1],
                    "required": specs[name][2],
                    "additionalProperties": False,
                },
            },
        }
        for name in names
    ]


def tool_catalog(names: tuple[str, ...]) -> str:
    return "\n".join(
        f"- {item['function']['name']}: {item['function']['description']}"
        for item in tool_schemas(names)
    )


def execute_tool(name: str, arguments: Any, session: AgentSession) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    if name == "list_user_texts":
        return _dump(
            [
                {
                    "id": item["id"],
                    "message_index": item.get("message_index"),
                    "index": item.get("message_index"),
                    "preview": str(item.get("text") or "")[:160],
                    "chars": len(str(item.get("text") or "")),
                }
                for item in _session_user_records(session)
            ]
        )
    if name == "read_session_context":
        if not isinstance(session.session_context, str):
            return "error: complete session context unavailable"
        return _window(session.session_context, args)
    if name == "read_session_message":
        return _read_session_message(session, args)
    if name == "read_user_text":
        record = _resolve_user_record(session, args)
        if record is None:
            return "error: unknown user text id"
        return _window(str(record.get("text") or ""), args)
    if name == "list_tool_names":
        return _dump(session.tool_names)
    if name == "list_evidence":
        return _dump(
            [
                {
                    "evidence_ref_id": item.get("evidence_ref_id"),
                    "name": item.get("name"),
                    "chars": len(_dump(item)),
                }
                for item in session.evidence
            ]
        )
    if name == "read_evidence":
        ref = str(args.get("id") or args.get("evidence_ref_id") or "")
        for item in session.evidence:
            if str(item.get("evidence_ref_id")) == ref:
                return _window(_dump(item), args)
        return "error: unknown evidence_ref_id"
    if name == "list_dir":
        raw = str(args.get("path") or ".")
        path = workspace_relpath(session, raw)
        if path is None:
            return "error: unsafe path"
        return _dump(_list_paths(session, path or "."))
    if name == "read_file":
        path = workspace_relpath(session, str(args.get("path") or ""))
        return "error: unsafe path" if path is None else _read_file(session, path, args)
    if name == "write_file":
        return _write_file(session, args)
    if name == "web_search":
        return _web_search(session, args)
    if name == "write_test":
        return _write_test(session, args)
    if name == "run_pytest":
        return _run_pytest(session, args)
    if name == "run_environment_probe":
        from traceforge.reconstruction.environment_probe import (
            environment_probe_summary,
            run_environment_probe,
        )

        result = run_environment_probe(
            session,
            python_code=args.get("python_code"),
            purpose=args.get("purpose"),
            timeout_seconds=args.get("timeout_seconds", 30),
        )
        return environment_probe_summary(result, max_chars=MAX_TOOL_RESULT_CHARS)
    return f"error: unknown tool {name}"


def _list_paths(session: AgentSession, raw: str) -> list[str]:
    if session.sandbox is not None:
        from traceforge.reconstruction.agents.sandbox import sandbox_list_paths

        return sandbox_list_paths(session.sandbox, raw)
    prefix = safe_relpath(raw) if raw not in {"", "."} else ""
    paths: set[str] = set(session.replay_files)
    paths.update(str(item.get("path")) for item in session.writes if item.get("path"))
    if session.workspace and session.workspace.is_dir():
        root = session.workspace.resolve()
        for path in session.workspace.rglob("*"):
            if path.is_file() and _is_relative_to(path.resolve(), root):
                paths.add(path.relative_to(session.workspace).as_posix())
    return sorted(
        item for item in paths if not prefix or item == prefix or item.startswith(prefix + "/")
    )


def _read_file(session: AgentSession, path: str, args: dict[str, Any]) -> str:
    if session.sandbox is not None:
        from traceforge.reconstruction.agents.sandbox import sandbox_read_file

        text = sandbox_read_file(session.sandbox, path)
        return text if text.startswith("error:") else _window(text, args)
    for item in reversed(session.writes):
        if item.get("path") == path and isinstance(item.get("content"), str):
            return _window(item["content"], args)
    if session.workspace is not None:
        target = session.workspace / path
        try:
            resolved = target.resolve()
            root = session.workspace.resolve()
        except OSError:
            return "error: cannot resolve path"
        if not _is_relative_to(resolved, root):
            return "error: unsafe path"
        if resolved.is_file():
            try:
                return _window(resolved.read_text(encoding="utf-8"), args)
            except UnicodeDecodeError:
                return "error: binary file"
    if path in session.replay_files:
        return _window(session.replay_files[path], args)
    return "error: file not found"


def _write_test(session: AgentSession, args: dict[str, Any]) -> str:
    if not session.allow_tests:
        return "error: this agent cannot write tests"
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        return "error: test content required"
    session.test_outputs_py = content
    # A verifier rewrite invalidates runs produced for an earlier test file.
    session.pytest_runs.clear()
    if session.sandbox is not None:
        from traceforge.reconstruction.agents.sandbox import sandbox_write_test

        return sandbox_write_test(session.sandbox, content)
    return "wrote tests/test_outputs.py"


def _run_pytest(session: AgentSession, args: dict[str, Any]) -> str:
    names = args.get("names")
    if not isinstance(names, list) or not names or any(not isinstance(item, str) for item in names):
        return "error: names must be a non-empty string array"
    if session.sandbox is None:
        return "error: pytest only runs in the sandbox"
    if session.test_outputs_py is None:
        return "error: write tests before running pytest"
    from traceforge.reconstruction.agents.sandbox import sandbox_run_pytest

    test_sha256 = hashlib.sha256(session.test_outputs_py.encode("utf-8")).hexdigest()
    runs = sandbox_run_pytest(session.sandbox, tuple(names), test_sha256=test_sha256)
    for run in runs:
        run["test_sha256"] = test_sha256
    session.pytest_runs.extend(runs)
    return _dump(runs)


def _write_file(session: AgentSession, args: dict[str, Any]) -> str:
    if not session.allow_write:
        return "error: this agent cannot write files"
    path = workspace_relpath(session, str(args.get("path") or ""))
    if path is None or path == "":
        return "error: unsafe path"
    if path in session.protected_paths:
        return f"error: PROTECTED_FILE_OVERWRITE:{path}"
    from traceforge.reconstruction.completion_holes import is_runtime_log, listing_stub_error

    if is_runtime_log(path):
        return f"error: RUNTIME_LOG_NOT_WRITABLE:{path}"
    content = args.get("content")
    if not isinstance(content, str):
        return "error: content must be a string"
    stub_error = listing_stub_error(
        path,
        content,
        listing_names=session.listing_names,
        body_paths=session.body_paths,
        replay_paths=set(session.replay_files),
        required_paths=session.required_paths,
    )
    if stub_error:
        return f"error: {stub_error}"
    observed = session.partial_files.get(path)
    if observed is not None and observed not in content:
        return f"error: PARTIAL_OBSERVED_CONTENT_LOST:{path}"
    previous = next((item for item in reversed(session.writes) if item.get("path") == path), None)
    if previous is not None:
        if previous.get("content") != content:
            return f"error: DUPLICATE_CONFLICTING_PATH:{path}"
        return f"error: DUPLICATE_PATH:{path}"
    refs = args.get("evidence_ref_ids")
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(item, str) or not item for item in refs)
    ):
        return "error: evidence_ref_ids required"
    known = {str(item.get("evidence_ref_id")) for item in session.evidence}
    if any(item not in known for item in refs):
        return "error: unknown evidence_ref_id"
    if session.sandbox is not None:
        from traceforge.reconstruction.agents.sandbox import sandbox_write_file

        written = sandbox_write_file(session.sandbox, path, content)
        if written.startswith("error:"):
            return written
        session.writes.append(
            {
                "path": path,
                "content": content,
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": list(dict.fromkeys(refs)),
            }
        )
        return written
    if session.workspace is not None:
        root = session.workspace.resolve()
        target = session.workspace / path
        resolved = target.resolve()
        if not _is_relative_to(resolved, root):
            return "error: unsafe path"
        # A symlink alias must not bypass the COMPLETE-file guard.
        canonical = resolved.relative_to(root).as_posix()
        if canonical in session.protected_paths:
            return f"error: PROTECTED_FILE_OVERWRITE:{canonical}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    session.writes.append(
        {
            "path": path,
            "content": content,
            "provenance": "MODEL_COMPLETED",
            "evidence_ref_ids": list(dict.fromkeys(refs)),
        }
    )
    return f"wrote {path}"


def _web_search(session: AgentSession, args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "error: query required"
    handler = session.web_search_handler
    if callable(handler):
        try:
            result = handler(query)
        except Exception as exc:
            return f"error: web_search failed: {type(exc).__name__}"
        return result if isinstance(result, str) else _dump(result)
    return _dump(
        {
            "status": "UNAVAILABLE",
            "query": query,
            "note": "Use TOPIC_CARDS. Do not write web source into workspace paths.",
        }
    )


def collect_workspace_writes(session: AgentSession) -> list[dict[str, Any]]:
    """Return only writes made through the evidence-validating tool."""
    return list(
        {
            item["path"]: item
            for item in session.writes
            if isinstance(item.get("path"), str) and item["path"]
        }.values()
    )


def workspace_file_count(session: AgentSession) -> int:
    return len(_list_paths(session, "."))


def workspace_tree_hash(root: Path) -> dict[str, str]:
    import hashlib

    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[rel] = "symlink:" + str(path.readlink())
        elif path.is_file():
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _window(text: str, args: dict[str, Any]) -> str:
    try:
        offset = max(0, int(args.get("offset", 0)))
        limit = max(1, min(int(args.get("limit", MAX_TOOL_RESULT_CHARS)), MAX_TOOL_RESULT_CHARS))
    except (TypeError, ValueError):
        return "error: offset and limit must be integers"
    end = min(offset + limit, len(text))
    chunk = text[offset:end]
    if end < len(text):
        chunk += f"\n...[continued; use offset={end}; total_chars={len(text)}]..."
    return chunk


def _read_session_message(session: AgentSession, args: dict[str, Any]) -> str:
    if not isinstance(session.session_context, str) or not session.session_context.strip():
        return "error: session context unavailable"
    try:
        index = int(args.get("index"))
    except (TypeError, ValueError):
        return "error: index must be an integer"
    try:
        payload = json.loads(session.session_context)
    except json.JSONDecodeError:
        return "error: session context is not JSON"
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return "error: session messages unavailable"
    if index < 0 or index >= len(messages):
        return "error: unknown session message index"
    return _window(json.dumps(messages[index], ensure_ascii=False), args)


def _session_user_records(session: AgentSession) -> list[dict[str, Any]]:
    if session.user_records:
        return [item for item in session.user_records if isinstance(item, dict) and item.get("id")]
    return [
        {"id": f"user:{index}", "message_index": index, "text": text}
        for index, text in enumerate(session.user_texts)
    ]


def _resolve_user_record(session: AgentSession, args: dict[str, Any]) -> dict[str, Any] | None:
    records = _session_user_records(session)
    raw_id = args.get("id") or args.get("evidence_ref_id")
    if isinstance(raw_id, str) and raw_id.strip():
        wanted = raw_id.strip()
        return next((item for item in records if str(item.get("id")) == wanted), None)
    if "index" not in args:
        return None
    try:
        index = int(args.get("index"))
    except (TypeError, ValueError):
        return None
    return next(
        (
            item
            for item in records
            if item.get("message_index") == index
            or (item.get("message_index") is None and str(item.get("id")) == f"user:{index}")
        ),
        None,
    )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
