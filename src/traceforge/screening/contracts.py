"""重建筛选的决策、路由与 rubric 契约。"""

from __future__ import annotations

from enum import StrEnum

SELECTION_MANIFEST_SCHEMA = "traceforge.reconstruction-screening-manifest.v1"
SCREENING_RECORD_SCHEMA = "traceforge.reconstruction-screening-record.v1"
SCREENING_CONTRACT_VERSION = "reconstruction-screening-v8"
TRIAGE_PROMPT_VERSION = "reconstruction-screening-triage-v6"
TRIAGE_RESPONSE_SCHEMA = "traceforge.reconstruction-screening-triage.v1"


class ScreeningDecision(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    REVIEW = "REVIEW"
    DEFER = "DEFER"
    REJECT = "REJECT"


class ScreeningRoute(StrEnum):
    RULE_HARD_REJECT = "RULE_HARD_REJECT"
    COST_DEFERRED = "COST_DEFERRED"
    NEEDS_MODEL_TRIAGE = "NEEDS_MODEL_TRIAGE"
    ELIGIBLE_CODE_FILE = "ELIGIBLE_CODE_FILE"
    RETRIEVAL_BACKEND_NOT_READY = "RETRIEVAL_BACKEND_NOT_READY"
    SUCCESS_NOT_RECONSTRUCTED = "SUCCESS_NOT_RECONSTRUCTED"
    RUBRIC_REJECT = "RUBRIC_REJECT"
    RUBRIC_REVIEW = "RUBRIC_REVIEW"
    MODEL_TRIAGE_FAILED = "MODEL_TRIAGE_FAILED"


class DomainRoute(StrEnum):
    CODE_FILE = "code_file"
    RETRIEVAL = "retrieval"
    OTHER = "other"


RUBRIC_KEYS = (
    "task_identifiability",
    "failure_evidence",
)
