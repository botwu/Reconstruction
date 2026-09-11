"""Terminal-Universe verifier agent 的受控生成—执行—反馈循环。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .synthesis import VerifierCandidate, synthesize_verifier


class VerifierExecutor(Protocol):
    def run(self, candidate: VerifierCandidate) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class VerifierIterationResult:
    candidate: VerifierCandidate | None
    status: str
    attempts: tuple[dict[str, Any], ...]
    open_questions: tuple[str, ...]


def synthesize_verifier_iterative(
    *,
    task: dict[str, Any],
    workspace_files: dict[str, str],
    model: Any,
    executor: VerifierExecutor,
    model_name: str = "claude-opus-4-8",
    max_rounds: int = 2,
) -> VerifierIterationResult:
    if max_rounds < 1 or max_rounds > 3:
        raise ValueError("max_rounds 必须在 1 到 3 之间")
    working = dict(task)
    attempts: list[dict[str, Any]] = []
    candidate = None
    questions: list[str] = []
    for index in range(max_rounds):
        candidate, audit = synthesize_verifier(
            task=working, workspace_files=workspace_files, model=model, model_name=model_name
        )
        if candidate is None:
            attempts.append(
                {
                    "round": index + 1,
                    "status": "REVIEW",
                    "feedback": audit.get("open_questions", []),
                }
            )
            questions.extend(str(x) for x in audit.get("open_questions", []) if isinstance(x, str))
            return VerifierIterationResult(None, "REVIEW", tuple(attempts), tuple(questions))
        execution = executor.run(candidate)
        if not isinstance(execution, dict):
            raise TypeError("VerifierExecutor 必须返回 object")
        outcome = str(execution.get("status", "INFRA_ERROR"))
        attempts.append(
            {"round": index + 1, "status": outcome, "feedback": execution.get("feedback", "")}
        )
        if outcome == "PASS":
            return VerifierIterationResult(candidate, "READY", tuple(attempts), tuple(questions))
        if outcome not in {"FAIL", "INFRA_ERROR"}:
            raise ValueError("executor status 必须为 PASS/FAIL/INFRA_ERROR")
        working["_verifier_feedback"] = str(execution.get("feedback", "未提供执行反馈"))
    return VerifierIterationResult(
        candidate,
        "REVIEW",
        tuple(attempts),
        tuple([*questions, "verifier 在限定轮数内未通过执行校验"]),
    )
