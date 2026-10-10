"""角色 Agent 的会话状态与受控工具。所有路径均相对隔离 workspace。"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from traceforge.reconstruction.capture_repair import (
    CAPTURE_REPAIRS_SCHEMA,
    apply_capture_repairs,
    capture_repair_error,
)

MAX_TOOL_RESULT_CHARS = 8000


@dataclass
class AgentConversation:
    """由调用方显式共享的研究者会话；独立求解和审查不传此对象。"""
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentSession:
    conversation: AgentConversation | None = None
    workspace: Path | None = None
    user_texts: list[str] = field(default_factory=list)
    user_records: list[dict[str, Any]] = field(default_factory=list)
    # 原文保留分页复查入口；首次作者输入由重建编排完整提供。
    session_context: str | None = None
    tool_names: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    replay_files: dict[str, str] = field(default_factory=dict)
    protected_paths: set[str] = field(default_factory=set)
    partial_files: dict[str, str] = field(default_factory=dict)
    complete_files: dict[str, str] = field(default_factory=dict)
    prior_capture_repairs: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    listing_names: set[str] = field(default_factory=set)
    body_paths: set[str] = field(default_factory=set)
    required_paths: set[str] = field(default_factory=set)
    web_search_handler: Any = None
    web_open_handler: Any = None
    view_image_handler: Callable[[str], str | dict[str, Any]] | None = None
    candidate_check_handler: Callable[..., dict[str, Any]] | None = None
    dependency_bundle: Path | None = None
    repair_feedback: dict[str, Any] | None = None
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
    probe_output_history: list[dict[str, Any]] = field(default_factory=list)
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


def source_evidence_refs(evidence: list[dict[str, Any]]) -> set[str]:
    """引用须唯一可定位；参与模型推断不等于可直接物化为初态。"""
    from collections import Counter

    counts = Counter(str(item.get("evidence_ref_id") or "") for item in evidence)
    return {ref for ref, count in counts.items() if ref and count == 1}


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
    from traceforge.reconstruction.environment_probe import PROBE_PURPOSES
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
        "list_evidence": ("列出可读证据及已知来源地址、查询、版本和类型；正文用 read_evidence 读取。", {}, []),
        "search_evidence": (
            "按字面关键词检索已交付原始证据，返回来源 id 和 read_evidence 可续读的字符位置；不联网。",
            {"query": text, "offset": {"type": "integer", "minimum": 0}},
            ["query"],
        ),
        "read_evidence": (
            "读取原始证据；可指定 path 只读取解析器已定位的该文件观察，正文不改写。支持 offset/limit 分页。",
            {"id": text, "path": text, **page},
            ["id"],
        ),
        "write_file": (
            "写入有证据支持的初态。COMPLETE 仅允许显式 capture_repairs；不得覆盖 UNKNOWN、ABSENT 或工作区外路径。",
            {
                "path": text,
                "content": text,
                "evidence_ref_ids": {"type": "array", "items": text, "minItems": 1},
                "capture_repairs": CAPTURE_REPAIRS_SCHEMA,
            },
            ["path", "content", "evidence_ref_ids"],
        ),
        "repair_capture": (
            "修复已有 COMPLETE/PARTIAL 采集文件，无需重复提交完整正文。"
            "每次只提供新增或更新的 capture_repairs；old_text 始终定位最初捕获，"
            "相同 old_text 更新已有声明，其余成功修改自动保留。"
            "撤销某项修改时将其 new_text 设回 old_text。"
            "PARTIAL 可用 append_content 提交完整缺失尾部；省略时保留已有尾部。"
            "仍校验证据、唯一匹配和写入权限，不恢复 UNKNOWN/ABSENT，不实现目标功能。",
            {"path": text, "capture_repairs": CAPTURE_REPAIRS_SCHEMA, "append_content": text,
             "evidence_ref_ids": {"type": "array", "items": text, "minItems": 1}},
            ["path", "capture_repairs", "evidence_ref_ids"],
        ),
        "restore_observed_file": (
            "从选定的初态全文观察直接恢复文件；分段观察按原行号拼接，重叠必须一致且从 1 连续。"
            "不解码、不猜测缺失行、不应用目标补丁；覆盖当前候选前先读取来源。",
            {"path": text, "evidence_ref_ids": {"type": "array", "items": text, "minItems": 1}},
            ["path", "evidence_ref_ids"],
        ),
        "edit_candidate_file": (
            "对当前候选执行文本替换，自动记录相对最初捕获的修复声明。"
            "默认 old_text 必须唯一；replace_all=true 明确替换全部匹配。"
            "只恢复必要初态，不能实现用户目标；原因和原始证据必填。",
            {"path": text, "old_text": text, "new_text": text, "reason": text,
             "replace_all": {"type": "boolean"},
             "evidence_ref_ids": {"type": "array", "items": text, "minItems": 1}},
            ["path", "old_text", "new_text", "reason", "evidence_ref_ids"],
        ),
        "web_search": (
            "查询公开资料。按当前角色要求使用，检索片段不等于来源全文。",
            {"query": text},
            ["query"],
        ),
        "view_image": (
            "查看已由 web_open 保存并校验的 JPEG/PNG 原图，不重新联网。"
            "直接向当前支持视觉的模型提供原像素；来源说明不等于已读图。",
            {"url": text},
            ["url"],
        ),
        "web_open": (
            "读取公开来源正文；offset/limit 分页。PDF 文本层不可读时可显式传 ocr_page"
            "（从 1 开始）识别单页；返回带坐标/置信度的原检测序列，公式和双栏顺序未经核实。"
            "需已配置本地 OCR；不会自动 OCR 所有 PDF。",
            {"url": text, **page, "ocr_page": {"type": "integer", "minimum": 1}},
            ["url"],
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
        "read_probe_output": (
            "按当前会话已保存的 probe_id 分页读取完整 stdout/stderr，不重新执行，不接受宿主路径。",
            {"probe_id": text, "execution_index": {"type": "integer", "minimum": 0},
             "stream": {"type": "string", "enum": ["stdout", "stderr"]}, **page},
            ["probe_id"],
        ),
        "restore_dependency_source": (
            "从已校验的锁定 wheel 原成员恢复 UTF-8 源码，按行保留已确认的初态观察。"
            "仅恢复当前任务必要依赖，不代表原机器完整快照；返回 wheel、成员和候选哈希。"
            "待核观察的采集损坏可显式声明 capture_repairs："
            "old_text 来自原观察，new_text 来自锁定成员；"
            "须引用冲突来源，声明只能证明候选与 wheel 原成员一致，不能晋升观察的初态资格。"
            "仍执行原文件保护，无需模型抄写正文。",
            {"distribution": text, "version": text, "member": text, "path": text,
             "capture_repairs": CAPTURE_REPAIRS_SCHEMA,
             "evidence_ref_ids": {"type": "array", "items": text, "minItems": 1}},
            ["distribution", "version", "member", "path", "evidence_ref_ids"],
        ),
        "run_candidate": (
            "冻结当前候选，在独立 AGS 只读环境中准备依赖并运行 Python 检查。"
            "返回实际退出码、日志及候选哈希；修改源码后需要重新检查。"
            "用 assert 或异常表达失败；临时文件写入 TRACEFORGE_PROBE_SCRATCH。"
            "purpose 可为 load、dependency、reset、task_conflict，不实现用户目标功能。",
            {"python_code": text, "purpose": {"type": "string", "enum":
                ["load", "reset", "dependency", "task_conflict"]},
             "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60}},
            ["python_code", "purpose", "timeout_seconds"],
        ),
        "run_environment_probe": (
            "在只读工作区的沙盒中运行环境探针；reset 和 task_conflict 在独立临时目录重复运行。"
            "核验 solver 终态必须用 purpose=rollout，在临时副本运行；该结果不证明初态缺口。"
            "Python 当前目录已是沙盒 workspace 根目录，读取文件请用相对路径，"
            "或 os.environ['TRACEFORGE_WORKSPACE']；宿主机的产物绝对路径在沙盒中不可用。"
            "临时写入仅使用 os.environ['TRACEFORGE_PROBE_SCRATCH']，不得修改 workspace。"
            "必要条件不满足时必须 assert、raise 或非零退出；只打印错误或捕获异常后正常退出"
            "无法证明能力通过。只验证指定能力，不证明任务可解，也不得修复或求解任务。",
            {
                "python_code": text,
                "purpose": {
                    "type": "string",
                    "enum": sorted(PROBE_PURPOSES),
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


def execute_tool(
    name: str, arguments: Any, session: AgentSession,
) -> str | dict[str, Any]:
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
                    **{key: item[key] for key in
                       ("url", "query", "title", "source_ref", "source_mode",
                        "content_kind", "retrieved_at", "initial_state_eligible")
                       if key in item},
                    "chars": len(_dump(item)),
                }
                for item in session.evidence
            ]
        )
    if name == "search_evidence":
        query = args.get("query")
        offset = args.get("offset", 0)
        if not isinstance(query, str) or not query or type(offset) is not int or offset < 0:
            return "error: query 必须为非空字符串，offset 必须为非负整数"
        matches = []
        for item in session.evidence:
            text = _dump(item)
            position = text.find(query)
            while position >= 0:
                start = max(0, position - 100)
                matches.append({"evidence_ref_id": item.get("evidence_ref_id"),
                                "offset": start, "preview": text[start:position + len(query) + 100]})
                position = text.find(query, position + len(query))
        return _dump({"matches": matches[offset:offset + 10], "total_matches": len(matches),
                      "next_offset": offset + 10 if offset + 10 < len(matches) else None})
    if name == "read_evidence":
        ref = str(args.get("id") or args.get("evidence_ref_id") or "")
        for item in session.evidence:
            if str(item.get("evidence_ref_id")) == ref:
                if args.get("path"):
                    parsed = item.get("session_parse") or {}
                    observations = [op for op in [*parsed.get("file_ops", []),
                                                   *parsed.get("reference_file_ops", [])]
                                    if op.get("kind") == "read"
                                    and args["path"] in {op.get("path"), op.get("source_path")}]
                    if not observations:
                        return "error: no parsed file observation for this path; read the full record"
                    return _window(_dump({"evidence_ref_id": ref,
                                          "initial_state_eligible": item.get(
                                              "initial_state_eligible", True),
                                          "observations": observations}), args)
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
    if name == "repair_capture":
        return _repair_capture(session, args)
    if name == "restore_observed_file":
        return _restore_observed_file(session, args)
    if name == "edit_candidate_file":
        return _edit_candidate_file(session, args)
    if name == "web_search":
        return _web_search(session, args)
    if name == "web_open":
        if not callable(session.web_open_handler):
            return _dump({"success": False, "error": "页面读取后端未配置"})
        return _dump(session.web_open_handler(
            str(args.get("url") or ""), offset=args.get("offset", 0),
            limit=args.get("limit", MAX_TOOL_RESULT_CHARS),
            **({"ocr_page": args["ocr_page"]} if "ocr_page" in args else {}),
        ))
    if name == "view_image":
        if not callable(session.view_image_handler):
            return "error: 原图读取后端未配置"
        result = session.view_image_handler(str(args.get("url") or ""))
        if isinstance(result, str) and result.startswith("error:"):
            return result
        if (not isinstance(result, dict) or result.get("_multimodal") is not True
                or not isinstance(result.get("content"), list)
                or not any(
                    isinstance(part, dict) and part.get("type") == "image_url"
                    and isinstance(part.get("image_url"), dict)
                    and str(part["image_url"].get("url") or "").startswith(
                        ("data:image/jpeg;base64,", "data:image/png;base64,")
                    )
                    for part in result["content"]
                )):
            return "error: 原图后端未返回原生图像内容"
        return result
    if name == "write_test":
        return _write_test(session, args)
    if name == "run_pytest":
        return _run_pytest(session, args)
    if name == "read_probe_output":
        matches = [item for item in [*session.environment_probes, *session.probe_output_history]
                   if item.get("probe_id") == args.get("probe_id")]
        index, stream = args.get("execution_index", 0), args.get("stream", "stdout")
        if len(matches) != 1:
            return "error: 当前会话没有唯一的指定 probe_id"
        executions = matches[0].get("executions") or []
        if type(index) is not int or not 0 <= index < len(executions) or stream not in {"stdout", "stderr"}:
            return "error: 执行编号或输出流无效"
        return _window(str(executions[index].get(stream) or ""), args)
    if name == "restore_dependency_source":
        return _restore_dependency_source(session, args)
    if name == "run_candidate":
        if session.candidate_check_handler is None:
            return "error: 当前会话没有配置候选执行后端"
        return _dump(session.candidate_check_handler(
            python_code=args.get("python_code"), purpose=args.get("purpose"),
            timeout_seconds=args.get("timeout_seconds", 30),
        ))
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


def _write_candidate_edit(session: AgentSession, args: dict[str, Any], content: str, reason: str) -> str:
    """当前候选编辑只在这里转为既有来源契约，原始 Replay 永不改变。"""
    path = workspace_relpath(session, str(args.get("path") or ""))
    original = session.complete_files.get(path, session.partial_files.get(path))
    if not original:
        return "error: 只能编辑已捕获文件；新文件请使用 write_file"
    previous = next((item for item in reversed(session.writes) if item["path"] == path), {})
    refs = args.get("evidence_ref_ids")
    if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
        refs = list(dict.fromkeys([*previous.get("evidence_ref_ids", []), *refs]))
    return _write_file(session, {**args, "content": content, "evidence_ref_ids": refs,
                                "capture_repairs": [{"old_text": original, "new_text": content,
                                                     "reason": reason}]})


def _restore_observed_file(session: AgentSession, args: dict[str, Any]) -> str:
    refs = args.get("evidence_ref_ids")
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs):
        return "error: evidence_ref_ids 必须是非空字符串数组"
    path = workspace_relpath(session, str(args.get("path") or ""))
    evidence = {str(item.get("evidence_ref_id")): item for item in session.evidence}
    lines: dict[int, str] = {}
    for ref in refs:
        if evidence.get(ref, {}).get("initial_state_eligible") is False:
            return f"error: 参考观察不能直接物化为初态：{ref}:{path}"
        observations = [op for op in (evidence.get(ref, {}).get("session_parse") or {}).get("file_ops", [])
                        if op.get("kind") == "read" and op.get("path") == path]
        if not observations:
            return f"error: 没有可用的初态文件观察：{ref}:{path}"
        for observation in observations:
            numbers, contents = observation.get("line_numbers"), observation.get("line_contents")
            if observation.get("partial") is False and isinstance(observation.get("content"), str):
                contents = observation["content"].splitlines()
                numbers = list(range(1, len(contents) + 1))
            if (not isinstance(numbers, list) or not numbers or not isinstance(contents, list)
                    or len(numbers) != len(contents)):
                return f"error: 观察没有可靠的行号与正文：{ref}:{path}"
            for number, content in zip(numbers, contents):
                if type(number) is not int or number < 1 or not isinstance(content, str):
                    return f"error: 观察行号或正文无效：{ref}:{path}"
                if number in lines and lines[number] != content:
                    return f"error: 选定观察在第 {number} 行冲突，请比较来源后重新选择"
                lines[number] = content
    if len(lines) != max(lines):
        return "error: 选定观察存在缺失行，必须从第 1 行连续覆盖"
    content = "\n".join(lines[number] for number in range(1, max(lines) + 1)) + "\n"
    return _write_candidate_edit(session, args, content, "依据选定初态观察按行号恢复：" + ", ".join(refs))


def _edit_candidate_file(session: AgentSession, args: dict[str, Any]) -> str:
    path = workspace_relpath(session, str(args.get("path") or ""))
    previous = next((item for item in reversed(session.writes) if item["path"] == path), {})
    content = previous.get("content", session.replay_files.get(path))
    old, new, reason = args.get("old_text"), args.get("new_text"), args.get("reason")
    if not isinstance(content, str) or not isinstance(old, str) or not old or not isinstance(new, str):
        return "error: 需要已有候选和非空 old_text，以及字符串 new_text"
    if not isinstance(reason, str) or not reason.strip():
        return "error: 必须说明该改动如何恢复原任务初态"
    # read_file 与既有采集修复使用 LF；原始 Replay 保留原换行。
    content, old, new = (value.replace("\r\n", "\n") for value in (content, old, new))
    count = content.count(old)
    if not count or (count != 1 and args.get("replace_all") is not True):
        return f"error: 当前候选中 old_text 匹配 {count} 次；请读取后唯一定位或明确 replace_all"
    return _write_candidate_edit(session, args, content.replace(old, new), reason)



def _observation_lines(op: dict[str, Any]) -> dict[int, str]:
    numbers, lines = op.get("line_numbers"), op.get("line_contents")
    if op.get("partial") is False and isinstance(op.get("content"), str):
        lines = op["content"].splitlines()
        numbers = list(range(1, len(lines) + 1))
    if (not isinstance(numbers, list) or not isinstance(lines, list)
            or len(numbers) != len(lines) or not numbers):
        raise ValueError("原始观察缺少可验证行号，不能覆盖")
    if any(type(number) is not int or number < 1 or not isinstance(line, str)
           for number, line in zip(numbers, lines, strict=True)):
        raise ValueError("原始观察行号或正文无效")
    observed = {}
    for number, line in zip(numbers, lines, strict=True):
        if number in observed and observed[number] != line:
            raise ValueError("原始观察在同一行冲突，不能自动选择")
        observed[number] = line
    return observed


def _check_unverified_observations(
    evidence: list[dict[str, Any]], path: str, content: str, *,
    capture_repairs: list[dict[str, str]], evidence_ref_ids: list[str],
) -> list[dict[str, Any]]:
    """待核观察只作比较；显式修复不改变原始观察的初态资格。"""
    lines = content.splitlines()
    checks = []
    conflicts_found = []
    combined: dict[int, str] = {}
    for event in evidence:
        for op in (event.get("session_parse") or {}).get("reference_file_ops", []):
            if op.get("kind") != "read" or op.get("path") != path:
                continue
            blockers = [row for row in op.get("initial_state_blockers", [])
                        if row.get("path") in {None, path}]
            reasons = {row.get("reason") for row in blockers}
            if "read_after_first_mutation" in reasons or not reasons.intersection({
                "read_after_unparsed_mutation", "unparsed_mutation_scope",
                "unparsed_mutation_unscoped",
            }):
                continue
            check = {"evidence_ref_id": event["evidence_ref_id"], "path": path,
                     "initial_state_blockers": blockers}
            try:
                observed = _observation_lines(op)
            except ValueError as exc:
                raise ValueError(
                    "待核观察无法比较：" + json.dumps(check, ensure_ascii=False)
                ) from exc
            for number, line in observed.items():
                if number in combined and combined[number] != line:
                    raise ValueError(
                        f"待核观察在同一行冲突，不能自动选择：{path}:{number}"
                    )
                combined[number] = line
            conflicts = [number for number, line in observed.items()
                         if number > len(lines) or lines[number - 1] != line]
            checks.append({**check, "matched_line_count": len(observed) - len(conflicts),
                           **({"capture_repair_line_numbers": conflicts} if conflicts else {})})
            if conflicts:
                conflicts_found.append({**check, "line_numbers": conflicts})
    if not capture_repairs:
        if conflicts_found:
            raise ValueError("待核观察冲突：" + json.dumps(conflicts_found, ensure_ascii=False))
        return checks
    if not conflicts_found:
        raise ValueError("capture_repairs 没有对应的待核观察差异")
    missing_refs = {row["evidence_ref_id"] for row in conflicts_found} - set(evidence_ref_ids)
    if missing_refs:
        raise ValueError("待核观察修复必须明确引用冲突来源：" + ", ".join(sorted(missing_refs)))
    # 仅用于校验声明；未观察的行来自候选，不将拼接正文物化为历史初态。
    original_lines = content.splitlines(keepends=True)
    for number, line in combined.items():
        if number > len(original_lines):
            raise ValueError("待核观察超出锁定依赖成员，不能用修复声明截掉")
        prior = original_lines[number - 1]
        ending = prior[len(prior.rstrip("\r\n")):]
        original_lines[number - 1] = line + ending
    repair_error = capture_repair_error(
        "".join(original_lines), content, capture_repairs, complete=True, diagnostics=True,
    )
    if repair_error:
        raise ValueError(repair_error)
    return checks


def _restore_dependency_source(session: AgentSession, args: dict[str, Any]) -> str:
    """原成员正文与全部初态观察合并后，仍经过已有候选写入保护。"""
    import zipfile

    from traceforge.reconstruction.python_runtime import read_locked_wheel_member

    path = workspace_relpath(session, str(args.get("path") or ""))
    refs = args.get("evidence_ref_ids")
    known = source_evidence_refs(session.evidence)
    if not session.allow_write or not path:
        return "error: 没有写入权限或目标路径无效"
    if (not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or ref not in known for ref in refs)):
        return "error: 必须提供唯一可定位的原始 evidence_ref_ids"
    if session.dependency_bundle is None:
        return "error: 尚无已锁定依赖；先用 run_candidate 准备并检查依赖"
    requirements = next((item["content"] for item in reversed(session.writes)
                         if item["path"] == "requirements.txt"), session.replay_files.get("requirements.txt"))
    if requirements is None and session.workspace is not None:
        target = session.workspace / "requirements.txt"
        if target.is_file() and target.resolve().is_relative_to(session.workspace.resolve()):
            requirements = target.read_text()
    if not isinstance(requirements, str):
        return "error: 当前候选没有 requirements.txt"
    try:
        content, provenance = read_locked_wheel_member(
            session.dependency_bundle, requirements, distribution=args.get("distribution"),
            version=args.get("version"), member=args.get("member"),
        )
        locked_content = content
        repairs = args.get("capture_repairs", [])
        if not isinstance(repairs, list):
            raise ValueError("CAPTURE_REPAIRS_INVALID")
        observed: dict[int, str] = {}
        observation_refs = []
        for event in session.evidence:
            if event.get("initial_state_eligible") is False:
                continue
            for op in (event.get("session_parse") or {}).get("file_ops", []):
                if op.get("kind") != "read" or op.get("path") != path:
                    continue
                observation_refs.append(str(event["evidence_ref_id"]))
                for number, line in _observation_lines(op).items():
                    if number in observed and observed[number] != line:
                        raise ValueError("原始观察在同一行冲突，不能自动选择")
                    observed[number] = line
        original = session.complete_files.get(path, session.partial_files.get(path))
        if original is not None and not observed:
            raise ValueError("已有捕获缺少对应行号观察，不能用上游覆盖")
        lines = content.splitlines(keepends=True)
        changed = []
        for number, line in sorted(observed.items()):
            if number > len(lines):
                raise ValueError("上游成员短于原始已观察行")
            prior = lines[number - 1]
            if prior.rstrip("\r\n") != line:
                ending = prior[len(prior.rstrip("\r\n")):]
                lines[number - 1] = line + ending
                changed.append(number)
        content = "".join(lines)
        if repairs and content != locked_content:
            raise ValueError("待核观察修复只能恢复锁定 wheel 原成员，不能覆盖已确认的初态观察")
        unverified_checks = _check_unverified_observations(
            session.evidence, path, content, capture_repairs=repairs, evidence_ref_ids=refs,
        )
        if path in session.complete_files and content != session.complete_files[path]:
            raise ValueError("完整原始文件不能用上游扩写或替换")
        provenance.update(
            scope="LOCKED_DEPENDENCY_WITH_SOURCE_OBSERVATIONS",
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            observed_line_count=len(observed), overlaid_line_numbers=changed,
            observation_evidence_ref_ids=sorted(set(observation_refs)),
            unverified_observation_checks=unverified_checks,
            observations_sha256=hashlib.sha256(json.dumps(observed, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        )
        if repairs:
            provenance["capture_repairs"] = [dict(item) for item in repairs]
        # 此声明针对待核观察，不冒充新文件中并不存在的 Replay 原片段。
        write_args = {key: value for key, value in args.items() if key != "capture_repairs"}
        write_args.update(
            path=path, evidence_ref_ids=list(dict.fromkeys([*refs, *observation_refs])),
        )
        reason = "从锁定依赖恢复缺失正文并保留全部初态观察：" + json.dumps(provenance, ensure_ascii=False)
        result = (_write_candidate_edit(session, write_args, content, reason)
                  if original else _write_file(session, {**write_args, "content": content}))
        if not result.startswith("error:"):
            session.writes[-1]["dependency_source"] = provenance
            return json.dumps({"status": "RESTORED", "path": path, "dependency_source": provenance}, ensure_ascii=False)
        return result
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        return f"error: 无法恢复锁定依赖源码：{exc}"


def _repair_capture(session: AgentSession, args: dict[str, Any]) -> str:
    """累计保存已批准修改；后续编辑不能因漏抄声明而撤销先前修复。"""
    path = workspace_relpath(session, str(args.get("path") or ""))
    original = session.complete_files.get(path, session.partial_files.get(path))
    previous = next((item for item in reversed(session.writes) if item["path"] == path), {})
    prior = previous.get("capture_repairs", session.prior_capture_repairs.get(path, []))
    updates = args.get("capture_repairs")
    try:
        apply_capture_repairs(original, updates, diagnostics=True)
        old_body = apply_capture_repairs(original, prior, diagnostics=True)
        repairs = {item["old_text"].replace("\r\n", "\n"): item for item in prior}
        repairs.update({item["old_text"].replace("\r\n", "\n"): item for item in updates})
        merged = list(repairs.values())
        new_body = apply_capture_repairs(original, merged, diagnostics=True)
    except ValueError as exc:
        return f"error: {exc}:{path}"
    current = previous.get("content", session.replay_files.get(path, original))
    current = current.replace("\r\n", "\n")
    if not old_body or current.count(old_body) != 1:
        return f"error: 当前候选无法唯一定位已声明修复的原片段，请先读取候选：{path}"
    prefix, _, suffix = current.partition(old_body)
    suffix = args.get("append_content", suffix)
    if not isinstance(suffix, str):
        return "error: append_content 必须为字符串"
    refs = args.get("evidence_ref_ids")
    if isinstance(refs, list) and refs and all(isinstance(ref, str) for ref in refs):
        refs = list(dict.fromkeys([*previous.get("evidence_ref_ids", []), *refs]))
    return _write_file(session, {**args, "content": prefix + new_body + suffix,
                                "capture_repairs": merged, "evidence_ref_ids": refs})


def _write_file(session: AgentSession, args: dict[str, Any]) -> str:
    if not session.allow_write:
        return "error: this agent cannot write files"
    path = workspace_relpath(session, str(args.get("path") or ""))
    if path is None or path == "":
        return "error: unsafe path"
    if path in session.protected_paths and path not in session.complete_files:
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
    capture_repairs = args.get("capture_repairs", session.prior_capture_repairs.get(path, []))
    repair_error = capture_repair_error(
        session.complete_files.get(path, session.partial_files.get(path)), content, capture_repairs,
        complete=path in session.complete_files, diagnostics=True,
    )
    if repair_error:
        return f"error: {repair_error}:{path}"
    repair_metadata = (
        {"capture_repairs": [dict(item) for item in capture_repairs]}
        if "capture_repairs" in args or path in session.prior_capture_repairs else {}
    )
    refs = args.get("evidence_ref_ids")
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(item, str) or not item for item in refs)
    ):
        return "error: evidence_ref_ids required"
    known = source_evidence_refs(session.evidence)
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
                **repair_metadata,
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
        if canonical in session.protected_paths and not (
            canonical == path and path in session.complete_files
        ):
            return f"error: PROTECTED_FILE_OVERWRITE:{canonical}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    session.writes.append(
        {
            "path": path,
            "content": content,
            "provenance": "MODEL_COMPLETED",
            "evidence_ref_ids": list(dict.fromkeys(refs)),
            **repair_metadata,
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
