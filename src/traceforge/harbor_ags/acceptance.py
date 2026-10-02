"""从冻结计划读取独立 Hermes rollout，并执行共享的后验响应验收。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from traceforge.harbor_ags.response_acceptance import (
    apply_file_semantic_receipts,
    apply_response_receipts,
)
from traceforge.harbor_ags.results import (
    HarborResultError,
    certify_hermes_job,
    read_rollout_results,
    rollout_passed,
)
from traceforge.harbor_ags.rollout import (
    ROLLOUT_RECEIPT_SCHEMA,
    load_verified_rollout_plan,
    validate_rollout_runtime,
)
from traceforge.reconstruction.environment_bindings import non_file_obligation_ids


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborResultError(f"无法读取验收输入：{path}") from exc
    if not isinstance(value, dict):
        raise HarborResultError(f"验收输入必须是对象：{path}")
    return value


def _task_acceptance(task_paths: list[Path]) -> tuple[dict[str, Any], dict[str, str]]:
    """只读取计划哈希已覆盖的隐藏合同；缺失合同输入不能降级成无响应义务。"""

    task: dict[str, Any] | None = None
    hashes: dict[str, str] = {}
    for path in task_paths:
        manifest_path = path / "tests/control/input-manifest.json"
        manifest = _read_json(manifest_path)
        acceptance = manifest.get("task_acceptance")
        if (
            manifest.get("schema_version") != "traceforge.control-input-manifest.v1"
            or not isinstance(acceptance, dict)
            or any(not isinstance(acceptance.get(key), list) for key in (
                "acceptance_obligations", "environment_bindings",
            ))
            or "response_contract" not in acceptance
            or not isinstance(acceptance.get("file_semantic_checks", {}), dict)
        ):
            raise HarborResultError(f"隐藏 task_acceptance 缺失或无效：{manifest_path}")
        if task is not None and task != acceptance:
            raise HarborResultError("同一 rollout 计划的 trials 必须绑定相同任务验收合同")
        task = acceptance
        hashes[str(manifest_path)] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if task is None:
        raise HarborResultError("rollout 计划缺少任务验收输入")
    return task, hashes


def _bind_trial_tasks(job: Path, task_paths: list[Path]) -> dict[str, str]:
    """核对 trial 实际输入属于该计划，且没有借用另一个 trial 的输入槽位。"""

    bindings: dict[str, str] = {}
    seen: set[Path] = set()
    for trial in sorted(path for path in job.iterdir() if path.is_dir() and path.name != "_control"):
        config = _read_json(trial / "config.json")
        task = config.get("task")
        value = task.get("path") if isinstance(task, dict) else None
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise HarborResultError(f"trial 缺少明确的 task.path：{trial}")
        path = Path(value).resolve()
        if path not in task_paths or path in seen:
            raise HarborResultError(f"trial task.path 与计划输入不匹配或重复：{trial}")
        seen.add(path)
        bindings[trial.name] = str(path)
    return bindings


def read_rollout_acceptance(
    plan_dir: Path | str,
    *,
    job_dir: Path | str | None = None,
    agent_mode: str | None = None,
    certification_harbor_root: Path | str | None = None,
) -> dict[str, Any]:
    """核对独立执行输入并验收结果；只更新本地派生认证和结果，不执行 rollout。

    每次读取均重新认证当前字节并重算响应收据，原子替换计划目录的
    rollout_results.json；不修改冻结计划、task workspace 或真实轨迹。
    PASS 仅表示本次 rollout 验收通过，不回写重建状态或声明 SFT/RED 认证。
    可显式使用新认证器复核旧产物；执行运行时仍按原计划核验。
    """

    plan_root = Path(plan_dir).resolve()
    plan = load_verified_rollout_plan(plan_root)
    agent = plan.get("agent")
    if not isinstance(agent, dict) or agent.get("mode") != "hermes":
        raise HarborResultError("完整响应验收仅适用于 Hermes 计划；oracle/nop 使用 --job-dir 读取指标")
    if agent_mode is not None and agent_mode != agent["mode"]:
        raise HarborResultError("agent-mode 与冻结计划不一致")
    expected_trials = agent.get("trials")
    if type(expected_trials) is not int or expected_trials < 1:
        raise HarborResultError("计划 trial 数无效")
    jobs_root, job_name = plan.get("jobs_root"), plan.get("job_name")
    if not isinstance(jobs_root, str) or not isinstance(job_name, str) or not job_name:
        raise HarborResultError("计划缺少 Job 路径")
    expected_job = (Path(jobs_root) / job_name).resolve()
    job = Path(job_dir).resolve() if job_dir is not None else expected_job
    if job != expected_job or not job.is_dir():
        raise HarborResultError(f"Job 目录不存在或未绑定到此计划：{job}")
    receipt_path = plan_root / "run_receipt.json"
    execution = _read_json(receipt_path)
    if (
        execution.get("schema_version") != ROLLOUT_RECEIPT_SCHEMA
        or execution.get("run_id") != plan.get("run_id")
        or execution.get("artifact_manifest_sha256") != hashlib.sha256(
            (plan_root / "artifact_manifest.json").read_bytes()
        ).hexdigest()
    ):
        raise HarborResultError("执行收据未绑定到当前冻结计划")
    dataset = plan["dataset"]
    task_paths = [
        (Path(dataset["dataset_root"]) / relative).resolve()
        for relative in dataset["task_relative_paths"]
    ]
    if len(task_paths) != expected_trials or len(set(task_paths)) != expected_trials:
        raise HarborResultError("计划输入槽位与 trial 数不一致")
    task, acceptance_hashes = _task_acceptance(task_paths)
    completed = (
        execution.get("status") == "COMPLETED"
        and execution.get("external_execution") is True
        and type(execution.get("returncode")) is int and execution["returncode"] == 0
    )
    bindings = _bind_trial_tasks(job, task_paths) if completed else {}
    if completed:
        validate_rollout_runtime(plan)
        certification_sha256 = None
        if certification_harbor_root is not None:
            validator_path = Path(certification_harbor_root) / "src/harbor_ags/validator.py"
            if not validator_path.is_file():
                raise HarborResultError(f"认证器不存在：{validator_path}")
            certification_sha256 = hashlib.sha256(validator_path.read_bytes()).hexdigest()
        certify_hermes_job(job, harbor_root=certification_harbor_root or plan["harbor_root"])
        if certification_sha256 is not None:
            for trial_name in bindings:
                certification = _read_json(job / trial_name / "reconstruction-certification.json")
                if certification.get("validator_source_sha256") != certification_sha256:
                    raise HarborResultError("实际认证器与指定源码不一致；请用指定 Harbor 路径启动独立复核进程")
    report = read_rollout_results(job, agent_mode="hermes", expected_trial_count=expected_trials)
    errors = list(report["quality_gate"]["reasons"])
    if not completed:
        errors.append(f"HARBOR_EXECUTION_NOT_COMPLETED:{execution.get('status', 'UNKNOWN')}")
    if not rollout_passed({"execution": execution, "results": report}, expected_trials):
        errors.append("ROLLOUT_NOT_PASSED")
    acceptance = {
        "status": "READY" if not errors else "REVIEW",
        "errors": errors,
        "unverified_obligations": sorted(
            set(non_file_obligation_ids(task)) | set(task.get("file_semantic_checks", {}))
        ),
    }
    if completed:
        apply_response_receipts(
            acceptance, {"execution": execution, "results": report}, task, expected_trials,
        )
        apply_file_semantic_receipts(
            acceptance, {"execution": execution, "results": report}, task, expected_trials,
        )
    pending = set(acceptance["unverified_obligations"])
    if pending & set(non_file_obligation_ids(task)):
        acceptance["errors"].append("NON_FILE_RESPONSE_UNVERIFIED")
    if pending & set(task.get("file_semantic_checks", {})):
        acceptance["errors"].append("FILE_SEMANTIC_UNVERIFIED")
    acceptance["errors"] = list(dict.fromkeys(acceptance["errors"]))
    acceptance["status"] = (
        "PASS" if acceptance["status"] == "READY" and not acceptance["errors"] else "REVIEW"
    )
    acceptance.pop("sft_eligible", None)
    report["acceptance"] = acceptance
    report["execution"] = {key: execution.get(key) for key in ("status", "external_execution", "returncode")}
    report["input_binding"] = {
        "certification_harbor_root": str(Path(certification_harbor_root or plan["harbor_root"]).resolve()),
        "plan_dir": str(plan_root),
        "plan_sha256": hashlib.sha256((plan_root / "rollout_plan.json").read_bytes()).hexdigest(),
        "run_id": plan["run_id"],
        "run_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "source_bundle": plan.get("source_bundle"),
        "task_acceptance_sha256": acceptance_hashes,
        "trial_task_paths": bindings,
    }
    destination = plan_root / "rollout_results.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return report
