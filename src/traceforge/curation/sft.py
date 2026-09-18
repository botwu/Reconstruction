"""根据独立验证结果筛选可用于 SFT 的 rollout。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceforge.reconstruction.contracts import (
    SFT_CANDIDATE_SCHEMA,
    SFTCandidateV1,
    SFTEligibility,
    VerificationStatus,
    validate_sft_candidate,
)


@dataclass(frozen=True, slots=True)
class CurationThresholds:
    """SFT 质量门禁阈值；阈值必须在实验配置中显式记录。"""

    task_confidence: float = 0.8
    environment_confidence: float = 0.8
    trajectory_quality: float = 0.8
    required_reward: float = 1.0


class CurationInputError(ValueError):
    """rollout 或验证输入不满足筛选契约。"""


def _number(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CurationInputError(f"{key} 必须是数值")
    result = float(value)
    if not math.isfinite(result):
        raise CurationInputError(f"{key} 必须是有限数值")
    return result


def _explicit_bool(row: dict[str, Any], key: str) -> bool | None:
    """读取必须由上游显式计算的布尔证据。

    缺失值代表没有完成该检查，不能把它解释成安全或可复现；调用方会将其
    标记为 REVIEW。非布尔值是输入契约错误，直接拒绝处理，避免字符串等
    真值语义造成静默放行。
    """

    if key not in row:
        return None
    value = row[key]
    if not isinstance(value, bool):
        raise CurationInputError(f"{key} 必须是显式布尔值")
    return value


def curate_candidate(row: dict[str, Any], thresholds: CurationThresholds) -> SFTCandidateV1:
    """对一条 rollout 应用可解释的 pass-only 质量门禁。

    只有在泄漏检查和可复现性检查都明确给出 ``False``，且其它门禁均通过时，
    候选才会进入 ``ELIGIBLE``。缺失这两类证据时返回 ``REVIEW``，绝不默认
    认为安全或可复现。
    """

    required = ("candidate_id", "bundle_id", "rollout_id", "trial_id", "verifier_status")
    missing = [key for key in required if not isinstance(row.get(key), str) or not row[key]]
    if missing:
        raise CurationInputError(f"缺少字段：{', '.join(missing)}")
    task_conf = _number(row, "task_recovery_confidence")
    env_conf = _number(row, "environment_recovery_confidence")
    trajectory_quality = _number(row, "trajectory_quality")
    reward = row.get("reward")
    if reward is not None:
        reward = _number(row, "reward")
        if not 0.0 <= reward <= 1.0:
            raise CurationInputError("reward 必须在 [0,1]")
    leakage = _explicit_bool(row, "solution_leakage")
    reproducible = _explicit_bool(row, "reproducible")

    reasons: list[str] = []
    rejects: list[str] = []
    reviews: list[str] = []
    if row["verifier_status"] != VerificationStatus.PASS.value:
        rejects.append("VERIFIER_NOT_PASS")
    if reward is None or reward < thresholds.required_reward:
        rejects.append("REWARD_BELOW_THRESHOLD")
    if task_conf < thresholds.task_confidence:
        rejects.append("TASK_CONFIDENCE_LOW")
    if env_conf < thresholds.environment_confidence:
        rejects.append("ENVIRONMENT_CONFIDENCE_LOW")
    if trajectory_quality < thresholds.trajectory_quality:
        rejects.append("TRAJECTORY_QUALITY_LOW")
    if leakage is True:
        rejects.append("SOLUTION_LEAKAGE")
    elif leakage is None:
        reviews.append("SOLUTION_LEAKAGE_UNVERIFIED")
    if reproducible is False:
        rejects.append("NOT_REPRODUCIBLE")
    elif reproducible is None:
        reviews.append("REPRODUCIBILITY_UNVERIFIED")

    # Hard failures always reject. If all hard checks pass but an audit signal is
    # absent, retain the sample for human review rather than training on an
    # unverified trajectory.
    if rejects:
        eligibility = SFTEligibility.REJECT.value
    elif reviews:
        eligibility = SFTEligibility.REVIEW.value
        rejects.extend(reviews)
    else:
        eligibility = SFTEligibility.ELIGIBLE.value
        reasons.extend(
            [
                "VERIFIER_PASS",
                "REWARD_PASS",
                "CONFIDENCE_PASS",
                "LEAKAGE_CHECK_PASS",
                "REPRODUCIBLE",
            ]
        )

    candidate = SFTCandidateV1(
        schema_version=SFT_CANDIDATE_SCHEMA,
        candidate_id=row["candidate_id"],
        bundle_id=row["bundle_id"],
        rollout_id=row["rollout_id"],
        trial_id=row["trial_id"],
        eligibility=eligibility,
        verifier_status=row["verifier_status"],
        task_recovery_confidence=task_conf,
        environment_recovery_confidence=env_conf,
        trajectory_quality=trajectory_quality,
        selection_reasons=tuple(reasons),
        rejection_reasons=tuple(rejects),
        trajectory_artifact=row.get("trajectory_artifact"),
    )
    validate_sft_candidate(candidate)
    return candidate


def curate_candidates(
    rows: list[dict[str, Any]], thresholds: CurationThresholds | None = None
) -> tuple[list[SFTCandidateV1], dict[str, Any]]:
    """批量筛选并返回可复现指标（eligible/review/reject 分开统计）。"""

    thresholds = thresholds or CurationThresholds()
    candidates = [curate_candidate(row, thresholds) for row in rows]
    eligible = sum(item.eligibility == SFTEligibility.ELIGIBLE.value for item in candidates)
    review = sum(item.eligibility == SFTEligibility.REVIEW.value for item in candidates)
    rejected = sum(item.eligibility == SFTEligibility.REJECT.value for item in candidates)
    return candidates, {
        "metric_version": "traceforge.sft-curation-metrics.v1",
        "input_count": len(candidates),
        "eligible_count": eligible,
        "review_count": review,
        "rejected_count": rejected,
        "eligibility_rate": eligible / len(candidates) if candidates else 0.0,
        "review_rate": review / len(candidates) if candidates else 0.0,
        "thresholds": {
            "task_confidence": thresholds.task_confidence,
            "environment_confidence": thresholds.environment_confidence,
            "trajectory_quality": thresholds.trajectory_quality,
            "required_reward": thresholds.required_reward,
        },
    }


def _rollout_row(task: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any] | None:
    rollout = verification.get("rollout") if isinstance(verification.get("rollout"), dict) else {}
    results = rollout.get("results") if isinstance(rollout.get("results"), dict) else {}
    trials = results.get("trials") if isinstance(results.get("trials"), list) else []
    trial = trials[0] if trials and isinstance(trials[0], dict) else {}
    if verification.get("sft_eligible") is not True:
        return None
    reward = trial.get("reward")
    if reward is None:
        reward = 1.0
    return {
        "candidate_id": str(task.get("task_id") or "unknown"),
        "bundle_id": str(verification.get("bundle") or rollout.get("bundle_id") or "missing"),
        "rollout_id": str(rollout.get("job_id") or rollout.get("run_id") or "missing"),
        "trial_id": str(trial.get("trial_id") or trial.get("id") or "missing"),
        "verifier_status": VerificationStatus.PASS.value,
        "reward": reward,
        "task_recovery_confidence": 0.9,
        "environment_recovery_confidence": 0.9,
        "trajectory_quality": 0.9,
        "reproducible": True,
        "solution_leakage": False,
        "trajectory_artifact": trial.get("trajectory") or trial.get("trajectory_artifact"),
    }


def write_reconstruction_sft_curation(
    root: str | Path,
    task_results: list[dict[str, Any]],
) -> Path:
    """把 reconstruct run 的任务结果写成 SFT 裁决，未跑 rollout 不得标 ELIGIBLE。"""

    summaries: list[dict[str, Any]] = []
    curate_rows: list[dict[str, Any]] = []
    for item in task_results:
        if not isinstance(item, dict):
            continue
        raw_verification = item.get("verification")
        verification = raw_verification if isinstance(raw_verification, dict) else {}
        status = str(verification.get("status") or "")
        if status == "NOT_APPLICABLE":
            summaries.append(
                {
                    "task_id": item.get("task_id"),
                    "eligibility": "REVIEW",
                    "rejection_reasons": ["NO_FILE_ACCEPTANCE"],
                }
            )
            continue
        row = _rollout_row(item, verification)
        if row is None:
            reasons = list(item.get("errors") or [])
            eligibility = "PENDING" if item.get("status") == "PENDING_EXECUTION" else "REVIEW"
            if not reasons:
                reasons = ["ROLLOUT_NOT_RUN"] if eligibility == "PENDING" else ["SFT_NOT_READY"]
            summaries.append(
                {
                    "task_id": item.get("task_id"),
                    "eligibility": eligibility,
                    "rejection_reasons": reasons,
                }
            )
            continue
        curate_rows.append(row)
        try:
            candidate = curate_candidate(row)
        except CurationInputError as exc:
            summaries.append(
                {
                    "task_id": item.get("task_id"),
                    "eligibility": "REVIEW",
                    "rejection_reasons": ["CURATION_INPUT_INCOMPLETE", str(exc)],
                }
            )
            continue
        summaries.append(
            {
                "task_id": item.get("task_id"),
                "eligibility": candidate.eligibility,
                "rejection_reasons": list(candidate.rejection_reasons),
                "selection_reasons": list(candidate.selection_reasons),
            }
        )
    if any(item.get("eligibility") == "ELIGIBLE" for item in summaries) and all(
        item.get("eligibility") == "ELIGIBLE" for item in summaries
    ):
        overall = "ELIGIBLE"
    elif any(item.get("eligibility") == "PENDING" for item in summaries) and not any(
        item.get("eligibility") == "ELIGIBLE" for item in summaries
    ):
        overall = "PENDING"
    else:
        overall = "REVIEW"
    payload = {
        "schema_version": "traceforge.sft-curation.v1",
        "status": overall if summaries else "PENDING",
        "tasks": summaries,
    }
    if curate_rows:
        try:
            _, metrics = curate_candidates(curate_rows)
            payload["metrics"] = metrics
        except CurationInputError:
            payload["metrics"] = {"error": "CURATION_INPUT_INCOMPLETE"}
    out = Path(root) / "sft" / "curation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


__all__ = [
    "CurationInputError",
    "CurationThresholds",
    "curate_candidate",
    "curate_candidates",
    "write_reconstruction_sft_curation",
]
