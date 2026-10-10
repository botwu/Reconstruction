"""把已认证的 terminal 实跑正文交给后审；完整执行证据不等于答卷正确。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.harbor_ags.results import HarborResultError, read_native_trial

_FINAL_WORKSPACE = Path("artifacts/logs/artifacts/traceforge/workspace")


def _bound_bytes(path: Path, root: Path, expected: str) -> bytes:
    """只读已绑定的普通文件，并核对本次实际交付的字节。"""
    if (not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve())
            or any(item.is_symlink() for item in (path, *path.parents)
                   if item == root or root in item.parents)):
        raise HarborResultError(f"TERMINAL_REVIEW_UNSAFE_PATH:{path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HarborResultError(f"TERMINAL_REVIEW_EVIDENCE_UNREADABLE:{path}") from exc
    if hashlib.sha256(raw).hexdigest() != expected:
        raise HarborResultError(f"TERMINAL_REVIEW_EVIDENCE_CHANGED:{path}")
    return raw


def _file_rows(root: Path, hashes: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for name, digest in sorted(hashes.items()):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise HarborResultError(f"TERMINAL_REVIEW_UNSAFE_PATH:{name}")
        raw = _bound_bytes(root / relative, root, digest)
        row: dict[str, Any] = {
            "path": name, "sha256": digest, "size_bytes": len(raw), "content_kind": "binary",
        }
        try:
            if b"\x00" not in raw:
                row.update(content_kind="text", content=raw.decode("utf-8"))
        except UnicodeDecodeError:
            pass
        rows.append(row)
    return rows


def build_terminal_rollout_evidence(native_trials: list[dict[str, Any]]) -> dict[str, Any]:
    """复用原生认证，完整交付对话、工具返回和前后文件；二进制仅声明元数据。"""
    if not native_trials:
        raise HarborResultError("TERMINAL_REVIEW_TRIALS_MISSING")
    reviews, references, seen = [], [], set()
    for trial in native_trials:
        receipt = trial.get("receipt") or {}
        if (trial.get("completed") is not True or trial.get("errors")
                or receipt.get("source_binding") is not True
                or receipt.get("final_workspace_binding") is not True
                or receipt.get("collection") != "VERIFIED"):
            raise HarborResultError("TERMINAL_REVIEW_TRIAL_UNBOUND")
        final = Path(receipt.get("final_workspace") or "")
        if (not final.is_absolute() or final.parts[-len(_FINAL_WORKSPACE.parts):]
                != _FINAL_WORKSPACE.parts):
            raise HarborResultError("TERMINAL_REVIEW_WORKSPACE_PATH_INVALID")
        root = final.parents[len(_FINAL_WORKSPACE.parts) - 1]
        if root.name != trial.get("trial") or root in seen:
            raise HarborResultError("TERMINAL_REVIEW_TRIAL_PATH_INVALID")
        seen.add(root)
        task = Path(receipt.get("input_task") or "")
        if not task.is_absolute():
            raise HarborResultError("TERMINAL_REVIEW_INPUT_TASK_MISSING")
        current = read_native_trial(root, expected_task=task, domain="terminal")
        if not current["completed"]:
            raise HarborResultError("TERMINAL_REVIEW_REVALIDATION_FAILED:"
                                    + ",".join(current["errors"]))
        if current != trial:
            raise HarborResultError(f"TERMINAL_REVIEW_RECEIPT_CHANGED:{root.name}")
        evidence = receipt["evidence_files"]
        trajectory_path = "agent/trajectory.full.json"
        full = json.loads(_bound_bytes(
            root / trajectory_path, root, evidence[trajectory_path]))
        if any(key not in full for key in ("messages", "system_prompt", "tools")):
            raise HarborResultError("TERMINAL_REVIEW_CONVERSATION_INCOMPLETE")
        initial_hashes = {item["path"]: item["sha256"]
                          for item in full["task_input"]["workspace"]["files"]}
        prefix = _FINAL_WORKSPACE.as_posix() + "/"
        final_hashes = {name.removeprefix(prefix): digest for name, digest in evidence.items()
                        if name.startswith(prefix)}
        name = trial["trial"]
        references.extend([
            f"{name}/answer", f"{name}/system_prompt", f"{name}/tool_definitions",
            *(f"{name}/messages/{index}" for index in range(len(full["messages"]))),
            *(f"{name}/tools/{event['tool_call_id']}" for event in trial["tool_events"]),
            *(f"{name}/initial/{path}" for path in sorted(initial_hashes)),
            *(f"{name}/files/{path}" for path in sorted(final_hashes)),
        ])
        reviews.append({
            **copy.deepcopy(trial),
            "initial_files": _file_rows(task / "workspace", initial_hashes),
            "final_files": _file_rows(final, final_hashes),
        })
    return {"schema_version": "traceforge.terminal-rollout-evidence.v1",
            "trials": reviews, "evidence_refs": references}


def terminal_review_outcome(
    judge: dict[str, Any], environment: dict[str, Any], sufficiency_path: Path,
) -> dict[str, Any]:
    """正常管线和后审重试使用同一判定；实跑完成不等于答案已评分。"""
    from traceforge.reconstruction.task_fit import environment_execution_blockers

    review = {**(judge.get("rollout_review") or {}),
              "sufficiency_path": str(sufficiency_path),
              "initial_environment_label": judge.get("label")}
    errors = []
    if review.get("status") != "COMPLETE":
        errors = review.get("errors") or ["ROLLOUT_REVIEW_INCOMPLETE"]
    elif any(item["status"] == "ENVIRONMENT_GAP" for item in review["requirements"]):
        errors = ["ROLLOUT_ENVIRONMENT_GAP"]
    elif judge.get("status") != "READY" or environment_execution_blockers(environment):
        errors = ["INITIAL_ENVIRONMENT_REVIEW_UNRESOLVED", *judge.get("errors", [])]
    return {
        "status": "REVIEW" if errors else "ROLLOUT_COMPLETED",
        "stopped_at": "rollout_review" if errors else None,
        "errors": errors, "rollout_review": review,
        "acceptance": "NOT_ASSESSED", "sft_eligible": False,
    }
