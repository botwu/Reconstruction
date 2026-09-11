"""Strict contracts for AgentRx-style trajectory diagnosis."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CheckType(StrEnum):
    PYTHON = "python_check"
    NL = "nl_check"


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCLEAR = "UNCLEAR"
    NOT_RUN = "NOT_RUN"
    NEEDS_SANDBOX = "NEEDS_SANDBOX"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class TrajectoryEvent:
    event_id: str
    step_index: int
    role: str
    content: str
    evidence_ref_ids: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TrajectoryIR:
    trajectory_id: str
    instruction: str
    events: tuple[TrajectoryEvent, ...]


@dataclass(frozen=True, slots=True)
class Invariant:
    invariant_id: str
    kind: str  # STATIC or DYNAMIC
    check_type: str
    criterion: str
    trigger_step: int | None = None
    code: str | None = None
    evidence_ref_ids: tuple[str, ...] = ()
    source: str = "AgentRx"
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class CheckResult:
    invariant_id: str
    check_type: str
    status: str
    trigger_step: int | None
    evidence_ref_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class StageReceipt:
    stage: str
    status: str
    request_id: str | None = None
    model_receipt: dict[str, Any] | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRxReport:
    schema_version: str
    trajectory_id: str
    static_invariants: tuple[Invariant, ...]
    dynamic_invariants_by_prefix: tuple[tuple[Invariant, ...], ...]
    checks: tuple[CheckResult, ...]
    root_cause: dict[str, Any]
    coverage: dict[str, Any]
    receipts: tuple[StageReceipt, ...]
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trajectory_id": self.trajectory_id,
            "static_invariants": [asdict(x) for x in self.static_invariants],
            "dynamic_invariants_by_prefix": [
                [asdict(x) for x in xs] for xs in self.dynamic_invariants_by_prefix
            ],
            "checks": [asdict(x) for x in self.checks],
            "root_cause": self.root_cause,
            "coverage": self.coverage,
            "receipts": [asdict(x) for x in self.receipts],
            "errors": list(self.errors),
        }
