"""重建筛选的决策、路由与 rubric 契约。"""

from __future__ import annotations

from enum import StrEnum

SELECTION_MANIFEST_SCHEMA = "traceforge.reconstruction-screening-manifest.v2"
SCREENING_RECORD_SCHEMA = "traceforge.reconstruction-screening-record.v2"
# Frozen 2026-09-15 after R01-50 DeepSeek calibration. Do not bump without a new calibration.
SCREENING_CONTRACT_VERSION = "reconstruction-screening-v10"
TRIAGE_PROMPT_VERSION = "reconstruction-screening-triage-v10"
TRIAGE_RESPONSE_SCHEMA = "traceforge.reconstruction-screening-triage.v2"

TASK_OUTCOMES = ("FAILURE", "INCOMPLETE", "SUCCESS", "UNCERTAIN")
RELATION_TYPES = ("continuation", "correction", "dependency")
TASK_LABEL_SCHEMA = "traceforge.reconstruction-task-label.v1"


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
    ELIGIBLE_TASK = "ELIGIBLE_TASK"
    RETRIEVAL_BACKEND_NOT_READY = "RETRIEVAL_BACKEND_NOT_READY"
    SUCCESS_NOT_RECONSTRUCTED = "SUCCESS_NOT_RECONSTRUCTED"
    RUBRIC_REJECT = "RUBRIC_REJECT"
    RUBRIC_REVIEW = "RUBRIC_REVIEW"
    MODEL_TRIAGE_FAILED = "MODEL_TRIAGE_FAILED"


class DomainRoute(StrEnum):
    TERMINAL = "terminal"
    # Backward-compatible alias emitted by frozen screening records.
    CODE_FILE = "code_file"
    RETRIEVAL = "retrieval"
    OTHER = "other"


RUBRIC_KEYS = (
    "task_identifiability",
    "failure_evidence",
)

# Frozen tag vocabulary. Screening derives these; reconstruct selects and prompts by them.
OUTCOME_TAGS = {
    "FAILURE": "failure",
    "INCOMPLETE": "incomplete",
    "SUCCESS": "success",
    "UNCERTAIN": "uncertain",
}
SELECTED_TASK_TAGS = ("selected_for_reconstruction", "rubric_pass")
TASK_TAG_VOCABULARY = (
    "actionable",
    "non_actionable",
    "failure",
    "incomplete",
    "success",
    "uncertain",
    "outcome_unknown",
    "needs_reconstruction",
    "no_reconstruction_needed",
    "selected_for_reconstruction",
    "rubric_pass",
    "rubric_reject",
    "rubric_review",
    "superseded_by_later_success",
)
SESSION_TAG_VOCABULARY = (
    "multiple_tasks",
    "contains_non_actionable",
    "contains_unfinished_task",
    "contains_success_task",
    "contains_reconstruction_candidate",
    "needs_reconstruction",
    "has_continuation",
    "has_correction",
    "has_dependency",
    "screening_eligible",
    "screening_review",
    "screening_defer",
    "screening_reject",
)

# session_tags 是整条 capture 的索引/提示，不是第二套入选尺。
SESSION_TAG_ROLE = {
    "admits_session": False,
    "selects_task": False,
    "routes_terminal_file": False,
    "used_for": (
        "index",
        "intent_context",
        "audit",
    ),
}
