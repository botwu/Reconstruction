"""内联与独立 rollout 共用的最终响应合同验收。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from traceforge.harbor_ags.response_receipt import (
    ResponseReceiptError,
    build_response_receipt,
    evaluate_response_contract,
)
from traceforge.reconstruction.environment_bindings import non_file_obligation_ids


def _acceptance_report_obligation_ids(task: dict[str, Any]) -> list[str]:
    """识别需采集报告的义务；关键字命中不代表语义已验证。"""

    non_file = set(non_file_obligation_ids(task))
    ids: list[str] = []
    for obligation in task.get("acceptance_obligations") or []:
        if not isinstance(obligation, dict):
            continue
        obligation_id = obligation.get("id")
        if not isinstance(obligation_id, str) or obligation_id not in non_file:
            continue
        text = str(obligation.get("text") or "").lower()
        if "acceptance-report" in text or "acceptance report" in text:
            ids.append(obligation_id)
    return ids


def _attach_response_receipts(
    rollout: dict[str, Any], expected_trials: int, response_contract: dict[str, Any] | None = None,
    require_acceptance_report: bool = True,
) -> tuple[list[str], list[dict[str, Any]]]:
    """绑定最终响应、执行合同检查并仅保留哈希收据。"""

    results = rollout.get("results")
    trials = results.get("trials") if isinstance(results, dict) else None
    if not isinstance(trials, list):
        return ["RESPONSE_RECEIPT_TRIALS_MISSING"], []
    if expected_trials < 1 or len(trials) != expected_trials:
        return [
            f"RESPONSE_RECEIPT_TRIAL_COUNT_MISMATCH:{len(trials)}:{expected_trials}"
        ], []
    errors: list[str] = []
    receipts: list[dict[str, Any]] = []
    for index, trial in enumerate(trials):
        # 失败或未认证的 trial 保留原诊断，不用响应格式覆盖执行失败。
        if not isinstance(trial, dict):
            errors.append(f"RESPONSE_RECEIPT_TRIAL_INVALID:{index}")
            continue
        trial_status = trial.get("status")
        if trial_status != "PASS":
            trial["response_receipt_status"] = "SKIPPED"
            trial["response_receipt_skip_reason"] = (
                f"TRIAL_STATUS_{trial_status or 'UNKNOWN'}"
            )
            continue
        if trial.get("content_valid") is False:
            trial["response_receipt_status"] = "SKIPPED"
            trial["response_receipt_skip_reason"] = "TRIAL_CONTENT_UNCERTIFIED"
            continue
        path = trial.get("trajectory_path")
        if not isinstance(path, str) or not path:
            errors.append(f"RESPONSE_RECEIPT_TRAJECTORY_MISSING:{index}")
            continue
        try:
            raw = Path(path).read_bytes()
            checks = response_contract.get("checks", []) if isinstance(response_contract, dict) else []
            require_report = (response_contract is None and require_acceptance_report) or any(
                isinstance(check, dict) and check.get("kind") == "acceptance_report"
                for check in (checks if isinstance(checks, list) else [])
            )
            receipt = build_response_receipt(
                raw, require_acceptance_report=require_report,
                validate_report_schema=response_contract is None and require_acceptance_report,
            )
            trial_root = Path(path).parent.parent if Path(path).parent.name == "agent" else None
            evaluation = (
                evaluate_response_contract(raw, response_contract, trial_root=trial_root)
                if response_contract is not None else None
            )
        except (ResponseReceiptError, OSError) as exc:
            errors.append(f"RESPONSE_RECEIPT_INVALID:{index}:{exc}")
            continue
        summary = {
            key: receipt[key]
            for key in (
                "schema_version",
                "trajectory_sha256",
                "assistant_message_index",
                "response_sha256",
                "acceptance_report_sha256",
                "verification_scope",
                "semantic_verified",
            )
        }
        summary["verified_obligation_ids"] = (
            evaluation["verified_obligation_ids"] if evaluation is not None else []
        )
        summary["contract_checks"] = evaluation["checks"] if evaluation is not None else []
        summary["contract_sha256"] = evaluation["contract_sha256"] if evaluation is not None else None
        trial["response_receipt"] = summary
        trial["response_receipt_status"] = "VERIFIED"
        receipts.append(summary)
    return errors, receipts


def apply_response_receipts(
    result: dict[str, Any], rollout: dict[str, Any], task: dict[str, Any], expected_trials: int
) -> None:
    """保存响应格式证据；格式合法不能代替义务内容的验收。"""

    obligation_ids = _acceptance_report_obligation_ids(task)
    response_contract = task.get("response_contract")
    execution = rollout.get("execution")
    if isinstance(execution, dict) and execution.get("status") != "COMPLETED":
        result["status"] = "REVIEW"
        result["sft_eligible"] = False
        return
    errors, receipts = _attach_response_receipts(
        rollout, expected_trials, response_contract, bool(obligation_ids)
    )
    result["response_receipts"] = receipts
    raw_results = rollout.get("results")
    trials = raw_results.get("trials") if isinstance(raw_results, dict) else None
    skipped = [
        {
            "index": index,
            "status": trial.get("status"),
            "reason": trial.get("response_receipt_skip_reason"),
        }
        for index, trial in enumerate(trials if isinstance(trials, list) else [])
        if isinstance(trial, dict) and trial.get("response_receipt_status") == "SKIPPED"
    ]
    if skipped:
        result["response_receipt_skipped"] = skipped
    if errors:
        result["status"] = "REVIEW"
        result["sft_eligible"] = False
        result["errors"] = list(
            dict.fromkeys(
                [*(result.get("errors") or []), *errors, "NON_FILE_RESPONSE_UNVERIFIED"]
            )
        )
        return
    # 回执证明原始响应与报告结构，不能用自报成功清除义务。
    if skipped:
        result["status"] = "REVIEW"
        result["sft_eligible"] = False
        return
    if len(receipts) != expected_trials:
        result["status"] = "REVIEW"
        result["sft_eligible"] = False
        result["errors"] = [*(result.get("errors") or []), "NON_FILE_RESPONSE_UNVERIFIED"]
        return
    verified = set(non_file_obligation_ids(task))
    for receipt in receipts:
        verified.intersection_update(receipt["verified_obligation_ids"])
    result["unverified_obligations"] = [
        item for item in result.get("unverified_obligations") or [] if item not in verified
    ]
    result["pending_response_obligations"] = [
        item for item in result["unverified_obligations"] if item in set(non_file_obligation_ids(task))
    ]
    contract_errors = [
        f"RESPONSE_CONTRACT_UNVERIFIED:{index}:{check['obligation_id']}:{error}"
        for index, receipt in enumerate(receipts)
        for check in receipt["contract_checks"]
        for error in check.get("errors") or []
    ]
    if contract_errors:
        result["status"] = "REVIEW"
        result["sft_eligible"] = False
        result["errors"] = list(dict.fromkeys([*(result.get("errors") or []), *contract_errors]))
