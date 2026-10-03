"""将 Foundry 的作者执行反馈方式接到现有 Hermes 和 AGS。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents import AgentRuntime, AgentSession, SandboxedAgentRuntime
from traceforge.reconstruction.agents.roles import AgentRole, SUFFICIENCY_ROLE
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.sandbox import prepare_role_sandbox, run_coro
from traceforge.reconstruction.agents.session import (
    AgentConversation, collect_workspace_writes, safe_relpath, workspace_tree_hash,
)
from traceforge.reconstruction.container_verification import ContainerRuntime
from traceforge.reconstruction.environment_bindings import workspace_task_context
from traceforge.reconstruction.environment_probe import (
    environment_probe_summary, run_environment_probe,
)
from traceforge.reconstruction.python_runtime import validate_python_runtime
from traceforge.reconstruction.search_tools import SearchTools
from traceforge.reconstruction.session_source import indexed_session, source_session_message_indices

COMPLETION_RESULT_CONTRACT = (
    "文件必须通过写入工具交付；实际写入是文件与来源的唯一依据，最终 JSON 只交付候选元数据，"
    "不重复返回源码或文件清单。dependencies、runtime_constraints、uncertainties 均为字符串数组。\n"
    '{"candidates":[{"dependencies":[],"runtime_constraints":[],"uncertainties":[],"decision":"READY|REVIEW"}],"open_questions":[]}'
)


def completion_metadata(result: AgentResult, session: AgentSession) -> AgentResult:
    """将作者元数据接到既有候选契约；文件继续由受控写入统一收集。"""
    candidates = result.payload.get("candidates")
    if not isinstance(candidates, list):
        return result
    current = {**session.replay_files,
               **{item["path"]: item["content"] for item in collect_workspace_writes(session)}}
    for candidate in candidates:
        declared = candidate.get("files", []) if isinstance(candidate, dict) else []
        for item in declared if isinstance(declared, list) else []:
            if isinstance(item, dict) and "content" in item:
                path = item.get("path")
                if not isinstance(path, str) or path not in current or current[path] != item["content"]:
                    return replace(result, completed=False, errors=[*result.errors, "AUTHOR_INLINE_FILES_NOT_APPLIED"])
    return replace(result, payload={**result.payload, "candidates": [
        {**item, "files": []} if isinstance(item, dict) else item for item in candidates
    ]})


def snapshot_candidate(session: AgentSession, destination: Path) -> dict[str, str]:
    """检查当前已批准写入的候选，不能退回最初 replay 或让检查改坏构建目录。"""
    destination.mkdir(parents=True, exist_ok=False)
    destination = destination.resolve()
    if session.workspace is not None and session.workspace.is_dir():
        shutil.copytree(session.workspace, destination, dirs_exist_ok=True, symlinks=True)
    files = dict(session.replay_files)
    files.update({item['path']: item['content'] for item in collect_workspace_writes(session)})
    for name, content in files.items():
        if safe_relpath(name) != name:
            raise ValueError(f"候选路径无效：{name}")
        target = destination / name
        if any(parent.is_symlink() for parent in (target, *target.parents)):
            raise ValueError(f"候选路径包含符号链接：{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    hashes = workspace_tree_hash(destination)
    if any(value.startswith("symlink:") for value in hashes.values()):
        raise ValueError("候选快照不能包含符号链接")
    return hashes


def reuse_python_runtime(bundle: Path | None, workspace: Path, target: Path) -> None:
    """只复用同一依赖声明且哈希完整的包；依赖改变后重新解析。"""
    if bundle is None or target.exists() or not (workspace / "requirements.txt").is_file():
        return
    try:
        validate_python_runtime(bundle, workspace / "requirements.txt")
    except ValueError as exc:
        if str(exc) != "PYTHON_RUNTIME_REQUIREMENTS_CHANGED":
            raise
        return
    shutil.copytree(bundle, target)


def check_candidate(
    *, session: AgentSession, runtime_factory: Callable[[], ContainerRuntime],
    output_root: Path, python_runtime: Path | None,
    python_code: str, purpose: str, timeout_seconds: int,
) -> dict[str, Any]:
    """一个工具调用完成快照、真实执行、完整落盘与沙盒清理。"""
    output_root.mkdir(parents=True, exist_ok=True)
    root = output_root / f"check-{len(list(output_root.glob('check-*'))) + 1:04d}"
    root.mkdir()
    result: dict[str, Any] = {"status": "INFRA_ERROR", "purpose": purpose,
                              "record_path": str(root / "result.json")}
    runtime = None
    try:
        result["candidate_files"] = snapshot_candidate(session, root / "workspace")
        result["candidate_sha256"] = hashlib.sha256(
            json.dumps(result["candidate_files"], sort_keys=True).encode()
        ).hexdigest()
        reuse_python_runtime(python_runtime, root / "workspace", root / "python_runtime")
        probe = AgentSession(workspace=root / "workspace")
        runtime = runtime_factory()
        run_coro(prepare_role_sandbox(role=SUFFICIENCY_ROLE, runtime=runtime,
                                     session=probe, staging_root=root))
        result.update(run_environment_probe(probe, python_code=python_code,
                                           purpose=purpose, timeout_seconds=timeout_seconds))
    except Exception as exc:
        result.update(status="INFRA_ERROR", error_code="CANDIDATE_CHECK_FAILED",
                      error=f"{type(exc).__name__}: {exc}")
    finally:
        if runtime is not None:
            try:
                run_coro(runtime.stop(delete=True))
                result["cleanup_verified"] = True
            except Exception as exc:
                result.update(status="INFRA_ERROR", cleanup_verified=False,
                              error_code="CANDIDATE_CLEANUP_FAILED",
                              error=f"{type(exc).__name__}: {exc}")
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        session.environment_probes.append(result)
    summary = json.loads(environment_probe_summary(result))
    summary["record_path"] = result["record_path"]
    summary["full_result_location"] = result["record_path"]
    summary["candidate_sha256"] = result.get("candidate_sha256")
    summary["cleanup_verified"] = result.get("cleanup_verified", False)
    return summary


def author_feedback(feedback: dict[str, Any] | None) -> dict[str, Any] | None:
    """反馈保留完整检查代码和输出，去除重复序列化及逐文件快照。原回执不变。"""
    if feedback is None:
        return None
    result = {key: value for key, value in feedback.items() if key != "failed_probes"}
    if "environment_probes" in result:
        result["environment_probes"] = [
            {
                **{key: value for key, value in probe.items()
                   if key not in {"workspace_before", "workspace_after", "runtime_stdout", "executions"}},
                "executions": [
                    {key: value for key, value in item.items()
                     if key not in {"workspace_before", "workspace_after", "scratch_hashes"}}
                    for item in probe.get("executions", [])
                ],
            } for probe in feedback["environment_probes"]
        ]
    return result


class ReconstructionRuntime:
    """同一研究者持续构建任务包，阶段仅切换可用工具和输出契约。"""

    backend = "hermes-sandbox"

    def __init__(self, agent: AgentRuntime, *, source: dict[str, Any], task: dict[str, Any],
                 runtime_factory: Callable[[], ContainerRuntime],
                 python_runtime: Path | None = None,
                 initial_feedback: dict[str, Any] | None = None) -> None:
        self.agent = SandboxedAgentRuntime(agent, runtime_factory)
        self.model_name = agent.model_name
        self.source, self.task = source, task
        self.runtime_factory, self.python_runtime = runtime_factory, python_runtime
        self.initial_feedback = initial_feedback
        self.conversation = AgentConversation()
        self.phases: list[dict[str, Any]] = []
        self.public_sources: SearchTools | None = None

    def _check_candidate(self, **kwargs: Any) -> dict[str, Any]:
        result = check_candidate(runtime_factory=self.runtime_factory,
                                 python_runtime=self.python_runtime, **kwargs)
        root = Path(result["record_path"]).parent
        receipt = root / "python-runtime-receipt.json"
        if receipt.is_file() and json.loads(receipt.read_text()).get("status") == "READY":
            self.python_runtime = root / "python_runtime"
            kwargs["session"].dependency_bundle = self.python_runtime
        return result

    def run(self, *, role: AgentRole, instruction: str, session: AgentSession,
            output_root: Path) -> AgentResult:
        role = replace(role, tools=tuple(dict.fromkeys((*role.tools, "read_probe_output"))))
        prior_probes = (session.repair_feedback or self.initial_feedback or {}).get("environment_probes", [])
        # 旧探针仅供续读，不计入当前候选的已执行检查。
        session.probe_output_history = [
            item for item in prior_probes if isinstance(item, dict) and item.get("probe_id")
        ]
        author = role.name != "sufficiency" and role.result_schema != "traceforge.verifier-semantic-review.v1"
        if author:
            session.conversation = self.conversation
            if "raw_session" in self.source:
                session.session_context = json.dumps(self.source["raw_session"], ensure_ascii=False)
            role = replace(role, identity=(
                "你是同一个任务重建研究者，持续负责从原始会话恢复完整任务包：任务初态、"
                "依赖、原任务说明、隐藏验证和参考解。阶段切换只改变工具权限与输出格式，"
                "此前读到的证据、实际错误和已作修改仍然有效。以原用户目标为边界，"
                "不改题、不预解初态、不用空函数或缩小验证范围掩盖环境缺口。"
                "构建声明和自检都不是独立验收；最终由真实执行证明。"
            ))
        elif role.name == "sufficiency":
            session.conversation = AgentConversation()
            session.session_context = json.dumps(self.source.get("raw_session"), ensure_ascii=False)
            role = replace(role, tools=(*role.tools, "read_session_message", "read_session_context"))
            instruction += (
                "\n必须按 TASK.task_start_message_index 核对原任务起点，而非整个 session 最早状态。"
                "使用 read_session_message 读取原用户消息及 task_time_context 标出的先前修改。"
                "先前任务已完成的模块或集成是本任务的历史上下文；不得因候选改回旧调用链就判定它们无关。"
                "当前任务开始之后的解决方案不能预置进初态。原始 session 只读保留，按索引核对。"
                "核对探针代码实际调用的实现：替换 sys.modules 或用假类绕过本地源码，不能证明真实模块可加载。"
                "外部网络可以隔离，但任务所需本地入口和依赖必须真实执行；不要把替身的 PASS 用来豁免损坏源码。"
            )
        if (role.name in {"sufficiency", "verifier"} and self.python_runtime is not None
                and session.workspace is not None):
            reuse_python_runtime(self.python_runtime, session.workspace,
                                 session.workspace.parent / "python_runtime")
        if role.name == "completion":
            session.dependency_bundle = self.python_runtime
            if self.public_sources is None:
                self.public_sources = SearchTools(output_root / "public-sources")
            session.web_search_handler = self.public_sources.search
            session.web_open_handler = self.public_sources.open
            session.candidate_check_handler = partial(
                self._check_candidate, session=session, output_root=output_root / "checks",
            )
            index = []
            for event in session.evidence:
                parsed = event.get("session_parse") or {}
                observations = [*parsed.get("file_ops", []),
                                *parsed.get("reference_file_ops", [])]
                index.append({
                    "id": event["evidence_ref_id"], "interpretation": parsed.get("reason"),
                    "initial_state_eligible": event.get("initial_state_eligible", True),
                    "assistant_message_index": event.get("assistant_message_index"),
                    "tool_message_index": event.get("tool_message_index"),
                    "written_paths": [op.get("path") for op in observations
                                      if op["kind"] == "write"],
                    "historical_only_paths": parsed.get("historical_only_paths", []),
                    "observations": [{
                        **{key: op.get(key) for key in (
                            "path", "source_path", "partial", "content_ref",
                            "initial_state_blockers")},
                        "observed_lines": {
                            "first": min(op["line_numbers"]), "last": max(op["line_numbers"]),
                            "count": len(op["line_numbers"]),
                        } if op.get("line_numbers") else None,
                    } for op in observations if op["kind"] == "read"],
                })
            source_line = "SOURCE_SESSION=" + json.dumps(
                indexed_session(self.source.get("raw_session", {})), ensure_ascii=False,
            )
            source_attached = bool(source_session_message_indices(
                self.conversation.messages, self.source.get("raw_session", {}),
            ))
            values = {
                "__SOURCE_SESSION__": (
                    "完整原始 session 已在当前作者会话中；继续利用此前全部原文。"
                    if source_attached else source_line
                ),
                "__TASK__": json.dumps(workspace_task_context(self.task), ensure_ascii=False),
                "__EVIDENCE_INDEX__": json.dumps(index, ensure_ascii=False),
                "__SYSTEM_CONTEXT__": json.dumps(
                    (self.source.get("session_parser") or {}).get("system_context", []), ensure_ascii=False),
                "__CURRENT_REPAIRS__": json.dumps({
                    path: [item["reason"] for item in repairs]
                    for path, repairs in session.prior_capture_repairs.items()
                }, ensure_ascii=False),
                "__FEEDBACK__": json.dumps(
                    author_feedback(session.repair_feedback if session.repair_feedback is not None
                                    else self.initial_feedback), ensure_ascii=False),
                "__RESULT_CONTRACT__": COMPLETION_RESULT_CONTRACT,
            }
            template = Path(__file__).with_name("researcher_instruction.md").read_text()
            instruction = re.sub(r"__[A-Z_]+__", lambda match: values[match[0]], template)
            rendered = output_root / "task"
            rendered.mkdir(parents=True, exist_ok=True)
            (rendered / "instruction.md").write_text(instruction)
            role = replace(role, tools=(*role.tools, "restore_observed_file", "edit_candidate_file",
                                        "run_candidate", "restore_dependency_source", "web_search", "web_open"))
        if author:
            instruction = f"同一研究者继续；当前阶段：{role.name}。\n" + instruction
        baseline = None
        probe = (self.initial_feedback or {}).get("primary_behavior_probe")
        baseline_check = partial(
            self._check_candidate, session=session, output_root=output_root / "checks",
            python_code=probe["code"], purpose=probe["purpose"],
            timeout_seconds=probe["timeout_seconds"],
        ) if probe is not None else None
        if role.name == "sufficiency" and baseline_check is not None:
            baseline = baseline_check()
            instruction += "\n原文绑定的必要初态行为检查（失败必须交回作者修复）：\n" + json.dumps(
                baseline, ensure_ascii=False,
            )
        failed_candidates: set[str] = set()
        missing_probe_sets: set[frozenset[str]] = set()
        turns = []
        attempt = 0
        while True:
            attempt += 1
            attempt_root = output_root / "attempts" / f"{attempt:03d}" if role.name in {"completion", "sufficiency"} else output_root
            before = len(session.conversation.messages) if session.conversation is not None else 0
            probe_count = len(session.environment_probes)
            result = self.agent.run(role=role, instruction=instruction, session=session,
                                    output_root=attempt_root)
            if role.name == "completion":
                result = completion_metadata(result, session)
            if role.name == "completion" and baseline_check is not None and result.completed and not result.errors:
                baseline = baseline_check()
            turns.extend(result.turns)
            self.phases.append({"phase": role.name, "schema": role.result_schema,
                                "authoring_session": author, "history_messages_before": before,
                                "history_messages_after": len(session.conversation.messages) if session.conversation is not None else 0,
                                "completed": result.completed, "output_root": str(attempt_root)})
            if (role.name == "sufficiency" and result.completed and not result.errors
                    and result.payload.get("label") == "SUFFICIENT"
                    and not result.payload.get("missing_context")
                    and (baseline is None or baseline.get("status") == "PASS")):
                missing = frozenset({"load", "dependency", "reset"} - {
                    item.get("purpose") for item in session.environment_probes
                    if item.get("status") == "PASS"
                })
                if missing:
                    if missing in missing_probe_sets:
                        result = replace(result, errors=[*result.errors, "REVIEWER_NO_PROGRESS"])
                        break
                    missing_probe_sets.add(missing)
                    instruction = (
                        "同一独立环境检查继续。你已判断源码上下文足够，但这些必要检查尚无成功回执："
                        + ", ".join(sorted(missing))
                        + "。这是当前检查步骤未完成，不是要求作者修改源码。保持只读，使用工具补齐这些检查，"
                        "若此前检查了被你判定无关的文件，应纠正检查范围，实际调用任务需要的原有入口。"
                        "源码 load 检查输出 module.__file__ 和实际正文 SHA256，并断言候选预期路径；"
                        "安装库成功不能证明同名原路径完整，不能用它豁免任务相关片段缺口。"
                        "依据真实结果返回完整 JSON；environment_checks 仅列 load/reset/dependency，引用有效 probe_id。"
                        "若发现真实源码缺口则明确返回 INSUFFICIENT；不得只再次声明未检查。"
                    )
                    continue
            candidates = result.payload.get("candidates", [])
            if (role.name != "completion" or not result.completed or result.errors
                    or result.payload.get("open_questions") or not isinstance(candidates, list)
                    or len(candidates) != 1 or not isinstance(candidates[0], dict)
                    or candidates[0].get("decision") not in {"READY", "REVIEW"}):
                break
            recent = session.environment_probes[probe_count:]
            repair_checked = session.repair_feedback is None or (
                recent and recent[-1].get("status") == "PASS"
            )
            if (candidates[0]["decision"] == "READY" and repair_checked
                    and (baseline is None or baseline.get("status") == "PASS")):
                break
            if session.repair_feedback is not None and not recent and attempt == 1:
                instruction = (
                    "本轮返修尚未执行检查，上一轮 READY 已被独立执行结果否定。"
                    "当前候选与原始证据仍在；先核对具体缺口，恢复任务所需的已有能力，"
                    "再用 run_candidate 重跑失败能力。不能仅将同一错误改写成 uncertainties 后返回 READY。"
                    "若探针检查的是用户待实现目标，指出对应原文；若必要事实确实缺失，"
                    "返回 REVIEW 和具体 open_questions。交付格式：\n" + COMPLETION_RESULT_CONTRACT
                )
                continue
            failure = recent[-1] if recent else {}
            digest = failure.get("candidate_sha256")
            if not recent:
                if attempt > 1:
                    result = replace(result, errors=[*result.errors, "AUTHOR_NO_PROGRESS"])
                break
            if failure.get("status") != "FAIL" or not digest or not failure.get("cleanup_verified"):
                break
            if digest in failed_candidates:
                result = replace(result, errors=[*result.errors, "AUTHOR_NO_PROGRESS"])
                break
            failed_candidates.add(digest)
            # 续跑种子只合入受控工具的成功写入；原始捕获及其修复依据保持不变。
            session.replay_files.update({item["path"]: item["content"] for item in collect_workspace_writes(session)})
            instruction = (
                "同一研究者继续修复当前任务初态。上一轮已实际执行失败，尚未提出需要外部补充的问题，"
                "因此本阶段未结束。当前文件和作者历史均保留。读取相关原始观察，定位本次错误并"
                "作有依据的恢复，重新执行失败检查；不能缩小检查或预解用户目标。"
                "确实缺少不可推断的原始事实时，在 open_questions 写明缺失事实和已读证据；"
                "否则继续使用工具，修复后返回完整候选声明。最新真实执行结果：\n"
                + environment_probe_summary(failure) + "\n交付格式：\n" + COMPLETION_RESULT_CONTRACT
            )
        result = replace(result, turns=turns)
        if baseline is not None and baseline.get("status") != "PASS":
            result = replace(result, errors=[*result.errors, "INITIAL_BEHAVIOR_PROBE_FAILED"])
        if session.conversation is not None:
            history = output_root / "private/conversation.json"
            history.parent.mkdir(parents=True, exist_ok=True)
            history.write_text(json.dumps(session.conversation.messages, ensure_ascii=False) + "\n")
        (output_root / "researcher-state.json").write_text(json.dumps({
            "source_sha256": self.source.get("line_sha256"), "task_id": self.task.get("task_id"),
            "phases": self.phases,
        }, ensure_ascii=False, indent=2) + "\n")
        return result
