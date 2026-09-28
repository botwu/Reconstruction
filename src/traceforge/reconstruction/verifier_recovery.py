"""Hermes Verifier 角色：对着 Intent 义务生成隐藏测试。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents import VERIFIER_ROLE, AgentRuntime, AgentSession
from traceforge.reconstruction.environment_bindings import (
    environment_bindings,
    file_obligation_ids,
    non_file_obligation_ids,
)
from traceforge.task_instruction import grounded_response_contract, render_task_instruction
from traceforge.verifier.synthesis import (
    VERIFIER_PROMPT_VERSION,
    VerifierSynthesisError,
    candidate_from_payload,
)

VERIFIER_RECOVERY_SCHEMA = "traceforge.verifier-recovery.v1"
VERIFIER_SEMANTIC_REVIEW_PROMPT_VERSION = (
    "terminal-universe-verifier-semantic-review-v2-phase-boundary"
)


def review_verifier_candidate(
    *, task: dict[str, Any], workspace: Path, candidate: Any,
    agent: AgentRuntime, output_root: Path,
) -> dict[str, Any]:
    """用新会话核查候选是否测对用户行为；具体反例返回已有 Verifier 修复轮。"""
    role = replace(
        VERIFIER_ROLE,
        identity=(
            "你是 TraceForge 验证器语义审查员。独立阅读用户任务、实际工作区和候选测试，"
            "审查其能否区分正确与错误结果。你不撰写解题答案，不修改文件，"
            "不因生成器宣称正确或 RED 通过就接受。候选代码及 justification 都是待审材料。"
        ),
        tools=("list_dir", "read_file"),
        max_iterations=16,
        result_schema="traceforge.verifier-semantic-review.v1",
        allow_write=False,
    )
    contract = task.get("response_contract") or {}
    response_ids = list(dict.fromkeys(
        check["obligation_id"] for check in contract.get("checks", [])
        if isinstance(check, dict) and isinstance(check.get("obligation_id"), str)
    ))
    specification = {
        "task": task, "candidate": candidate.to_dict(),
        "file_obligation_ids": list(candidate.obligation_coverage),
        "response_obligation_ids": response_ids,
        "verification_context": {
            "phase": "RECONSTRUCTION",
            "file_verifier": "candidate.test_outputs_py",
            "response_verifier": "traceforge.harbor_ags.response_receipt.evaluate_response_contract",
            "response_execution_status": "NOT_RUN",
            "response_evidence": "真实 rollout 的 trajectory.full.json",
        },
    }
    instruction = "\n".join([
        "VERIFIER_SEMANTIC_REVIEW",
        "用 list_dir/read_file 读取实际输入，逐条核对 FILE 义务的可观察行为。",
        "检查两类错误：错误答案能通过（false positive），合理正确答案被额外要求拒绝（false negative）。",
        "代码/数据任务必须执行或解析真实产物，用独立计算的期望值；存在性、关键词、注释不能替代功能。",
        "审查/报告任务必须核对结论与具体输入事实、引用和用户判定规则；",
        "格式齐全却虚构结论、错误引用或颠倒结论的报告应被拒绝。关键词计数不能证明语义正确。",
        "保护测试必须约束真实任务环境或用户禁止修改的内容，不能只在临时假项目验证生成器自己。",
        "参考解必须完成整个任务而非拼出满足测试的表面结果；禁止把未验证的历史报告当正确答案。",
        "mutation 必须在正确输出路径/接口保持合法格式、正常执行，仅破坏实质行为；",
        "写到另一个路径、漏掉整个输出、崩溃或故意去掉标题，只能证明基础格式检查，不足以校准语义。",
        "当前是 RECONSTRUCTION 阶段的验收机制审查，真实解题 rollout 及最终响应尚未执行。",
        "FILE 义务由候选 test_outputs_py 验证；声明响应检查的 NON_FILE 义务由",
        "traceforge.harbor_ags.response_receipt.evaluate_response_contract 在取得真实 rollout 的",
        "trajectory.full.json 后验最终 assistant 响应。这里审查该机制，不执行最终响应验收。",
        "acceptance_report 检查响应结构、编号和字段类型；basic_summary 检查摘要及其与报告的一致性。",
        "不要求候选 pytest 检查最终 assistant 响应，也不因当前缺少 trajectory 或 acceptance-report 拒绝机制；",
        "不得要求把最终响应写入 workspace 或生成虚构响应来完成本阶段审查。",
        "对每条 response_obligation_id，必须回到对应义务及其引用的原始用户消息核对：",
        "response_contract 是否受原始要求支持，受支持的检查是否完整覆盖所要求的响应格式/一致性。",
        "仅要求格式/一致性且机制完整时可以 covered=true；这只表示重建时机制足够，",
        "不表示最终响应已产生、响应义务已通过或任何独立 rollout 入口已完成验收接线。",
        "如果义务要求事实正确、实际完成或外部操作，不能降成格式/一致性或哈希绑定；",
        "映射不完整、检查不受支持或不足以覆盖这些实质要求时 covered=false，给出具体反例。",
        "响应机制完整不能抵消 FILE 验证器的漏检或误拒绝；两类义务分别审查。",
        "输出 JSON：{decision: ACCEPT|REVISE, obligation_reviews: [{obligation_id, covered: bool, reason}],",
        "issues: [{obligation_id, problem, counterexample, repair}]}。每条 FILE 和声明响应检查的义务恰好一项。",
        "发现问题时给具体错误产物/行为反例及可执行修复建议，让生成器改测试和参考解；",
        "不要凭空扩大任务或要求恢复无关工程。无问题才 ACCEPT；这个判断本身不代替真实 RED 执行。",
        json.dumps(specification, ensure_ascii=False, sort_keys=True),
    ])
    session = AgentSession(workspace=workspace, allow_write=False)
    ran = agent.run(role=role, instruction=instruction, session=session, output_root=output_root)
    payload = dict(ran.payload) if isinstance(ran.payload, dict) else {}
    rows = payload.get("obligation_reviews")
    issues = payload.get("issues")
    expected_ids = set(candidate.obligation_coverage) | set(response_ids)
    valid_rows = (
        isinstance(rows, list) and len(rows) == len(expected_ids)
        and all(isinstance(row, dict) and isinstance(row.get("obligation_id"), str)
                and isinstance(row.get("covered"), bool)
                and isinstance(row.get("reason"), str) and row["reason"].strip() for row in rows)
        and {row.get("obligation_id") for row in rows} == expected_ids
    )
    errors = list(ran.errors)
    if not ran.completed:
        errors.append("VERIFIER_REVIEW_INCOMPLETE")
    if not valid_rows or not isinstance(issues, list) or payload.get("decision") not in {"ACCEPT", "REVISE"}:
        errors.append("VERIFIER_REVIEW_INVALID")
    accepted = not errors and payload.get("decision") == "ACCEPT" and not issues and all(
        row["covered"] for row in rows
    )
    if not accepted and not errors:
        errors.append("VERIFIER_SEMANTIC_REPAIR_REQUIRED")
    result = {
        "schema_version": role.result_schema, "status": "ACCEPT" if accepted else "REVISE",
        "prompt_version": VERIFIER_SEMANTIC_REVIEW_PROMPT_VERSION,
        "candidate_id": candidate.candidate_id,
        "test_sha256": hashlib.sha256(candidate.test_outputs_py.encode()).hexdigest(),
        "judgment_kind": "MODEL_SEMANTIC_REVIEW", "errors": errors,
        "obligation_reviews": rows, "issues": issues,
        "tool_events": session.tool_events,
        "agent": {"model": agent.model_name, "backend": ran.backend, "completed": ran.completed},
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "review.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def _workspace_has_files(root: Path) -> bool:
    return any(path.is_file() and not path.is_symlink() for path in root.rglob("*"))


def _pytest_red_ok(runs: list[dict[str, Any]], candidate: Any) -> bool:
    if candidate is None:
        return False
    expected_hash = hashlib.sha256(candidate.test_outputs_py.encode("utf-8")).hexdigest()
    by_name: dict[str, list[str]] = {}
    for row in runs:
        if not isinstance(row, dict):
            continue
        if row.get("test_sha256") != expected_hash:
            continue
        name = str(row.get("name", "")).split("[", 1)[0]
        if name:
            by_name.setdefault(name, []).append(str(row.get("status", "")))
    missing = [by_name.get(name, []) for name in candidate.missing_capability_tests]
    protective = [by_name.get(name, []) for name in candidate.protective_tests]
    if any(not statuses for statuses in missing + protective):
        return False
    if any(status not in {"PASS", "FAIL"} for statuses in missing + protective for status in statuses):
        return False
    if not any("FAIL" in statuses for statuses in missing):
        return False
    return all(all(status == "PASS" for status in statuses) for statuses in protective)


def run_verifier_recovery(
    *,
    task: dict[str, Any],
    workspace_root: str | Path,
    agent: AgentRuntime,
    output_root: str | Path,
    source: dict[str, Any] | None = None,
    feedback: dict[str, Any] | None = None,
    round_number: int = 1,
) -> tuple[dict[str, Any], Any]:
    obligations = task.get("acceptance_obligations")
    if not isinstance(obligations, list) or not obligations:
        raise VerifierSynthesisError("任务必须提供用户验收义务")
    ids = [item.get("id") for item in obligations if isinstance(item, dict)]
    file_ids = file_obligation_ids(task)
    unverified = non_file_obligation_ids(task)
    workspace = Path(workspace_root).resolve()
    all_non_file = bool(environment_bindings(task)) and not file_ids
    if (
        (
            source is not None
            and not _workspace_has_files(workspace)
            and source.get("selected_span_has_file_ops") is False
        )
        or (all_non_file and not _workspace_has_files(workspace))
        or all_non_file
    ):
        result = {
            "schema_version": VERIFIER_RECOVERY_SCHEMA,
            "prompt_version": VERIFIER_PROMPT_VERSION,
            "status": "REVIEW",
            "errors": ["NON_FILE_TASK"],
            "unverified_obligations": unverified or [str(item) for item in ids if item],
            "pytest_runs": [],
            "verifier": None,
            "agent": {"role": VERIFIER_ROLE.name, "backend": None, "turns": 0, "completed": False},
        }
        root = Path(output_root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "verifier.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return result, None
    instruction = "\n".join(
        [
            "Write hidden pytest for FILE acceptance obligations only.",
            "Inspect using list_dir/read_file; their paths are relative to the workspace.",
            "Use write_test({content: <full pytest file>}) and run_pytest({names: [<bare test_function_name>]}) in the sandbox.",
            "Tests must resolve the workspace from os.environ['TRACEFORGE_WORKSPACE']; never use host paths.",
            "Iterate tests from actual tool feedback until at least one missing-capability test FAILs and every protective test PASSes on the current completed workspace (bE).",
            "Do not write existence-only missing tests; asserting that a binding file exists is not a missing capability.",
            "Reference scripts must implement only the task obligations and preserve user prohibitions.",
            "Each oracle is an independent COMPLETE solution of ALL FILE obligations, not a component of a combined solution. The name is a label, not a destination filename.",
            "Return executable workspace-editing installers, not source files meant to be installed. Prefer Python standard-library Path.write_text with repr-escaped content. Do not import ROS/simulation dependencies or start target services merely to install code.",
            "The runner exports TRACEFORGE_WORKSPACE=/home/user/workspace. Both oracles and mutations must finish with exit code 0. A crash, ImportError, missing environment variable, permission or syntax error is not a valid semantic mutation.",
            "Test requested behavior, using isolated dependency stubs if necessary to exercise real workspace code. Comments, keyword presence and copied expected implementations cannot prove behavior. Never weaken assertions merely to make a reference pass.",
            "报告类任务：先阅读真实输入并确定可复核的事实与判定规则，再验证报告结论和引用与这些事实一致。格式齐全、关键词齐全但结论错误的报告必须失败；不要用从未读过的源码推断 APPROVED。",
            "保护性测试必须作用于实际任务输入或用户要求保持的行为；不要把 oracle/mutation 安装脚本复制进 pytest，再在假工作区自证正确。",
            "mutation 必须写入与合法参考解相同的目标文件/接口，保留合法输出格式但破坏一项核心语义；不得靠改输出路径、删除输出、遗漏标题或执行崩溃让 mutation 失败。",
            "Do not impose literal wording the task did not require. If the task names Critical/Important/Minor findings without prescribing exact headings, accept normalized labels such as Critical, Critical Findings, Important, Important Findings, Minor, or Minor Findings (including Markdown and case variants); never make the optional word Findings mandatory in generated tests.",
            "For strengths, residual risks, limitations, or equivalent review sections, test the required substance and accept clear semantic headings such as What was verified, Limitations, Forward-looking notes, Strengths, or Residual Risks; never require one exact English phrase unless the task explicitly requires it.",
            "For retries, repair the previous candidate from the concrete process/test feedback; keep valid tests and correct implementations unless evidence requires a change. Read every failure message, run the complete test list after edits, and do not repeat an unchanged script. Generated YAML/configuration must remain syntactically valid with correct indentation; write a quoted `$placeholder` without a backslash.",
            "Every Python reference or mutation script must be standalone syntactically valid; "
            "compile it mentally with ast.parse or python -m py_compile, and do not put raw newlines "
            "inside quoted string literals.",
            "INFRA_ERROR, TIMEOUT, invalid selectors and collection/usage errors are not RED evidence.",
            "Do not modify the workspace or apply a solution. Reference and mutation scripts are private output only.",
            "NON_FILE obligations must not appear in obligation_coverage. Do not invent a new output file or pytest for them.",
            "同时负责补全 NON_FILE 响应验收机制；Intent 中的 response_contract 只是初稿，可能漏项。",
            "返回完整 response_contract，保留有效检查并补齐遗漏义务。复用 traceforge.response-contract.v1：",
            "acceptance_report 检查使用 obligation_id、criterion_ids、required_fields；",
            "basic_summary 检查使用 obligation_id、verdicts、finding_levels、report_path、match_report:true。",
            "所有字段、枚举、路径与编号必须来自原始用户要求，只覆盖输出结构或摘要与报告的一致性。",
            "不得把事实正确、真实执行或外部操作伪装为格式检查；无法提供完整机制时返回 REVIEW 和具体问题。",
            "候选响应契约和 FILE 测试由同一轮独立语义审查；此阶段不生成或伪造解题响应。",
            "If no FILE obligation can be observed by file-based pytest, return status=REVIEW with open_questions.",
            "Finish with a JSON object only. status must be exactly READY or REVIEW.",
            "READY schema: {status: 'READY', test_outputs_py: <exact bytes last passed to write_test>, oracle_solutions: [{name, script, justification}, {name, script, justification}], mutation_solutions: [{name, script, justification}], missing_capability_tests: [<bare test name>], protective_tests: [<bare test name>], obligation_coverage: {<each FILE obligation id>: [<test name>]}, expected_value_strategy: <independent calculation explanation>, response_contract: <完整响应验收契约，纯FILE任务可省略>, open_questions: []}.",
            "Provide exactly two distinct valid reference scripts and exactly one meaningful incorrect implementation script, all starting from the initial workspace. Scripts execute in the workspace and may not access /tests or /solution.",
            "REVIEW schema: {status: 'REVIEW', open_questions: [<specific unresolved problem>]}.",
            "TASK:",
            json.dumps({**task, "task_instruction": render_task_instruction(task)}, ensure_ascii=False, sort_keys=True),
            "FILE_OBLIGATIONS:",
            json.dumps(file_ids, ensure_ascii=False),
            "NON_FILE_OBLIGATIONS:",
            json.dumps(unverified, ensure_ascii=False),
            "ENVIRONMENT_BINDINGS:",
            json.dumps(environment_bindings(task), ensure_ascii=False),
            "WORKSPACE_ROOT: /home/user/workspace (use relative paths in tools)",
            f"CALIBRATION_ROUND: {round_number}",
            "PREVIOUS_CALIBRATION_FEEDBACK:",
            json.dumps(feedback or {}, ensure_ascii=False, sort_keys=True),
        ]
    )
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    session = AgentSession(workspace=workspace, allow_write=False, allow_tests=True)
    ran = agent.run(
        role=VERIFIER_ROLE,
        instruction=instruction,
        session=session,
        output_root=root,
    )
    errors = list(ran.errors)
    audit_warnings: list[str] = []
    if not ran.completed:
        errors.append("AGENT_INCOMPLETE")
    payload = dict(ran.payload) if isinstance(ran.payload, dict) else {}
    # The bytes accepted by write_test are the executed source of truth. Hermes
    # often reserializes a multi-line test in its final JSON (whitespace or
    # quoting changes), so requiring the model to echo the whole file creates a
    # false protocol failure after a valid RED run. Preserve the declaration for
    # audit, but validate and build the candidate from the exact bytes that the
    # sandbox executed.
    declared_test_bytes = payload.get("test_outputs_py")
    if session.test_outputs_py:
        payload["test_outputs_py"] = session.test_outputs_py
    # 候选合同只进入本轮任务副本；通过原始要求约束和语义审查前不改 Intent。
    proposal = payload.get("response_contract", task.get("response_contract"))
    effective_task = {**task, "response_contract": proposal}
    response_contract = grounded_response_contract(effective_task)
    effective_task["response_contract"] = response_contract
    proposed_checks = proposal.get("checks") if isinstance(proposal, dict) else None
    final_checks = response_contract["checks"] if response_contract is not None else []
    if proposal is not None and (
        not isinstance(proposed_checks, list) or not proposed_checks
        or len(proposed_checks) != len(final_checks)
    ):
        errors.append("RESPONSE_CONTRACT_UNGROUNDED")
    response_ids = {check["obligation_id"] for check in final_checks}
    errors.extend(f"RESPONSE_VERIFIER_MISSING:{oid}" for oid in unverified if oid not in response_ids)
    digest = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    candidate = None
    extra: dict[str, Any] = {}
    try:
        coverage = payload.get("obligation_coverage")
        if isinstance(coverage, dict) and any(str(key) in set(unverified) for key in coverage):
            errors.append("NON_FILE_COVERAGE_FORBIDDEN")
        candidate, extra = candidate_from_payload(
            payload,
            obligation_ids=[str(item) for item in file_ids or ids if item],
            model_name=agent.model_name,
            prompt_sha256=digest,
            response_sha256=hashlib.sha256((ran.final_text or "").encode("utf-8")).hexdigest(),
            task=effective_task,
        )
    except VerifierSynthesisError as exc:
        errors.append(str(exc))
    # The pytest run is only evidence for the exact bytes that became the
    # candidate.  A previous test file/run must never satisfy this round.
    if session.test_outputs_py and isinstance(declared_test_bytes, str) and declared_test_bytes != session.test_outputs_py:
        # Keep a non-fatal audit marker. The candidate remains tied to the
        # executed bytes above, so a model cannot smuggle unexecuted tests.
        audit_warnings.append("AGENT_TEST_BYTES_NORMALIZED")
    # A deliberate REVIEW (for example a chat-only obligation with no
    # observable file effect) does not claim RED evidence. READY candidates
    # still need a sandbox RED pytest run. A synthesis error (for example
    # ORACLE_WRITES_INJECTOR) is already authoritative; do not stack
    # SANDBOX_PYTEST_RED_REQUIRED when pytest evidence exists.
    if session.sandbox is not None:
        if candidate is not None and not _pytest_red_ok(session.pytest_runs, candidate):
            errors.append("SANDBOX_PYTEST_RED_REQUIRED")
            candidate = None
        elif (
            candidate is None
            and payload.get("status") == "READY"
            and not session.pytest_runs
        ):
            errors.append("SANDBOX_PYTEST_RED_REQUIRED")
    semantic_review = None
    if candidate is not None and not errors:
        semantic_review = review_verifier_candidate(
            task={**effective_task, "task_instruction": render_task_instruction(effective_task)},
            workspace=workspace, candidate=candidate, agent=agent, output_root=root / "semantic-review",
        )
        errors.extend(semantic_review["errors"])
    blocking_errors = [item for item in errors if item != "AGENT_TEST_BYTES_NORMALIZED"]
    status = "READY" if candidate is not None and not blocking_errors else "REVIEW"
    if extra.get("status") == "REVIEW":
        status = "REVIEW"
        errors.extend(extra.get("open_questions") or [])
    # Do not return a usable candidate alongside errors.  Callers must not
    # accidentally calibrate a candidate whose local contract failed.
    if blocking_errors or status != "READY":
        candidate = None
    result = {
        "schema_version": VERIFIER_RECOVERY_SCHEMA,
        "prompt_version": VERIFIER_PROMPT_VERSION,
        "status": status,
        "errors": errors,
        "feedback": {"generation_errors": list(errors), "previous_candidate": payload} if errors else {},
        "semantic_review": semantic_review,
        "response_contract": response_contract if candidate is not None else None,
        "unverified_obligations": list(unverified),
        "warnings": audit_warnings,
        "pytest_runs": list(session.pytest_runs),
        "sandbox": session.sandbox is not None,
        "verifier": candidate.to_dict() if candidate is not None else None,
        "agent": {
            "role": VERIFIER_ROLE.name,
            "backend": ran.backend,
            "turns": len(ran.turns),
            "completed": ran.completed,
        },
    }
    if semantic_review is not None and semantic_review["status"] != "ACCEPT":
        result["feedback"]["semantic_review"] = semantic_review
    (root / "verifier.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result, candidate
