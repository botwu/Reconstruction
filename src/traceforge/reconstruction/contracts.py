"""Stable contracts for reconstruction -> Harbor -> rollout -> SFT.

This module is intentionally model/runtime agnostic.  It carries references and
checksums rather than raw source logs so each downstream stage can be rerun and
 audited independently.
"""

from __future__ import annotations

import hashlib
import math
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from traceforge.trajectory.contracts import SerializableContract
from traceforge.trajectory.json_codec import stable_id

RECONSTRUCTION_CONTRACT_VERSION = "reconstruction-pipeline-v1"
TASK_RECOVERY_SCHEMA = "traceforge.task-recovery.v1"
ENVIRONMENT_RECOVERY_SCHEMA = "traceforge.environment-recovery.v1"
HARBOR_BUNDLE_MANIFEST_SCHEMA = "traceforge.harbor-bundle-manifest.v1"
ROLLOUT_REQUEST_SCHEMA = "traceforge.rollout-request.v1"
ROLLOUT_TRIAL_SCHEMA = "traceforge.rollout-trial.v1"
VERIFICATION_RESULT_SCHEMA = "traceforge.verification-result.v1"
SFT_CANDIDATE_SCHEMA = "traceforge.sft-candidate.v1"


class Decision(StrEnum):
    READY = "READY"
    REVIEW = "REVIEW"
    DEFER = "DEFER"
    REJECT = "REJECT"


class Visibility(StrEnum):
    PUBLIC_WORKSPACE = "PUBLIC_WORKSPACE"
    HIDDEN_CONTROL = "HIDDEN_CONTROL"
    HIDDEN_VERIFIER = "HIDDEN_VERIFIER"
    METADATA = "METADATA"


class RolloutStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    INFRA_ERROR = "INFRA_ERROR"
    CANCELLED = "CANCELLED"


class VerificationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    INFRA_ERROR = "INFRA_ERROR"


class SFTEligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class RecoveryEvidenceV1(SerializableContract):
    evidence_ref_id: str
    role: str
    source_pointer: str
    content_sha256: str | None


@dataclass(frozen=True, slots=True)
class TaskRecoveryV1(SerializableContract):
    schema_version: str
    recovery_id: str
    attempt_ref: str
    source_report_id: str
    task_title: str
    task_instruction: str
    user_intent: str
    acceptance_obligations: tuple[dict[str, Any], ...]
    explicit_constraints: tuple[str, ...]
    ambiguities: tuple[str, ...]
    do_not_infer: tuple[str, ...]
    evidence: tuple[RecoveryEvidenceV1, ...]
    confidence: float
    decision: str


@dataclass(frozen=True, slots=True)
class EnvironmentFileV1(SerializableContract):
    path: str
    visibility: str
    content_sha256: str | None
    size_bytes: int | None
    origin: str
    required_before_attempt: bool
    redaction_status: str


@dataclass(frozen=True, slots=True)
class EnvironmentRecoveryV1(SerializableContract):
    schema_version: str
    recovery_id: str
    attempt_ref: str
    source_report_id: str
    public_files: tuple[EnvironmentFileV1, ...]
    hidden_control_files: tuple[EnvironmentFileV1, ...]
    hidden_verifier_files: tuple[EnvironmentFileV1, ...]
    dependencies: tuple[dict[str, Any], ...]
    runtime_constraints: tuple[str, ...]
    observed_state_refs: tuple[str, ...]
    completion_actions: tuple[str, ...]
    uncertainties: tuple[str, ...]
    confidence: float
    decision: str


@dataclass(frozen=True, slots=True)
class HarborBundleManifestV1(SerializableContract):
    schema_version: str
    bundle_id: str
    bundle_root: str
    task_name: str
    harbor_schema_version: str
    task_recovery_id: str
    environment_recovery_id: str
    source_attempt_ref: str
    public_paths: tuple[str, ...]
    hidden_control_paths: tuple[str, ...]
    hidden_verifier_paths: tuple[str, ...]
    file_sha256: dict[str, str]
    verifier_obligation_ids: tuple[str, ...]
    compiler_status: str
    validation_status: str
    bundle_sha256: str


@dataclass(frozen=True, slots=True)
class RolloutRequestV1(SerializableContract):
    schema_version: str
    rollout_id: str
    bundle_id: str
    agent_name: str
    agent_model: str
    provider: str
    n_trials: int
    temperature: float
    timeout_seconds: int
    seeds: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RolloutTrialV1(SerializableContract):
    schema_version: str
    rollout_id: str
    trial_id: str
    bundle_id: str
    status: str
    reward: float | None
    trajectory_artifact: str | None
    verifier_result_ref: str | None
    error_code: str | None
    duration_seconds: float | None


@dataclass(frozen=True, slots=True)
class VerificationResultV1(SerializableContract):
    schema_version: str
    verification_id: str
    bundle_id: str
    trial_id: str
    status: str
    reward: float | None
    criterion_scores: dict[str, float]
    obligation_results: tuple[dict[str, Any], ...]
    anti_shortcut_results: tuple[dict[str, Any], ...]
    evidence_refs: tuple[str, ...]
    verifier_sha256: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class SFTCandidateV1(SerializableContract):
    schema_version: str
    candidate_id: str
    bundle_id: str
    rollout_id: str
    trial_id: str
    eligibility: str
    verifier_status: str
    task_recovery_confidence: float
    environment_recovery_confidence: float
    trajectory_quality: float
    selection_reasons: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    trajectory_artifact: str | None


def recovery_id(*, attempt_ref: str, source_report_id: str, kind: str) -> str:
    return stable_id(
        "reconstruction-recovery-v1",
        {
            "version": RECONSTRUCTION_CONTRACT_VERSION,
            "attempt_ref": attempt_ref,
            "source_report_id": source_report_id,
            "kind": kind,
        },
    )


def rollout_id(*, bundle_id: str, agent_model: str, n_trials: int, seeds: tuple[int, ...]) -> str:
    return stable_id(
        "reconstruction-rollout-v1",
        {
            "version": RECONSTRUCTION_CONTRACT_VERSION,
            "bundle_id": bundle_id,
            "agent_model": agent_model,
            "n_trials": n_trials,
            "seeds": seeds,
        },
    )


def _confidence(value: float, name: str = "confidence") -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"{name} must be finite and in [0,1]")


def _safe_rel(path: str) -> str:
    p = PurePosixPath(path)
    if not path or p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe relative path: {path!r}")
    return p.as_posix()


def validate_task_recovery(value: TaskRecoveryV1) -> None:
    if (
        value.schema_version != TASK_RECOVERY_SCHEMA
        or not value.recovery_id
        or not value.attempt_ref
    ):
        raise ValueError("invalid TaskRecovery schema or identity")
    if not value.task_instruction.strip() or not value.user_intent.strip():
        raise ValueError("task instruction and intent must be non-empty")
    _confidence(value.confidence)
    Decision(value.decision)
    if any(not item.strip() for item in value.do_not_infer):
        raise ValueError("do_not_infer entries must be non-empty")


def validate_environment_recovery(value: EnvironmentRecoveryV1) -> None:
    if (
        value.schema_version != ENVIRONMENT_RECOVERY_SCHEMA
        or not value.recovery_id
        or not value.attempt_ref
    ):
        raise ValueError("invalid EnvironmentRecovery schema or identity")
    _confidence(value.confidence)
    Decision(value.decision)
    seen: set[str] = set()
    for item in (*value.public_files, *value.hidden_control_files, *value.hidden_verifier_files):
        path = _safe_rel(item.path)
        if path in seen:
            raise ValueError(f"duplicate environment path: {path}")
        seen.add(path)
        Visibility(item.visibility)
        if item.size_bytes is not None and (
            isinstance(item.size_bytes, bool) or item.size_bytes < 0
        ):
            raise ValueError("size_bytes must be non-negative")


def validate_rollout_request(value: RolloutRequestV1) -> None:
    if (
        value.schema_version != ROLLOUT_REQUEST_SCHEMA
        or value.n_trials < 1
        or len(value.seeds) != value.n_trials
    ):
        raise ValueError("invalid rollout request")
    if value.timeout_seconds < 1 or value.temperature < 0 or not math.isfinite(value.temperature):
        raise ValueError("invalid rollout limits")


def validate_verification_result(value: VerificationResultV1) -> None:
    if value.schema_version != VERIFICATION_RESULT_SCHEMA:
        raise ValueError("invalid verification schema")
    VerificationStatus(value.status)
    if value.reward is not None and (not math.isfinite(value.reward) or not 0 <= value.reward <= 1):
        raise ValueError("reward must be in [0,1]")
    for key, score in value.criterion_scores.items():
        if (
            not isinstance(key, str)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0 <= float(score) <= 1
        ):
            raise ValueError("criterion scores must be in [0,1]")


def validate_sft_candidate(value: SFTCandidateV1) -> None:
    if value.schema_version != SFT_CANDIDATE_SCHEMA:
        raise ValueError("invalid SFT candidate schema")
    SFTEligibility(value.eligibility)
    VerificationStatus(value.verifier_status)
    for name in (
        "task_recovery_confidence",
        "environment_recovery_confidence",
        "trajectory_quality",
    ):
        _confidence(getattr(value, name), name)


def harbor_bundle_manifest(
    *,
    bundle_root: str | Path,
    bundle_id: str,
    task_name: str,
    task_recovery_id: str,
    environment_recovery_id: str,
    source_attempt_ref: str,
    verifier_obligation_ids: tuple[str, ...] = (),
) -> HarborBundleManifestV1:
    """Create a deterministic manifest for the Harbor v1.4 directory contract.

    Public workspace/environment are agent-visible; tests/control and grader are
    hidden.  This function never copies files or executes a verifier.
    """
    root = Path(bundle_root).resolve()
    if not root.is_dir():
        raise ValueError(f"bundle root is not a directory: {root}")
    required = ("task.toml", "instruction.md", "workspace", "environment", "solution", "tests")
    if any(not (root / item).exists() for item in required):
        raise ValueError("bundle root does not satisfy Harbor required entries")
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise ValueError(f"unsupported bundle file: {path}")
        rel = path.relative_to(root).as_posix()
        _safe_rel(rel)
        hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    public = tuple(
        sorted(
            p
            for p in hashes
            if p == "task.toml"
            or p == "instruction.md"
            or p.startswith("workspace/")
            or p.startswith("environment/")
            or p.startswith("solution/")
        )
    )
    control = tuple(sorted(p for p in hashes if p.startswith("tests/control/")))
    verifier = tuple(sorted(p for p in hashes if p.startswith("tests/") and p not in control))
    tree = hashlib.sha256(
        "".join(f"{p}:{hashes[p]}\\n" for p in sorted(hashes)).encode()
    ).hexdigest()
    return HarborBundleManifestV1(
        HARBOR_BUNDLE_MANIFEST_SCHEMA,
        bundle_id,
        root.as_posix(),
        task_name,
        "1.4",
        task_recovery_id,
        environment_recovery_id,
        source_attempt_ref,
        public,
        control,
        verifier,
        hashes,
        verifier_obligation_ids,
        "COMPILED",
        "NOT_RUN",
        tree,
    )


# Orchestration-level references. Kept separate from model/runtime contracts above.
PIPELINE_MANIFEST_SCHEMA = "traceforge.reconstruction-pipeline-manifest.v1"
SELECTION_MANIFEST_SCHEMA = "traceforge.reconstruction-selection-manifest.v1"
EXECUTION_PLAN_SCHEMA = "traceforge.reconstruction-execution-plan.v1"
PIPELINE_RUN_RECEIPT_SCHEMA = "traceforge.reconstruction-pipeline-run-receipt.v1"
RECONSTRUCTION_NODE_SCHEMA = "traceforge.reconstruction-node.v1"
RECONSTRUCTION_CANDIDATE_SCHEMA = "traceforge.reconstruction-candidate.v1"


class NodeStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PENDING_MODEL = "PENDING_MODEL"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class CandidateDecision(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    REVIEW = "REVIEW"
    DEFER = "DEFER"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class PipelineNodeV1(SerializableContract):
    schema_version: str
    node_id: str
    node_type: str
    status: str
    input_refs: tuple[str, ...]
    output_refs: tuple[str, ...]
    blocking_reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconstructionCandidateV1(SerializableContract):
    schema_version: str
    candidate_id: str
    report_id: str
    session_ref: str
    episode_ref: str
    attempt_ref: str
    decision: str
    route: str
    primary_failure: str
    failure_layer: str
    recoverability: str
    confidence: float
    evidence_ref_ids: tuple[str, ...]
    reconstruction_targets: tuple[str, ...]
    blocking_reason_codes: tuple[str, ...]


def pipeline_run_id(
    *,
    m1b_run_id: str,
    m1b_manifest_sha256: str,
    m1d_run_id: str | None,
    m4_run_id: str,
    policy_version: str = RECONSTRUCTION_CONTRACT_VERSION,
) -> str:
    return stable_id(
        "reconstruction-pipeline-run-v1",
        {
            "version": RECONSTRUCTION_CONTRACT_VERSION,
            "policy_version": policy_version,
            "m1b_run_id": m1b_run_id,
            "m1b_manifest_sha256": m1b_manifest_sha256,
            "m1d_run_id": m1d_run_id,
            "m4_run_id": m4_run_id,
        },
    )


__all__ = [
    name
    for name in globals()
    if (
        (name.isupper() and not name.startswith("_"))
        or name.endswith("V1")
        or name.endswith("Status")
        or name.endswith("Eligibility")
        or name.endswith("Decision")
        or name
        in {
            "recovery_id",
            "rollout_id",
            "pipeline_run_id",
            "harbor_bundle_manifest",
            "validate_task_recovery",
            "validate_environment_recovery",
            "validate_rollout_request",
            "validate_verification_result",
            "validate_sft_candidate",
        }
    )
]
