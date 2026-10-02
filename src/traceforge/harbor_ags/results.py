"""读取 Harbor Job 结果并计算不含臆测的 rollout 指标。"""

from __future__ import annotations

import hashlib
import importlib
import json
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROLLOUT_RESULTS_SCHEMA = "traceforge.harbor-ags-rollout-results.v1"
_HARBOR_AGS_SRC = Path(
    "/mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags/src"
)


class HarborResultError(RuntimeError):
    """Harbor 结果目录缺失或内容不满足契约。"""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborResultError(f"无法读取 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise HarborResultError(f"JSON 根节点必须是对象：{path}")
    return value


def _trial_diagnostic(trial_dir: Path, result: dict[str, Any]) -> dict[str, str]:
    """保留短的失败原因，避免只暴露 TrajectoryCaptureError 包装层。"""
    diagnostic: dict[str, str] = {}
    exception = result.get("exception_info")
    if isinstance(exception, dict):
        kind = exception.get("exception_type")
        message = exception.get("exception_message")
        if isinstance(kind, str) and kind:
            diagnostic["error_code"] = kind
        if isinstance(message, str) and message:
            diagnostic["error_detail"] = message[-1200:]
    try:
        payload = json.loads((trial_dir / "agent/hermes-result.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = None
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if isinstance(meta, dict) and meta.get("failed") is True:
        final_response = meta.get("final_response")
        if isinstance(final_response, str) and final_response:
            diagnostic["agent_error"] = final_response[-1200:]
    if isinstance(exception, dict):
        traceback = exception.get("exception_traceback")
        evidence = traceback if isinstance(traceback, str) else ""
        causes = (
            "RemoteProtocolError", "SandboxException", "ConnectException",
            "EvidenceError", "TimeoutExpired",
        )
        cause = next((marker for marker in causes if marker in evidence), None)
        if cause is None:
            # 长日志只读取末尾；优先保留结构化 traceback 给出的原因。
            try:
                with (trial_dir / "trial.log").open("rb") as log:
                    log.seek(0, 2)
                    log.seek(max(0, log.tell() - 65536))
                    evidence = log.read().decode("utf-8", errors="replace")
            except OSError:
                evidence = ""
            cause = next((marker for marker in causes if marker in evidence), None)
        if cause is not None:
            diagnostic["cause_code"] = cause
    return diagnostic


def _duration_seconds(value: dict[str, Any]) -> float | None:
    started = value.get("started_at")
    finished = value.get("finished_at")
    try:
        if isinstance(started, str) and isinstance(finished, str):
            left = datetime.fromisoformat(started.replace("Z", "+00:00"))
            right = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            return max(0.0, (right - left).total_seconds())
    except ValueError:
        return None
    details = value.get("agent_result")
    if isinstance(details, dict):
        metadata = details.get("metadata")
        evidence = metadata.get("_harbor_ags_evidence") if isinstance(metadata, dict) else None
        elapsed = evidence.get("elapsed_s") if isinstance(evidence, dict) else None
        if isinstance(elapsed, int | float):
            return float(elapsed)
    return None


def _trial_status(result: dict[str, Any], verdict: dict[str, Any] | None) -> str:
    exception = result.get("exception_info")
    if isinstance(exception, dict):
        kind = str(exception.get("exception_type", ""))
        return "TIMEOUT" if "Timeout" in kind else "INFRA_ERROR"
    verifier_result = result.get("verifier_result")
    rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
    reward = rewards.get("task") if isinstance(rewards, dict) else None
    verdict_status = verdict.get("status") if verdict else None
    if reward == 1.0 and verdict_status in {"TASK_PASS", "PASS"}:
        return "PASS"
    if reward == 0.0 and verdict_status in {"TASK_FAIL", "FAIL"}:
        return "FAIL"
    return "INCONCLUSIVE"


def _import_audit_sandbox_ledger() -> Any:
    try:
        from harbor_ags.sandbox_ledger import audit_sandbox_ledger

        return audit_sandbox_ledger
    except ImportError:
        pass
    candidates = (
        _HARBOR_AGS_SRC,
        Path(__file__).resolve().parents[4] / "harbor_ags" / "src",
    )
    for source in candidates:
        if not (source / "harbor_ags" / "sandbox_ledger.py").is_file():
            continue
        inserted = str(source)
        if inserted not in sys.path:
            sys.path.insert(0, inserted)
        try:
            from harbor_ags.sandbox_ledger import audit_sandbox_ledger

            return audit_sandbox_ledger
        except ImportError:
            continue
    raise HarborResultError("无法导入 harbor_ags.sandbox_ledger.audit_sandbox_ledger")


def _cleanup_ok(ledger: Path) -> bool | None:
    if not ledger.is_file():
        return None
    try:
        audit = _import_audit_sandbox_ledger()(ledger)
    except Exception:
        return False
    return bool(getattr(audit, "ok", False))


def _requires_hermes_artifacts(agent_mode: str) -> bool:
    return agent_mode == "hermes"


def _validate_hermes_artifacts(trial_dir: Path) -> tuple[bool, list[str]]:
    """Validate the content of Hermes artifacts, rather than only their paths.

    A present-but-corrupt trajectory must never be eligible for SFT.  The
    artifact manifest is generated by the Harbor control plane and must have
    the locked schema and at least one hashed file.
    """

    errors: list[str] = []
    trajectory = trial_dir / "agent/trajectory.full.json"
    manifest = trial_dir / "artifacts/manifest.json"
    try:
        value = json.loads(trajectory.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != "traceforge-lossless-trajectory-v1" or not value.get("messages"):
            errors.append("TRAJECTORY_SCHEMA_OR_MESSAGES_INVALID")
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append("TRAJECTORY_INVALID_JSON")
    try:
        payload = _read_json(manifest)
        if payload.get("schema_version") != "traceforge-harbor-artifacts/v1":
            errors.append("ARTIFACT_MANIFEST_SCHEMA_INVALID")
        files = payload.get("files")
        if not isinstance(files, list) or not files:
            errors.append("ARTIFACT_MANIFEST_EMPTY")
        else:
            for item in files:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    errors.append("ARTIFACT_MANIFEST_ENTRY_INVALID")
                    break
                rel = Path(item["path"])
                artifact_root = trial_dir / "artifacts"
                artifact = artifact_root / rel
                if rel.is_absolute() or ".." in rel.parts or artifact.is_symlink() or not artifact.resolve().is_relative_to(artifact_root.resolve()):
                    errors.append("ARTIFACT_MANIFEST_PATH_UNSAFE")
                    break
                if not artifact.is_file():
                    errors.append("ARTIFACT_MANIFEST_FILE_MISSING")
                    break
                if item.get("sha256") != hashlib.sha256(artifact.read_bytes()).hexdigest():
                    errors.append("ARTIFACT_MANIFEST_HASH_MISMATCH")
                    break
    except HarborResultError:
        errors.append("ARTIFACT_MANIFEST_INVALID_JSON")
    try:
        receipt = _read_json(trial_dir / "reconstruction-certification.json")
        if receipt.get("schema_version") != "traceforge.rollout-certification.v1" or receipt.get("certified") is not True:
            errors.append("HERMES_CERTIFICATION_FAILED")
        for label, path in (("trajectory_sha256", trajectory), ("manifest_sha256", manifest)):
            if not path.is_file() or receipt.get(label) != hashlib.sha256(path.read_bytes()).hexdigest():
                errors.append("HERMES_CERTIFICATION_STALE")
    except (HarborResultError, OSError):
        errors.append("HERMES_CERTIFICATION_MISSING")
    return not errors, list(dict.fromkeys(errors))


def certify_hermes_job(job_dir: Path | str, *, harbor_root: Path | str | None = None) -> None:
    """保存可信 validator 结果，并将认证绑定到当前轨迹与 manifest 字节。"""
    root = Path(job_dir).resolve()
    sources = ([Path(harbor_root).resolve() / "src"] if harbor_root is not None else []) + [
        _HARBOR_AGS_SRC, Path(__file__).resolve().parents[4] / "harbor_ags" / "src",
    ]
    for source in sources:
        if (source / "harbor_ags/artifacts.py").is_file():
            if str(source) not in sys.path:
                sys.path.insert(0, str(source))
            break
    for trial_dir in sorted(path for path in root.iterdir() if path.is_dir() and path.name != "_control"):
        receipt: dict[str, Any] = {"schema_version": "traceforge.rollout-certification.v1", "certified": False}
        try:
            from harbor_ags.artifacts import build_artifact_manifest
            validator = importlib.import_module("harbor_ags.validator")
            build_artifact_manifest(trial_dir)
            validated = validator.validate_harbor_trial(trial_dir, preserve_source_literals=True)
            receipt["certified"] = getattr(validated, "certified", False) is True
            receipt["validator_status"] = str(getattr(validated, "status", "UNKNOWN"))
            receipt["validator_errors"] = getattr(validated, "errors", [])
            receipt["validator_warnings"] = getattr(validated, "warnings", [])
            receipt["validator_source_sha256"] = hashlib.sha256(Path(validator.__file__).read_bytes()).hexdigest()
        except Exception as exc:
            receipt["error_code"] = type(exc).__name__
        for label, path in (
            ("trajectory_sha256", trial_dir / "agent/trajectory.full.json"),
            ("manifest_sha256", trial_dir / "artifacts/manifest.json"),
        ):
            if path.is_file():
                receipt[label] = hashlib.sha256(path.read_bytes()).hexdigest()
        (trial_dir / "reconstruction-certification.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rollout_passed(rollout: dict[str, Any] | None, expected_trials: int) -> bool:
    """只把完整且通过质量门的多次 Hermes 复验视为成功。"""
    if not isinstance(rollout, dict):
        return False
    execution = rollout.get("execution")
    if not isinstance(execution, dict) or execution.get("status") != "COMPLETED":
        return False
    results = rollout.get("results")
    gate = results.get("quality_gate") if isinstance(results, dict) else None
    if not isinstance(gate, dict) or gate.get("ok") is not True:
        return False
    trials = results.get("trials")
    return (
        isinstance(trials, list)
        and len(trials) == expected_trials
        and all(
            isinstance(trial, dict)
            and trial.get("status") == "PASS"
            and not isinstance(trial.get("reward"), bool)
            and trial.get("reward") == 1.0
            for trial in trials
        )
    )


def read_search_trial(
    trial_dir: Path | str, *, expected_task: Path | str | None = None,
    harbor_root: Path | str | None = None,
) -> dict[str, Any]:
    """读取并对账原生检索轨迹，完成状态不代表回答内容通过。"""
    root = Path(trial_dir).resolve()
    output: dict[str, Any] = {
        "trial": root.name, "answer": "", "tool_events": [], "model": None,
        "errors": [], "completed": False, "final_stop_reason": None, "web_cache_root": None,
        "receipt": {"backend": "native_harbor", "acceptance": "NOT_ASSESSED",
                    "evidence_files": {}, "input_task": None, "source_binding": False},
    }
    try:
        if harbor_root is not None:
            runtime_source = str(Path(harbor_root).resolve() / "src")
            if runtime_source not in sys.path:
                sys.path.insert(0, runtime_source)
        evidence = importlib.import_module("harbor_ags.evidence")
        if harbor_root is not None and not Path(evidence.__file__).resolve().is_relative_to(
            Path(harbor_root).resolve()
        ):
            raise HarborResultError("NATIVE_EVIDENCE_RUNTIME_MISMATCH")
        result = _read_json(root / "result.json")
        if result.get("exception_info") or not result.get("finished_at"):
            raise HarborResultError("NATIVE_TRIAL_NOT_COMPLETED")
        full = _read_json(root / "agent/trajectory.full.json")
        if full.get("schema_version") != "traceforge-lossless-trajectory-v1":
            raise HarborResultError("NATIVE_TRAJECTORY_SCHEMA_INVALID")
        rebuilt = evidence.build_full_trajectory(root / "agent", metadata=full.get("metadata"))
        for field in ("messages", "anthropic_calls", "tools", "system_prompt",
                      "task_input", "hashes", "harness_drift"):
            if full.get(field) != rebuilt.get(field):
                raise HarborResultError(f"NATIVE_CAPTURE_BINDING_MISMATCH:{field}")
        atif = _read_json(root / "agent/trajectory.json")
        reconciliation = evidence.reconcile_evidence(full, atif)
        if reconciliation.get("ok") is not True:
            codes = [str(item.get("code")) for item in reconciliation.get("issues", [])]
            raise HarborResultError("NATIVE_RECONCILIATION_FAILED:" + ",".join(codes))
        calls = full.get("anthropic_calls")
        if not isinstance(calls, list) or not calls:
            raise HarborResultError("NATIVE_MODEL_CALLS_MISSING")
        events: dict[str, dict[str, Any]] = {}
        results: dict[str, tuple[int, dict[str, Any]]] = {}
        for index, call in enumerate(calls):
            for block in call.get("response", {}).get("content", []):
                if block.get("type") == "tool_use":
                    identity = block.get("id")
                    if not isinstance(identity, str) or identity in events:
                        raise HarborResultError("NATIVE_TOOL_CALL_ID_INVALID")
                    events[identity] = {
                        "tool_call_id": identity, "name": block["name"],
                        "arguments": block["input"],
                        "source": {"response_call_index": index},
                    }
            for message in call.get("request", {}).get("messages", []):
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if block.get("type") == "tool_result":
                        results.setdefault(block["tool_use_id"], (index, block))
        for identity, event in events.items():
            if identity not in results:
                raise HarborResultError(f"NATIVE_TOOL_RESULT_MISSING:{identity}")
            index, block = results[identity]
            value = block.get("content")
            event.update(result=value, ok=block.get("is_error") is not True,
                         result_sha256=hashlib.sha256(json.dumps(
                             value, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
            event["source"]["result_request_call_index"] = index
        response = calls[-1].get("response", {})
        final = response.get("content", [])
        answer = "\n".join(block["text"] for block in final
                           if block.get("type") == "text" and isinstance(block.get("text"), str))
        output.update(answer=answer, tool_events=list(events.values()),
                      model=full.get("model"), final_stop_reason=response.get("stop_reason"))
        task_input = full.get("task_input") or {}
        if expected_task is not None:
            from .adapter import validate_search_delivery

            task = Path(expected_task).resolve()
            validate_search_delivery(task)
            original = (task / "instruction.md").read_text(encoding="utf-8")
            component = task_input.get("instruction", {}).get("task_instruction", {})
            if component.get("content") != original:
                raise HarborResultError("NATIVE_TASK_INSTRUCTION_MISMATCH")
            observed = {item["path"]: item["sha256"]
                        for item in task_input.get("workspace", {}).get("files", [])}
            expected = {p.relative_to(task / "workspace").as_posix():
                        hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (task / "workspace").rglob("*") if p.is_file()}
            if observed != expected:
                raise HarborResultError("NATIVE_INITIAL_WORKSPACE_MISMATCH")
            output["receipt"].update(input_task=str(task), source_binding=True)
        sources = [
            root / "result.json", root / "agent/trajectory.full.json",
            root / "agent/trajectory.json", root / "agent/anthropic-exchanges.jsonl",
            root / "agent/anthropic-sse.jsonl", root / "agent/hermes-session.jsonl",
            root / "agent/task-input.json", root / "agent/workspace-initial-manifest.json",
        ]
        output["receipt"]["evidence_files"] = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        }
        if output["final_stop_reason"] in {"max_tokens", "length", "model_context_window_exceeded"}:
            raise HarborResultError("NATIVE_FINAL_RESPONSE_TRUNCATED")
        if any(block.get("type") == "tool_use" for block in final):
            raise HarborResultError("NATIVE_FINAL_RESPONSE_PENDING_TOOL")
        if output["final_stop_reason"] in {"pause_turn", "tool_use"}:
            raise HarborResultError("NATIVE_FINAL_RESPONSE_INCOMPLETE")
        if not answer.strip():
            raise HarborResultError("NATIVE_FINAL_RESPONSE_EMPTY")
        web_cache = root / "artifacts/logs/artifacts/search"
        if web_cache.is_dir():
            if not web_cache.resolve().is_relative_to(root):
                raise HarborResultError("NATIVE_WEB_CACHE_PATH_INVALID")
            output["web_cache_root"] = str(web_cache.resolve())
        output["completed"] = True
    except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError, AttributeError) as exc:
        output["errors"] = [str(exc) if isinstance(exc, HarborResultError) else type(exc).__name__]
    return output



def _content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _workspace_hashes(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise HarborResultError(f"FILE_SNAPSHOT_WORKSPACE_MISSING_OR_UNSAFE:{root}")
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode) or not path.resolve().is_relative_to(root.resolve()):
            raise HarborResultError(f"FILE_SNAPSHOT_UNSAFE:{path}")
        files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def build_file_artifact_snapshot(
    trial_dir: Path | str, *, expected_task: Path | str | None = None,
) -> dict[str, Any]:
    """绑定真实初态、collect 钩子回收的终态和执行字节，不接受模型自报路径。"""

    root = Path(trial_dir).resolve()
    config = _read_json(root / "config.json")
    configured = config.get("task")
    task_path = configured.get("path") if isinstance(configured, dict) else None
    if not isinstance(task_path, str) or not Path(task_path).is_absolute():
        raise HarborResultError("FILE_SNAPSHOT_TASK_PATH_INVALID")
    task = Path(task_path).resolve()
    if expected_task is not None and task != Path(expected_task).resolve():
        raise HarborResultError("FILE_SNAPSHOT_TASK_MISMATCH")
    control = task / "tests/control/input-manifest.json"
    manifest = _read_json(control)
    if manifest.get("schema_version") != "traceforge.control-input-manifest.v1":
        raise HarborResultError("FILE_SNAPSHOT_CONTROL_INVALID")
    acceptance = manifest.get("task_acceptance") or {}
    checks = acceptance.get("file_semantic_checks", {})
    if not isinstance(checks, dict) or any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not value.strip()
        for key, value in checks.items()
    ):
        raise HarborResultError("FILE_SEMANTIC_CHECKS_INVALID")
    initial = task / "workspace"
    initial_files = _workspace_hashes(initial)
    if initial_files != manifest.get("workspace_sha256"):
        raise HarborResultError("FILE_SNAPSHOT_INITIAL_WORKSPACE_MISMATCH")
    final = root / "artifacts/logs/artifacts/traceforge/workspace"
    if any(path.is_symlink() for path in [final, *final.parents] if path != root and root in path.parents):
        raise HarborResultError("FILE_SNAPSHOT_UNSAFE")
    final_files = _workspace_hashes(final)
    result = _read_json(root / "result.json")
    if result.get("exception_info") or not result.get("finished_at"):
        raise HarborResultError("FILE_SNAPSHOT_EXECUTION_INCOMPLETE")
    result_task = (result.get("config") or {}).get("task") or {}
    if result_task.get("path") and Path(result_task["path"]).resolve() != task:
        raise HarborResultError("FILE_SNAPSHOT_TASK_MISMATCH")
    execution_files = ["config.json", "result.json", "verifier/verdict.json"]
    execution_files.extend(name for name in (
        "agent/trajectory.full.json", "agent/trajectory.json", "agent/hermes-result.json",
        "agent/anthropic-exchanges.jsonl", "agent/anthropic-sse.jsonl",
        "agent/hermes-session.jsonl", "agent/task-input.json",
        "agent/workspace-initial-manifest.json", "agent/workspace-manifest.json",
        "agent/oracle.txt", "agent/exit-code.txt",
    ) if (root / name).is_file())
    try:
        execution = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in execution_files
        }
        task_files = {
            name: hashlib.sha256((task / name).read_bytes()).hexdigest()
            for name in ("instruction.md", "task.toml", "tests/control/input-manifest.json")
        }
        test_sha256 = hashlib.sha256((task / "tests/test_outputs.py").read_bytes()).hexdigest()
    except OSError as exc:
        raise HarborResultError(f"FILE_SNAPSHOT_INPUT_MISSING:{exc.filename}") from exc
    binding = {
        "schema_version": "traceforge.file-artifact-binding.v1",
        "trial_path": str(root), "task_path": str(task),
        "task_sha256": _content_hash(task_files), "test_sha256": test_sha256,
        "criteria_sha256": _content_hash(checks),
        "initial_sha256": _content_hash(initial_files), "final_sha256": _content_hash(final_files),
        "execution_sha256": _content_hash(execution),
    }
    # 向审查者交付测试和实际行为正文；含进程配置的 config/result 只绑定哈希。
    evidence_files = {"verifier/test_outputs.py": str(task / "tests/test_outputs.py")}
    evidence_files.update({
        name: str(root / name) for name in (
            "verifier/verdict.json", "agent/trajectory.full.json", "agent/trajectory.json",
            "agent/oracle.txt", "agent/exit-code.txt",
        ) if name in execution
    })
    return {
        "initial_workspace": str(initial), "workspace": str(final),
        "initial_files": initial_files, "final_files": final_files, "binding": binding,
        "evidence_files": evidence_files,
    }


def validate_file_semantic_receipt(
    receipt: dict[str, Any], snapshot: dict[str, Any], checks: dict[str, str],
    *, require_accepted: bool = True,
) -> list[str]:
    """复验来源、判断与实际引文；校准可读取合法 REVISE，正式验收仅接受 ACCEPT。"""

    if (receipt.get("schema_version") != "traceforge.file-semantic-review.v1"
            or receipt.get("binding") != snapshot["binding"]
            or snapshot["binding"]["criteria_sha256"] != _content_hash(checks)):
        return ["FILE_SEMANTIC_BINDING_MISMATCH"]
    if receipt.get("errors"):
        return ["FILE_SEMANTIC_REVIEW_ERRORS"]
    status = receipt.get("status")
    if status not in {"ACCEPT", "REVISE"} or (require_accepted and status != "ACCEPT"):
        return [f"FILE_SEMANTIC_NOT_ACCEPTED:{status or 'UNKNOWN'}"]
    obligations = receipt.get("obligations")
    if not isinstance(obligations, list):
        return ["FILE_SEMANTIC_OBLIGATIONS_MISSING"]
    ids = [item.get("obligation_id") for item in obligations if isinstance(item, dict)]
    if (len(ids) != len(obligations) or len(ids) != len(checks)
            or any(not isinstance(oid, str) for oid in ids) or set(ids) != set(checks)):
        return ["FILE_SEMANTIC_OBLIGATIONS_MISMATCH"]
    if (any(type(item.get("covered")) is not bool for item in obligations)
            or (status == "ACCEPT") != all(item["covered"] for item in obligations)):
        return ["FILE_SEMANTIC_DECISION_INCONSISTENT"]
    errors: list[str] = []
    for item in obligations:
        oid = item["obligation_id"]
        evidence = item.get("evidence")
        if (not isinstance(item.get("reason"), str) or not item["reason"].strip()
                or not isinstance(evidence, list) or not evidence):
            errors.append(f"FILE_SEMANTIC_EVIDENCE_MISSING:{oid}")
            continue
        final_evidence = False
        for ref in evidence:
            path = ref.get("path") if isinstance(ref, dict) else None
            relative = Path(path) if isinstance(path, str) else Path()
            if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2
                    or relative.parts[0] not in {"initial", "final"}):
                errors.append(f"FILE_SEMANTIC_EVIDENCE_INVALID:{oid}")
                continue
            is_final = relative.parts[0] == "final"
            name = Path(*relative.parts[1:]).as_posix()
            if ref.get("absent") is True:
                valid = (is_final and name in snapshot["initial_files"]
                         and name not in snapshot["final_files"])
            else:
                quote = ref.get("quote")
                workspace = Path(snapshot["workspace"] if is_final else snapshot["initial_workspace"])
                actual = workspace / name
                try:
                    valid = (isinstance(quote, str) and bool(quote.strip())
                             and actual.resolve().is_relative_to(workspace.resolve())
                             and quote in actual.read_text())
                except (OSError, UnicodeError):
                    valid = False
            if not valid:
                errors.append(f"FILE_SEMANTIC_EVIDENCE_INVALID:{oid}")
            elif is_final:
                final_evidence = True
        if not final_evidence:
            errors.append(f"FILE_SEMANTIC_FINAL_EVIDENCE_MISSING:{oid}")
    return list(dict.fromkeys(errors))


def read_rollout_results(
    job_dir: Path | str,
    *,
    agent_mode: str = "hermes",
    expected_trial_count: int | None = None,
    domain: str = "terminal",
    expected_task: Path | str | None = None,
    harbor_root: Path | str | None = None,
) -> dict[str, Any]:
    """读取 Job 下每个 trial 的 reward/verdict/trajectory 并聚合指标。"""

    if domain not in {"terminal", "search"}:
        raise HarborResultError("domain 必须是 terminal 或 search")
    search = domain == "search"
    root = Path(job_dir).resolve()
    if not root.is_dir():
        raise HarborResultError(f"Job 目录不存在：{root}")
    trials: list[dict[str, Any]] = []
    trial_dirs = sorted(
        path for path in root.iterdir() if path.is_dir() and path.name != "_control"
    )
    hermes_artifacts = _requires_hermes_artifacts(agent_mode)
    for trial_dir in trial_dirs:
        result_path = trial_dir / "result.json"
        if not result_path.is_file():
            trials.append(
                {
                    "trial_name": trial_dir.name,
                    "status": "INFRA_ERROR",
                    "reward": None,
                    "verdict_status": None,
                    "trajectory_present": False,
                    "artifact_manifest_present": False,
                    "tokens": {"input": None, "cache": None, "output": None},
                    "duration_seconds": None,
                    "result_path": str(result_path),
                    "verdict_path": None,
                    "error_code": "TRIAL_RESULT_MISSING",
                }
            )
            continue
        result = _read_json(result_path)
        verdict_path = trial_dir / "verifier/verdict.json"
        verdict = _read_json(verdict_path) if verdict_path.is_file() else None
        trajectory_path = trial_dir / "agent/trajectory.full.json"
        artifact_manifest_path = trial_dir / "artifacts/manifest.json"
        agent_result = (
            result.get("agent_result") if isinstance(result.get("agent_result"), dict) else {}
        )
        verifier_result = result.get("verifier_result")
        rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
        token_info = {
            "input": agent_result.get("n_input_tokens"),
            "cache": agent_result.get("n_cache_tokens"),
            "output": agent_result.get("n_output_tokens"),
        }
        native = (read_search_trial(trial_dir, expected_task=expected_task, harbor_root=harbor_root)
                  if search else None)
        if native is not None:
            content_valid, content_errors = native["completed"], native["errors"]
        else:
            content_valid, content_errors = (
                _validate_hermes_artifacts(trial_dir) if hermes_artifacts else (True, [])
            )
        snapshot = None
        if not search:
            config_path = trial_dir / "config.json"
            if config_path.is_file():
                task_config = _read_json(config_path).get("task") or {}
                task_path = task_config.get("path")
                control = Path(task_path) / "tests/control/input-manifest.json" if task_path else None
                if control is not None and control.is_file():
                    checks = (_read_json(control).get("task_acceptance") or {}).get("file_semantic_checks")
                    if checks:
                        try:
                            snapshot = build_file_artifact_snapshot(trial_dir)
                        except (HarborResultError, OSError, ValueError, TypeError) as exc:
                            content_valid = False
                            content_errors = [*content_errors, str(exc)]
        diagnostic = _trial_diagnostic(trial_dir, result)
        trials.append(
            {
                "trial_name": trial_dir.name,
                "status": ("COMPLETED" if content_valid else "INFRA_ERROR")
                if search else _trial_status(result, verdict),
                **({"acceptance": "NOT_ASSESSED", "native_trial": native} if search else {}),
                **({"artifact_snapshot": snapshot} if snapshot is not None else {}),
                "reward": rewards.get("task") if isinstance(rewards, dict) else None,
                "verdict_status": verdict.get("status") if verdict else None,
                "trajectory_present": trajectory_path.is_file(),
                "trajectory_path": str(trajectory_path) if trajectory_path.is_file() else None,
                "artifact_manifest_present": artifact_manifest_path.is_file(),
                "content_valid": content_valid,
                "content_errors": content_errors,
                "tokens": token_info,
                "duration_seconds": _duration_seconds(result),
                "result_path": str(result_path),
                "verdict_path": str(verdict_path) if verdict_path.is_file() else None,
                **diagnostic,
            }
        )
    trial_count_mismatch = False
    if expected_trial_count is not None:
        if expected_trial_count < 1:
            raise HarborResultError("expected_trial_count 必须大于 0")
        if len(trial_dirs) != expected_trial_count:
            trial_count_mismatch = True
        missing_count = expected_trial_count - len(trials)
        for index in range(max(0, missing_count)):
            trials.append(
                {
                    "trial_name": f"missing-trial-{index + 1:03d}",
                    "status": "INFRA_ERROR",
                    "reward": None,
                    "verdict_status": None,
                    "trajectory_present": False,
                    "artifact_manifest_present": False,
                    "tokens": {"input": None, "cache": None, "output": None},
                    "duration_seconds": None,
                    "result_path": None,
                    "verdict_path": None,
                    "error_code": "TRIAL_RESULT_MISSING",
                }
            )
    total = len(trials)
    completed = sum(item["status"] in {"PASS", "FAIL", "COMPLETED"} for item in trials)
    passed = sum(item["status"] == "PASS" for item in trials)
    trajectory_count = sum(item["trajectory_present"] for item in trials)
    artifact_manifest_count = sum(item["artifact_manifest_present"] for item in trials)
    durations = [
        item["duration_seconds"] for item in trials if item["duration_seconds"] is not None
    ]
    token_totals = {
        key: sum(item["tokens"][key] for item in trials if isinstance(item["tokens"][key], int))
        for key in ("input", "cache", "output")
    }
    cleanup = _cleanup_ok(root / "_control/ags-sandbox-ledger.jsonl")
    artifact_missing = hermes_artifacts and not search and artifact_manifest_count != total
    trajectory_missing = hermes_artifacts and trajectory_count != total
    content_invalid = any(item.get("content_valid") is False for item in trials)
    return {
        "schema_version": ROLLOUT_RESULTS_SCHEMA,
        "job_dir": str(root),
        "agent_mode": agent_mode,
        **({"domain": "search", "acceptance": "NOT_ASSESSED",
            "execution_completed": bool(total and completed == total and cleanup is True
                                        and not trial_count_mismatch and not content_invalid)}
           if search else {}),
        "trial_count": total,
        "expected_trial_count": expected_trial_count,
        "trials": trials,
        "metrics": {
            "completion_rate": completed / total if total else 0.0,
            "pass_rate": None if search else (passed / total if total else 0.0),
            "trajectory_capture_rate": trajectory_count / total if total else 0.0,
            "artifact_manifest_rate": artifact_manifest_count / total if total else 0.0,
            "cleanup_rate": 1.0 if cleanup is True else 0.0,
            "total_tokens": token_totals,
            "mean_duration_seconds": sum(durations) / len(durations) if durations else None,
        },
        "cleanup": {"ok": cleanup},
        "quality_gate": {
            **({"scope": "EXECUTION"} if search else {}),
            "ok": bool(
                total
                and completed == total
                and cleanup is True
                and not trial_count_mismatch
                and not artifact_missing
                and not trajectory_missing
                and not content_invalid
            ),
            "reasons": [
                reason
                for reason, condition in (
                    ("NO_TRIAL_RESULTS", total == 0),
                    ("TRIAL_COUNT_MISMATCH", trial_count_mismatch),
                    ("TRIAL_INCOMPLETE_OR_INFRA_ERROR", completed != total),
                    ("SANDBOX_CLEANUP_UNCONFIRMED", cleanup is not True),
                    ("ARTIFACT_MANIFEST_MISSING", artifact_missing),
                    ("TRAJECTORY_MISSING", trajectory_missing),
                    ("TRAJECTORY_OR_ARTIFACT_CONTENT_INVALID", content_invalid),
                )
                if condition
            ],
        },
    }


__all__ = [
    "ROLLOUT_RESULTS_SCHEMA",
    "HarborResultError",
    "build_file_artifact_snapshot",
    "validate_file_semantic_receipt",
    "certify_hermes_job",
    "read_rollout_results",
    "read_search_trial",
    "rollout_passed",
]
