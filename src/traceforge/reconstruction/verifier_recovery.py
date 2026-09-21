"""Hermes Verifier 角色：对着 Intent 义务生成隐藏测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents import VERIFIER_ROLE, AgentRuntime, AgentSession
from traceforge.reconstruction.environment_bindings import (
    environment_bindings,
    file_obligation_ids,
    non_file_obligation_ids,
)
from traceforge.verifier.synthesis import (
    VERIFIER_PROMPT_VERSION,
    candidate_from_payload,
    VerifierSynthesisError,
)

VERIFIER_RECOVERY_SCHEMA = "traceforge.verifier-recovery.v1"


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
            "For retries, repair the previous candidate from the concrete process/test feedback; keep valid tests and correct implementations unless evidence requires a change. Read every failure message, run the complete test list after edits, and do not repeat an unchanged script. Generated YAML/configuration must remain syntactically valid with correct indentation; write a quoted `$placeholder` without a backslash.",
            "Every Python reference or mutation script must be standalone syntactically valid; "
            "compile it mentally with ast.parse or python -m py_compile, and do not put raw newlines "
            "inside quoted string literals.",
            "INFRA_ERROR, TIMEOUT, invalid selectors and collection/usage errors are not RED evidence.",
            "Do not modify the workspace or apply a solution. Reference and mutation scripts are private output only.",
            "NON_FILE obligations must not appear in obligation_coverage. Do not invent a new output file or pytest for them.",
            "If no FILE obligation can be observed by file-based pytest, return status=REVIEW with open_questions.",
            "Finish with a JSON object only. status must be exactly READY or REVIEW.",
            "READY schema: {status: 'READY', test_outputs_py: <exact bytes last passed to write_test>, oracle_solutions: [{name, script, justification}, {name, script, justification}], mutation_solutions: [{name, script, justification}], missing_capability_tests: [<bare test name>], protective_tests: [<bare test name>], obligation_coverage: {<each FILE obligation id>: [<test name>]}, expected_value_strategy: <independent calculation explanation>, open_questions: []}.",
            "Provide two distinct valid reference scripts and at least one meaningful incorrect implementation script, all starting from the initial workspace. Scripts execute in the workspace and may not access /tests or /solution.",
            "REVIEW schema: {status: 'REVIEW', open_questions: [<specific unresolved problem>]}.",
            "TASK:",
            json.dumps(task, ensure_ascii=False, sort_keys=True),
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
    (root / "verifier.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result, candidate
