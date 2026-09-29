"""按阶段验收 reconstruct raw-run 活跑产物，不打印密钥或文件正文。"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from traceforge.reconstruction.model_gateway import load_channel_connection, load_e2b_api_key

ALLOWED_COMPLETION_PROVENANCE = {
    "REPLAYED",
    "MODEL_COMPLETED",
    "SYNTHETIC_STUB",
    "NEIGHBOR",
    "PRIOR_VISIBLE_OBSERVATION",
    "TRAJECTORY_FIRST_OBSERVATION",
}
USER_ID = re.compile(r"^user:\d+$")
INTENT_CODES = {
    "INVALID_JSON",
    "AGENT_INCOMPLETE",
    "TASK_INSTRUCTION_REQUIRED",
    "TASK_NOT_EXECUTABLE",
    "INTENT_REVIEW",
}
COMPLETION_CODES = {
    "CONTAINER_REQUIRED",
    "EMPTY_REPLAY_TREE",
    "NO_HOLES",
    "EMPTY_TREE_INVENTION",
    "COMPLETION_REVIEW",
    "COMPLETION_PAYLOAD_NOT_OBJECT",
    "CANDIDATES_NOT_ARRAY",
    "CANDIDATE_LIMIT_EXCEEDED",
    "CANDIDATE_NOT_OBJECT",
}
INFRA_HINTS = (
    "SANDBOX_INIT",
    "TIMEOUT",
    "TIMED OUT",
    "APICONNECTION",
    "CONNECTERROR",
    "CONNECTION ERROR",
    "NETWORK",
    "HTTP_5",
    "TOKENHUB",
    "MODEL_CONNECTION_ERROR",
    "MODEL_TIMEOUT",
    "MODEL_API_FAILED",
)


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _secret_values(config: Path | None, channel: str) -> tuple[str, ...]:
    values: set[str] = set()
    for name in (
        "ANTHROPIC_API_KEY",
        "TOKENHUB_KEY",
        "ROLLOUT_LLM_API_KEY",
        "AGS_API_KEY",
        "E2B_API_KEY",
        "ROLLOUT_E2B_API_KEY",
    ):
        raw = os.environ.get(name, "").strip()
        if len(raw) >= 8:
            values.add(raw)
    if config is not None and config.is_file():
        try:
            sandbox_key = load_e2b_api_key(config)
        except Exception:
            sandbox_key = None
        if sandbox_key and len(sandbox_key) >= 8:
            values.add(sandbox_key)
        try:
            _url, channel_key = load_channel_connection(config, channel)
        except Exception:
            channel_key = None
        if channel_key and len(channel_key) >= 8:
            values.add(channel_key)
    return tuple(values)


def _scan_secrets(root: Path, secrets: tuple[str, ...]) -> list[str]:
    leaks: list[str] = []
    skip_suffixes = {".whl", ".png", ".jpg", ".pyc", ".so"}
    if not secrets:
        return leaks
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() in skip_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for secret in secrets:
            if secret in text:
                leaks.append(str(path.relative_to(root)))
                break
    return leaks


def _kind_for_errors(errors: list[str]) -> str:
    joined = " ".join(errors).upper()
    if any(hint in joined for hint in INFRA_HINTS):
        return "infra"
    return "logic"


def _stage(name: str, *, ok: bool, kind: str, detail: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"stage": name, "ok": ok, "kind": kind, "detail": detail}
    if extra:
        row.update(extra)
    return row


def _task_row(manifest: dict[str, Any]) -> dict[str, Any]:
    tasks = manifest.get("tasks")
    if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict):
        return tasks[0]
    return {}


def _check_source(root: Path, task_id: str, task: dict[str, Any]) -> dict[str, Any]:
    replay_path = root / "tasks" / task_id / "replay.json"
    workspace = root / "tasks" / task_id / "initial_workspace"
    replay = _load(replay_path)
    files = [item.get("path") for item in replay.get("files") or [] if isinstance(item, dict)]
    support = task.get("execution_support_route") or {}
    errors: list[str] = []
    if not replay_path.is_file():
        errors.append("REPLAY_MISSING")
    if not workspace.is_dir():
        errors.append("WORKSPACE_MISSING")
    if support.get("route") != "TERMINAL_FILE":
        errors.append(f"ROUTE_{support.get('route') or 'MISSING'}")
    # Terminal tasks may expose a single source file; requiring the old five-file
    # fixture made valid terminal sessions fail before reconstruction was checked.
    if not files:
        errors.append("REPLAY_FILE_COUNT_0")
    return _stage(
        "source",
        ok=not errors,
        kind="logic" if errors else "ok",
        detail=";" .join(errors) or "replay and TERMINAL_FILE ok",
        extra={
            "task_id": task_id,
            "replay_file_count": len(files),
            "replay_paths": files,
            "route": support.get("route"),
            "workspace": workspace.is_dir(),
        },
    )


def _check_intent(root: Path, task_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    intent = manifest.get("intent") if isinstance(manifest.get("intent"), dict) else {}
    task_intent = {}
    for item in intent.get("tasks") or []:
        if isinstance(item, dict) and str((item.get("task") or {}).get("task_id") or item.get("task_id")) == task_id:
            task_intent = item
            break
    if not task_intent and isinstance(intent.get("task"), dict):
        task_intent = intent
    recovered = task_intent.get("task") if isinstance(task_intent.get("task"), dict) else {}
    agent = task_intent.get("agent") if isinstance(task_intent.get("agent"), dict) else {}
    errors = [str(x) for x in (task_intent.get("errors") or intent.get("errors") or [])]
    instruction = str(recovered.get("task_instruction") or "")
    obligations = recovered.get("acceptance_obligations") or []
    refs: list[str] = []
    for item in obligations:
        if isinstance(item, dict):
            refs.extend(str(x) for x in (item.get("evidence_ref_ids") or []))
    ags_during_intent = (root / "ags_environment").is_dir() and not (
        root / "tasks" / task_id / "completion" / "completion.json"
    ).is_file()
    problems: list[str] = []
    kind = "ok"
    exchange = _load(root / "intent" / "tasks" / task_id / "private" / "model_exchange.json")
    response_text = str(((exchange.get("response") or {}) if isinstance(exchange.get("response"), dict) else {}).get("text") or "")
    if intent.get("status") != "READY" or task_intent.get("status") != "READY":
        problems.append("INTENT_NOT_READY")
        kind = _kind_for_errors([*errors, response_text])
    if not instruction.strip():
        problems.append("EMPTY_INSTRUCTION")
        kind = "logic" if kind == "ok" else kind
    if intent.get("status") == "READY" or task_intent.get("status") == "READY":
        if errors:
            problems.append("INTENT_ERRORS_ON_READY")
            kind = "logic"
        if agent.get("completed") is not True:
            problems.append("INTENT_AGENT_INCOMPLETE")
            kind = "logic"
    if any(not USER_ID.match(ref) for ref in refs):
        problems.append("EVIDENCE_ID_NOT_USER")
        kind = "logic"
    backend = str(agent.get("backend") or "hermes")
    if backend != "hermes":
        problems.append(f"BACKEND_{backend}")
        kind = "logic"
    if any(str(item).startswith("SANDBOX_INIT") for item in errors):
        problems.append("SANDBOX_INIT_ON_INTENT")
        kind = "logic"
    if ags_during_intent:
        problems.append("AGS_DIR_BEFORE_COMPLETION")
        kind = "logic"
    return _stage(
        "intent",
        ok=not problems,
        kind=kind if problems else "ok",
        detail=";" .join(problems + errors) or "intent READY",
        extra={
            "status": intent.get("status"),
            "backend": agent.get("backend"),
            "obligation_refs": refs,
            "errors": errors,
        },
    )


def _candidate_provenance(candidate: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in candidate.get("files") or []:
        if isinstance(item, dict) and item.get("provenance"):
            values.append(str(item["provenance"]))
    for item in candidate.get("file_provenance") or []:
        if isinstance(item, dict) and item.get("provenance"):
            values.append(str(item["provenance"]))
    return values


def _check_completion(root: Path, task_id: str, task: dict[str, Any]) -> dict[str, Any]:
    path = root / "tasks" / task_id / "completion" / "completion.json"
    completion = _load(path) or (task.get("completion") if isinstance(task.get("completion"), dict) else {})
    # Once a completion artifact exists, its errors are authoritative. Falling
    # back to task-level errors here mixed an earlier intent failure into a later
    # completion result and made the report claim a stage failed for the wrong
    # reason.
    if path.is_file() or isinstance(task.get("completion"), dict):
        completion_errors = completion.get("errors")
        errors = [str(x) for x in (completion_errors if isinstance(completion_errors, list) else [])]
    else:
        errors = [str(x) for x in (task.get("errors") or [])]
    agent = completion.get("agent") if isinstance(completion.get("agent"), dict) else {}
    problems: list[str] = []
    kind = "ok"
    if not path.is_file() and not completion:
        return _stage(
            "completion",
            ok=False,
            kind="logic",
            detail="COMPLETION_NOT_REACHED",
            extra={"errors": [str(x) for x in (task.get("errors") or [])]},
        )
    if agent.get("backend") != "hermes-sandbox":
        problems.append(f"BACKEND_{agent.get('backend') or 'MISSING'}")
        kind = "logic"
    if any(item == "CONTAINER_REQUIRED" for item in errors):
        problems.append("CONTAINER_REQUIRED")
        kind = "logic"
    if completion.get("status") == "READY":
        if errors:
            problems.append("COMPLETION_ERRORS_ON_READY")
            kind = "logic"
        if agent.get("completed") is not True:
            problems.append("COMPLETION_AGENT_INCOMPLETE")
            kind = "logic"
        if agent.get("skipped") is not False:
            problems.append("COMPLETION_SKIPPED_ON_READY")
            kind = "logic"
    if any(str(item).startswith("SANDBOX_INIT") for item in errors):
        problems.append("SANDBOX_INIT")
        kind = "infra"
    provenances: list[str] = []
    for candidate in completion.get("candidates") or []:
        if isinstance(candidate, dict):
            provenances.extend(_candidate_provenance(candidate))
    bad_prov = sorted({item for item in provenances if item not in ALLOWED_COMPLETION_PROVENANCE})
    if bad_prov:
        problems.append("BAD_PROVENANCE")
        kind = "logic"
    ran = path.is_file() and completion.get("status") == "READY" and not errors and "SANDBOX_INIT" not in problems and "CONTAINER_REQUIRED" not in problems
    if completion.get("status") != "READY" and ran:
        if kind == "ok":
            kind = "content"
        if "COMPLETION_NOT_READY" not in problems:
            problems.append("COMPLETION_NOT_READY")
    return _stage(
        "completion",
        ok=ran and not errors and "SANDBOX_INIT" not in problems and "CONTAINER_REQUIRED" not in problems,
        kind=kind if problems else "ok",
        detail=";" .join(problems + [item for item in errors if item not in problems]) or "completion ran in sandbox",
        extra={
            "status": completion.get("status"),
            "backend": agent.get("backend"),
            "skipped": agent.get("skipped"),
            "errors": errors,
            "candidate_count": len(completion.get("candidates") or []),
            "bad_provenance": bad_prov,
            "pipeline_ran": ran,
        },
    )


def _check_sufficiency(
    root: Path,
    task_id: str,
    task: dict[str, Any],
    completion_ok: bool,
    completion_ready: bool,
) -> dict[str, Any]:
    sufficiency_root = root / "tasks" / task_id / "sufficiency"
    selected_index = task.get("selected_index")
    selected_path = (
        sufficiency_root / f"{int(selected_index):03d}" / "sufficiency.json"
        if isinstance(selected_index, int) and selected_index >= 0
        else None
    )
    all_paths = sorted(sufficiency_root.glob("*/sufficiency.json"))
    # A REVIEW manifest may have judge artifacts but no selected candidate.
    # Inspect those artifacts so the report says INSUFFICIENT/REVIEW instead
    # of incorrectly claiming that Sufficiency was never produced.
    paths = (
        [selected_path]
        if selected_path is not None and selected_path.is_file()
        else all_paths
    )
    present = bool(paths)
    if not completion_ready:
        return _stage(
            "sufficiency",
            ok=True,
            kind="ok",
            detail="skipped; completion not READY",
            extra={"present": present, "status": "NOT_RUN"},
        )
    if not present:
        return _stage(
            "sufficiency",
            ok=False,
            kind="logic" if completion_ok else "infra",
            detail="SUFFICIENCY_MISSING_AFTER_COMPLETION_READY",
            extra={"status": "MISSING"},
        )
    judges = []
    for path in sorted(sufficiency_root.glob("*/sufficiency.json")):
        payload = _load(path)
        judge_errors = payload.get("errors")
        judges.append(
            {
                "status": payload.get("status"),
                "label": payload.get("label"),
                "decision": payload.get("decision"),
                "errors": judge_errors if isinstance(judge_errors, list) else [],
            }
        )
        if any(str(item).startswith("SANDBOX_INIT") for item in (judge_errors or [])):
            return _stage(
                "sufficiency",
                ok=False,
                kind="infra",
                detail="SANDBOX_INIT",
                extra={"judges": judges, "status": "REVIEW"},
            )
        if (
            payload.get("status") != "READY"
            or payload.get("label") != "SUFFICIENT"
            or payload.get("decision") != "READY"
            or judge_errors != []
        ):
            return _stage(
                "sufficiency",
                ok=False,
                kind="logic",
                detail="SUFFICIENCY_NOT_READY",
                extra={"judges": judges, "status": payload.get("status")},
            )
    return _stage(
        "sufficiency",
        ok=True,
        kind="ok",
        detail="sufficiency ran",
        extra={
            "judges": judges,
            "audit": (task.get("sufficiency_audit") or {}).get("status"),
            "status": "READY",
        },
    )


def _check_verifier(root: Path, task_id: str, task: dict[str, Any], intent_refs: list[str]) -> dict[str, Any]:
    path = root / "tasks" / task_id / "verification" / "verification.json"
    verification = _load(path) or (task.get("verification") if isinstance(task.get("verification"), dict) else {})
    selected = task.get("selected_index")
    if selected is None and not path.is_file():
        return _stage(
            "verifier",
            ok=True,
            kind="ok",
            detail="skipped; no sufficient candidate",
            extra={"stopped_at": task.get("stopped_at")},
        )
    if not path.is_file() and not verification:
        return _stage(
            "verifier",
            ok=False,
            kind="logic",
            detail="VERIFICATION_MISSING_AFTER_SELECTION",
        )
    errors = [str(x) for x in (verification.get("errors") or [])]
    if any(item.startswith("SANDBOX_INIT") for item in errors):
        return _stage("verifier", ok=False, kind="infra", detail="SANDBOX_INIT", extra={"errors": errors})
    if verification.get("status") == "NOT_APPLICABLE":
        return _stage(
            "verifier",
            ok=True,
            kind="content",
            detail="NOT_APPLICABLE",
            extra={"errors": errors, "unverified": verification.get("unverified_obligations")},
        )
    if "NON_FILE_TASK" in errors:
        return _stage(
            "verifier",
            ok=False,
            kind="logic",
            detail="NON_FILE_TASK_ON_TERMINAL_FILE",
            extra={"errors": errors},
        )
    return _stage(
        "verifier",
        ok=True,
        kind="content" if verification.get("status") not in {"READY", "PENDING_EXECUTION"} else "ok",
        detail=str(verification.get("status") or "ran"),
        extra={
            "status": verification.get("status"),
            "bundle": bool(verification.get("bundle")),
            "errors": errors,
            "intent_refs": intent_refs,
        },
    )




def _red_evidence(verification: dict[str, Any]) -> tuple[bool, str]:
    """校验真实 RED calibration attempt，而不是相信单独的 status 字段。"""

    calibration_runs = verification.get("calibration_runs")
    if not isinstance(calibration_runs, list):
        return False, "RED_EVIDENCE_MISSING"

    def valid_run(
        run: Any,
        *,
        expected_status: str,
        expected_reward: float,
        test_hash: str,
    ) -> bool:
        if not isinstance(run, dict) or run.get("verifier_test_sha256") != test_hash:
            return False
        results = run.get("results")
        if not isinstance(results, dict):
            return False
        if results.get("schema_version") != "traceforge.harbor-ags-rollout-results.v1":
            return False
        quality = results.get("quality_gate")
        if not isinstance(quality, dict) or quality.get("ok") is not True:
            return False
        trials = results.get("trials")
        if not isinstance(trials, list) or not trials:
            return False
        return all(
            isinstance(trial, dict)
            and trial.get("status") == expected_status
            and trial.get("reward") == expected_reward
            for trial in trials
        )

    for attempt in calibration_runs:
        if not isinstance(attempt, dict) or attempt.get("status") != "PASS":
            continue
        initial = attempt.get("initial_red")
        if (
            not isinstance(initial, dict)
            or initial.get("passed") is not True
            or initial.get("errors") != []
            or not isinstance(initial.get("verdict_count"), int)
            or initial.get("verdict_count") <= 0
            or attempt.get("failed_cases") != []
            or not isinstance(attempt.get("verifier_test_sha256"), str)
            or not attempt.get("verifier_test_sha256")
        ):
            continue
        test_hash = attempt["verifier_test_sha256"]
        runs = attempt.get("runs")
        if not isinstance(runs, dict):
            continue
        nop = [value for key, value in runs.items() if str(key).endswith("-nop")]
        oracle = [value for key, value in runs.items() if "-oracle-" in str(key)]
        mutation = [value for key, value in runs.items() if "-mutation-" in str(key)]
        if (
            len(nop) >= 1
            and len(oracle) >= 1
            and len(mutation) >= 1
            and all(valid_run(value, expected_status="FAIL", expected_reward=0.0, test_hash=test_hash) for value in nop)
            and all(valid_run(value, expected_status="PASS", expected_reward=1.0, test_hash=test_hash) for value in oracle)
            and all(valid_run(value, expected_status="FAIL", expected_reward=0.0, test_hash=test_hash) for value in mutation)
        ):
            return True, "calibration_runs"
    return False, "RED_EVIDENCE_MISSING"


def _check_red(manifest: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    verification = task.get("verification") if isinstance(task.get("verification"), dict) else {}
    executed, evidence_source = _red_evidence(verification)
    if manifest.get("status") == "READY":
        if not executed:
            return _stage(
                "red",
                ok=False,
                kind="logic",
                detail="RED_EVIDENCE_MISSING",
                extra={"status": verification.get("status"), "evidence_source": evidence_source},
            )
        return _stage(
            "red",
            ok=True,
            kind="ok",
            detail="READY after RED",
            extra={"status": "READY", "evidence_source": evidence_source},
        )
    if not executed:
        return _stage(
            "red",
            ok=True,
            kind="ok",
            detail="RED not executed; READY forbidden",
            extra={"manifest_status": manifest.get("status"), "evidence_source": evidence_source},
        )
    return _stage(
        "red",
        ok=verification.get("status") == "READY",
        kind="content" if verification.get("status") != "READY" else "ok",
        detail=str(verification.get("status") or "RED_REVIEW"),
        extra={"status": verification.get("status"), "evidence_source": evidence_source},
    )


def _mixed_errors(task: dict[str, Any], intent_errors: list[str], completion_errors: list[str]) -> dict[str, Any]:
    combined = [str(x) for x in (task.get("errors") or [])]
    has_intent = any(item in INTENT_CODES for item in combined)
    has_completion = any(item in COMPLETION_CODES or item.startswith("SANDBOX_INIT") for item in combined)
    mixed = has_intent and has_completion
    return _stage(
        "error_hygiene",
        ok=not mixed,
        kind="logic" if mixed else "ok",
        detail="MIXED_INTENT_COMPLETION_ERRORS" if mixed else "errors isolated",
        extra={"task_errors": combined, "intent_errors": intent_errors, "completion_errors": completion_errors},
    )



def _check_rollout(root: Path, task: dict[str, Any] | None = None) -> dict[str, Any]:
    """校验 Harbor rollout 聚合结果，支持 task verification 内嵌或 root 文件。"""

    candidates: list[tuple[str, dict[str, Any]]] = []
    if isinstance(task, dict):
        verification = task.get("verification")
        nested = verification.get("rollout") if isinstance(verification, dict) else None
        payload = nested.get("results") if isinstance(nested, dict) else None
        if isinstance(payload, dict):
            candidates.append(("task_verification", payload))
        elif isinstance(nested, dict) and isinstance(nested.get("trials"), list):
            candidates.append(("task_verification", nested))
    paths = [
        root / "ags_trial" / "results.json",
        root / "rollout" / "results.json",
        root / "rollout-results.json",
        root / "ags_trial" / "rollout-results.json",
    ]
    paths.extend(
        path
        for path in sorted(root.rglob("results.json"))
        if path not in paths and "_control" not in path.parts
    )
    for path in paths:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("trials"), list):
            candidates.append((str(path), payload))
    for source, payload in candidates:
        if payload.get("schema_version") != "traceforge.harbor-ags-rollout-results.v1":
            continue
        trials = payload.get("trials")
        quality = payload.get("quality_gate")
        if not isinstance(trials, list) or not isinstance(quality, dict):
            continue
        if quality.get("ok") is not True:
            continue
        trial_count = payload.get("trial_count")
        if isinstance(trial_count, int) and trial_count != len(trials):
            continue
        cleanup = payload.get("cleanup")
        if isinstance(cleanup, dict) and cleanup.get("ok") is not True:
            continue
        successes = [
            item
            for item in trials
            if isinstance(item, dict)
            and item.get("status") == "PASS"
            and item.get("reward") == 1.0
        ]
        if len(successes) < 2:
            continue
        return _stage(
            "rollout",
            ok=True,
            kind="ok",
            detail="rollout quality evidence ok",
            extra={
                "source": source,
                "trial_count": len(trials),
                "success_count": len(successes),
                "quality_gate": True,
            },
        )
    return _stage(
        "rollout",
        ok=False,
        kind="logic",
        detail="ROLLOUT_RESULTS_MISSING_OR_INVALID",
        extra={"success_count": 0, "quality_gate": False},
    )


def grade(root: Path, *, config: Path | None, channel: str, expected_task_id: str | None) -> dict[str, Any]:
    manifest = _load(root / "reconstruction_manifest.json")
    task = _task_row(manifest)
    task_id = str(task.get("task_id") or expected_task_id or "")
    stages: list[dict[str, Any]] = []
    if not manifest:
        report = {
            "root": str(root),
            "pipeline_ok": False,
            "delivery_ready": False,
            "end_to_end_ready": False,
            "stages": [_stage("manifest", ok=False, kind="logic", detail="MANIFEST_MISSING")],
        }
        return report
    source = _check_source(root, task_id, task)
    intent = _check_intent(root, task_id, manifest)
    completion = _check_completion(root, task_id, task)
    completion_ready = (task.get("completion") or {}).get("status") == "READY" or _load(
        root / "tasks" / task_id / "completion" / "completion.json"
    ).get("status") == "READY"
    sufficiency = _check_sufficiency(root, task_id, task, bool(completion["ok"]), bool(completion_ready))
    intent_refs = list(intent.get("obligation_refs") or [])
    verifier = _check_verifier(root, task_id, task, intent_refs)
    red = _check_red(manifest, task)
    hygiene = _mixed_errors(
        task,
        list(intent.get("errors") or []),
        list(completion.get("errors") or []),
    )
    secrets = _scan_secrets(root, _secret_values(config, channel))
    secret_stage = _stage(
        "secrets",
        ok=not secrets,
        kind="logic" if secrets else "ok",
        detail="SECRET_LEAK" if secrets else "no secrets in artifacts",
        extra={"leaks": secrets},
    )
    rollout = _check_rollout(root, task)
    stages = [source, intent, completion, sufficiency, verifier, red, hygiene, secret_stage, rollout]
    core_stages = [source, intent, completion, sufficiency, verifier, red, hygiene, secret_stage]
    related_ready = (
        intent.get("status") == "READY"
        and completion.get("status") == "READY"
        and sufficiency.get("status") == "READY"
        and verifier.get("status") == "READY"
    )
    pipeline_ok = bool(
        manifest.get("status") == "READY"
        and related_ready
        and all(stage["ok"] for stage in core_stages)
    )
    delivery_ready = bool(pipeline_ok and red["ok"])
    end_to_end_ready = bool(delivery_ready and rollout["ok"])
    return {
        "root": str(root),
        "task_id": task_id,
        "manifest_status": manifest.get("status"),
        "stopped_at": task.get("stopped_at") or manifest.get("stopped_at"),
        "pipeline_ok": pipeline_ok,
        "delivery_ready": delivery_ready,
        "end_to_end_ready": end_to_end_ready,
        "stages": stages,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--channel", default="claude")
    parser.add_argument("--task-id", default="task_828b37beaca277cc500e")
    arguments = parser.parse_args()
    report = grade(
        arguments.root,
        config=arguments.config,
        channel=arguments.channel,
        expected_task_id=arguments.task_id,
    )
    dest = arguments.output or (arguments.root / "e2e-report.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(dest)
    print(json.dumps({k: report[k] for k in ("pipeline_ok", "delivery_ready", "end_to_end_ready", "manifest_status", "stopped_at")}, ensure_ascii=False))
    return 0 if report["pipeline_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
