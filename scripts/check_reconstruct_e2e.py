"""按阶段验收 reconstruct run 活跑产物，不打印密钥或文件正文。"""

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
    if len(files) < 5:
        errors.append(f"REPLAY_FILE_COUNT_{len(files)}")
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
    errors = [str(x) for x in (completion.get("errors") or task.get("errors") or [])]
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
    ran = path.is_file() and "SANDBOX_INIT" not in problems and "CONTAINER_REQUIRED" not in problems
    if completion.get("status") != "READY" and ran:
        if kind == "ok":
            kind = "content"
        if "COMPLETION_NOT_READY" not in problems:
            problems.append("COMPLETION_NOT_READY")
    return _stage(
        "completion",
        ok=ran and "SANDBOX_INIT" not in problems and "CONTAINER_REQUIRED" not in problems,
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


def _check_sufficiency(root: Path, task_id: str, task: dict[str, Any], completion_ok: bool, completion_ready: bool) -> dict[str, Any]:
    sufficiency_root = root / "tasks" / task_id / "sufficiency"
    present = sufficiency_root.is_dir() and any(sufficiency_root.glob("*/sufficiency.json"))
    if not completion_ready:
        return _stage(
            "sufficiency",
            ok=True,
            kind="ok",
            detail="skipped; completion not READY",
            extra={"present": present},
        )
    if not present:
        return _stage(
            "sufficiency",
            ok=False,
            kind="logic" if completion_ok else "infra",
            detail="SUFFICIENCY_MISSING_AFTER_COMPLETION_READY",
        )
    judges = []
    for path in sorted(sufficiency_root.glob("*/sufficiency.json")):
        payload = _load(path)
        judges.append(
            {
                "label": payload.get("label") or payload.get("decision"),
                "errors": payload.get("errors") or [],
            }
        )
        if any(str(item).startswith("SANDBOX_INIT") for item in (payload.get("errors") or [])):
            return _stage(
                "sufficiency",
                ok=False,
                kind="infra",
                detail="SANDBOX_INIT",
                extra={"judges": judges},
            )
    return _stage(
        "sufficiency",
        ok=True,
        kind="ok",
        detail="sufficiency ran",
        extra={"judges": judges, "audit": (task.get("sufficiency_audit") or {}).get("status")},
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


def _check_red(manifest: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    verification = task.get("verification") if isinstance(task.get("verification"), dict) else {}
    red = verification.get("red") or verification.get("calibration") or {}
    executed = bool(red) or verification.get("status") == "READY"
    if manifest.get("status") == "READY":
        if not executed:
            return _stage(
                "red",
                ok=False,
                kind="logic",
                detail="READY_WITHOUT_RED",
            )
        return _stage("red", ok=True, kind="ok", detail="READY after RED", extra={"status": "READY"})
    if not executed:
        return _stage(
            "red",
            ok=True,
            kind="ok",
            detail="RED not executed; READY forbidden",
            extra={"manifest_status": manifest.get("status")},
        )
    return _stage(
        "red",
        ok=verification.get("status") == "READY",
        kind="content" if verification.get("status") != "READY" else "ok",
        detail=str(verification.get("status") or "RED_REVIEW"),
        extra={"status": verification.get("status")},
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
    stages = [source, intent, completion, sufficiency, verifier, red, hygiene, secret_stage]
    pipeline_ok = bool(intent["ok"] and completion["ok"])
    delivery_ready = manifest.get("status") == "READY" and bool(red["ok"]) and "READY_WITHOUT_RED" not in str(red.get("detail"))
    return {
        "root": str(root),
        "task_id": task_id,
        "manifest_status": manifest.get("status"),
        "stopped_at": task.get("stopped_at") or manifest.get("stopped_at"),
        "pipeline_ok": pipeline_ok,
        "delivery_ready": delivery_ready,
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
    print(json.dumps({k: report[k] for k in ("pipeline_ok", "delivery_ready", "manifest_status", "stopped_at")}, ensure_ascii=False))
    return 0 if report["pipeline_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
