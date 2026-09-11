"""根据独立验证结果筛选可用于 SFT 的 rollout。"""

from __future__ import annotations

from dataclasses import dataclass
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
    return float(value)


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
        reasons.extend([
            "VERIFIER_PASS",
            "REWARD_PASS",
            "CONFIDENCE_PASS",
            "LEAKAGE_CHECK_PASS",
            "REPRODUCIBLE",
        ])

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


__all__ = ["CurationInputError", "CurationThresholds", "curate_candidate", "curate_candidates"]
