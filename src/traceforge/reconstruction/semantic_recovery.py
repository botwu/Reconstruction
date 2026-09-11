"""调用模型生成 Task/Environment 候选，并把不确定性传给人工复核。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from traceforge.trajectory.json_codec import stable_id

from .contracts import (
    Decision,
    EnvironmentFileV1,
    EnvironmentRecoveryV1,
    RecoveryEvidenceV1,
    TaskRecoveryV1,
    validate_environment_recovery,
    validate_task_recovery,
)
from .model_gateway import (
    ChatModel,
    ModelCallReceipt,
    ModelGatewayError,
    ModelRequest,
    parse_json_object,
    receipt_for_response,
)


class RecoveryStatus(StrEnum):
    COMPLETE = "COMPLETE"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SemanticRecoveryOutcome:
    status: str
    kind: str
    attempt_ref: str
    source_report_id: str
    candidates: tuple[TaskRecoveryV1 | EnvironmentRecoveryV1, ...]
    open_questions: tuple[str, ...]
    errors: tuple[str, ...]
    model_receipt: ModelCallReceipt | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "kind": self.kind,
            "attempt_ref": self.attempt_ref,
            "source_report_id": self.source_report_id,
            "candidates": [asdict(candidate) for candidate in self.candidates],
            "open_questions": list(self.open_questions),
            "errors": list(self.errors),
            "model_receipt": (
                asdict(self.model_receipt) if self.model_receipt is not None else None
            ),
        }


def _request_id(kind: str, attempt_ref: str, report_id: str) -> str:
    return stable_id(
        "traceforge.semantic-recovery-request-v1",
        {
            "kind": kind,
            "attempt_ref": attempt_ref,
            "source_report_id": report_id,
        },
    )


def _prompt_evidence(evidence: list[dict[str, Any]], *, max_chars: int = 3000) -> list[dict[str, Any]]:
    """压缩上下文窗口，保留可追溯字段，避免完整 payload 使请求被截断。"""
    slim: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        row = {key: item.get(key) for key in ("evidence_ref_id", "role", "event_kind", "sequence_number", "content_sha256")}
        text = item.get("text")
        if isinstance(text, str):
            row["text"] = text[:max_chars]
            row["text_truncated"] = len(text) > max_chars
        slim.append(row)
    return slim


def _prompt(kind: str, report: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    if kind == "task":
        instruction = "重述用户任务和可观察验收条件，只能使用证据支持的事实。"
        shape = (
            "task_title, task_instruction, user_intent, acceptance_obligations, "
            "explicit_constraints, ambiguities, do_not_infer, evidence, confidence, decision"
        )
    else:
        instruction = "补全初始环境；不得臆造文件内容，solution/tests 必须保持 Agent 隐藏。"
        shape = (
            "public_files, hidden_control_files, hidden_verifier_files, dependencies, "
            "runtime_constraints, observed_state_refs, completion_actions, uncertainties, "
            "confidence, decision"
        )
    return (
        "你是可审计的 Agent World 重建器。"
        f"{instruction} 输出 JSON object，包含 candidates 数组（最多 3 个）和 open_questions 数组。"
        f"每个候选包含字段：{shape}。decision 只能是 READY、REVIEW、DEFER、REJECT；"
        "证据只能引用给定 evidence_ref_id。"
        f"\n失败分析报告：{report!r}\n证据索引（已裁剪，原文通过 hash 追溯）：{_prompt_evidence(evidence)!r}"
    )


def _evidence(items: Any, allowed: set[str]) -> tuple[tuple[RecoveryEvidenceV1, ...], list[str]]:
    if not isinstance(items, list):
        return (), ["EVIDENCE_MUST_BE_ARRAY"]
    output: list[RecoveryEvidenceV1] = []
    errors: list[str] = []
    for item in items:
        # 模型常将只含 id 的证据写成字符串；在允许集合内可无损规范化，
        # 仍然要求该 id 来自输入索引，不能借此引入新证据。
        if isinstance(item, str):
            item = {"evidence_ref_id": item, "role": "MODEL_REFERENCED", "source_pointer": ""}
        if not isinstance(item, dict):
            errors.append("EVIDENCE_ITEM_NOT_OBJECT")
            continue
        ref = str(item.get("evidence_ref_id", ""))
        if ref not in allowed:
            errors.append(f"EVIDENCE_REF_UNKNOWN:{ref}")
            continue
        output.append(
            RecoveryEvidenceV1(
                evidence_ref_id=ref,
                role=str(item.get("role", "")),
                source_pointer=str(item.get("source_pointer", "")),
                content_sha256=str(item["content_sha256"]) if item.get("content_sha256") else None,
            )
        )
    return tuple(output), errors


def _failed(
    kind: str,
    attempt_ref: str,
    report_id: str,
    status: RecoveryStatus,
    errors: tuple[str, ...],
    receipt: ModelCallReceipt | None = None,
) -> SemanticRecoveryOutcome:
    return SemanticRecoveryOutcome(
        status.value, kind, attempt_ref, report_id, (), (), errors, receipt
    )


def _files(candidate: dict[str, Any], key: str, visibility: str) -> tuple[EnvironmentFileV1, ...]:
    values = candidate.get(key, [])
    if not isinstance(values, list):
        raise ValueError(f"{key} 必须是数组")
    output: list[EnvironmentFileV1] = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError(f"{key} 包含非对象元素")
        output.append(
            EnvironmentFileV1(
                path=str(item.get("path", "")),
                visibility=visibility,
                content_sha256=None,
                size_bytes=None,
                origin=str(item.get("origin", "observed")),
                required_before_attempt=bool(item.get("required_before_attempt", True)),
                redaction_status="NOT_APPLIED",
            )
        )
    return tuple(output)


def _confidence_value(value: Any) -> float:
    """规范化模型常见的 low/medium/high 标签；未知值仍保持拒绝。"""
    if isinstance(value, str):
        labels = {"low": 0.3, "medium": 0.6, "high": 0.9}
        if value.strip().lower() in labels:
            return labels[value.strip().lower()]
    return float(value)


def canonicalize_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """统一 M4 evidence_id 与重建阶段 evidence_ref_id 的字段名。"""
    if not isinstance(evidence, list):
        raise TypeError("evidence 必须是 array")
    output: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict):
            output.append(item)
            continue
        value = dict(item)
        if not value.get("evidence_ref_id") and isinstance(value.get("evidence_id"), str):
            value["evidence_ref_id"] = value["evidence_id"]
        if not value.get("source_id") and isinstance(value.get("event_id"), str):
            value["source_id"] = value["event_id"]
        output.append(value)
    return output


def recover(
    *,
    kind: str,
    attempt_ref: str,
    source_report_id: str,
    report: dict[str, Any],
    evidence: list[dict[str, Any]],
    model: ChatModel,
    model_name: str = "claude-opus-4-8",
) -> SemanticRecoveryOutcome:
    """执行一次恢复；模型输出不完整时不会构造伪造候选。"""
    if kind not in {"task", "environment"}:
        raise ValueError("kind 必须是 task 或 environment")
    evidence = canonicalize_evidence(evidence)
    allowed = {str(item.get("evidence_ref_id")) for item in evidence if item.get("evidence_ref_id")}
    request = ModelRequest(
        request_id=_request_id(kind, attempt_ref, source_report_id),
        model=model_name,
        system="证据不足时使用 REVIEW，并列出 open_questions。只返回 JSON。",
        prompt=_prompt(kind, report, evidence),
        response_schema=f"traceforge.{kind}-recovery-candidates.v1",
    )
    try:
        response = model.complete(request)
        receipt = receipt_for_response(response)
        payload = parse_json_object(response.text)
    except ModelGatewayError as exc:
        status = RecoveryStatus.BLOCKED if exc.code == "API_KEY_MISSING" else RecoveryStatus.FAILED
        return _failed(kind, attempt_ref, source_report_id, status, (exc.code,))
    candidates = payload.get("candidates")
    questions = tuple(x for x in payload.get("open_questions", []) if isinstance(x, str))
    if not isinstance(candidates, list) or not candidates:
        return _failed(
            kind, attempt_ref, source_report_id, RecoveryStatus.REVIEW, ("NO_CANDIDATE",), receipt
        )
    if len(candidates) > 3:
        return _failed(
            kind,
            attempt_ref,
            source_report_id,
            RecoveryStatus.REVIEW,
            ("CANDIDATE_LIMIT_EXCEEDED",),
            receipt,
        )
    parsed: list[TaskRecoveryV1 | EnvironmentRecoveryV1] = []
    errors: list[str] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            errors.append(f"CANDIDATE_NOT_OBJECT:{index}")
            continue
        refs, ref_errors = _evidence(candidate.get("evidence", []), allowed)
        errors.extend(f"candidate_{index}:{error}" for error in ref_errors)
        if ref_errors:
            continue
        try:
            identity = {"attempt": attempt_ref, "report": source_report_id, "index": index}
            decision = str(candidate.get("decision", Decision.REVIEW.value))
            if kind == "task":
                value = TaskRecoveryV1(
                    "traceforge.task-recovery.v1",
                    stable_id("traceforge.task-recovery-candidate-v1", identity),
                    attempt_ref,
                    source_report_id,
                    str(candidate.get("task_title", "")),
                    str(candidate.get("task_instruction", "")),
                    str(candidate.get("user_intent", "")),
                    tuple(
                        x
                        for x in candidate.get("acceptance_obligations", [])
                        if isinstance(x, dict)
                    ),
                    tuple(
                        x for x in candidate.get("explicit_constraints", []) if isinstance(x, str)
                    ),
                    tuple(x for x in candidate.get("ambiguities", []) if isinstance(x, str)),
                    tuple(x for x in candidate.get("do_not_infer", []) if isinstance(x, str)),
                    refs,
                    _confidence_value(candidate.get("confidence", 0.0)),
                    decision,
                )
                validate_task_recovery(value)
            else:
                value = EnvironmentRecoveryV1(
                    "traceforge.environment-recovery.v1",
                    stable_id("traceforge.environment-recovery-candidate-v1", identity),
                    attempt_ref,
                    source_report_id,
                    _files(candidate, "public_files", "PUBLIC_WORKSPACE"),
                    _files(candidate, "hidden_control_files", "HIDDEN_CONTROL"),
                    _files(candidate, "hidden_verifier_files", "HIDDEN_VERIFIER"),
                    tuple(x for x in candidate.get("dependencies", []) if isinstance(x, dict)),
                    tuple(
                        x for x in candidate.get("runtime_constraints", []) if isinstance(x, str)
                    ),
                    tuple(
                        x for x in candidate.get("observed_state_refs", []) if isinstance(x, str)
                    ),
                    tuple(x for x in candidate.get("completion_actions", []) if isinstance(x, str)),
                    tuple(x for x in candidate.get("uncertainties", []) if isinstance(x, str)),
                    _confidence_value(candidate.get("confidence", 0.0)),
                    decision,
                )
                validate_environment_recovery(value)
            parsed.append(value)
        except (TypeError, ValueError) as exc:
            errors.append(f"candidate_{index}:INVALID:{exc}")
    if errors or not parsed:
        return SemanticRecoveryOutcome(
            RecoveryStatus.REVIEW.value,
            kind,
            attempt_ref,
            source_report_id,
            tuple(parsed),
            questions,
            tuple(errors),
            receipt,
        )
    status = (
        RecoveryStatus.COMPLETE
        if all(x.decision == Decision.READY.value for x in parsed)
        else RecoveryStatus.REVIEW
    )
    return SemanticRecoveryOutcome(
        status.value, kind, attempt_ref, source_report_id, tuple(parsed), questions, (), receipt
    )


def recovery_metrics(outcomes: list[SemanticRecoveryOutcome]) -> dict[str, float | int]:
    total = len(outcomes)
    return {
        "sample_count": total,
        "candidate_count": sum(len(x.candidates) for x in outcomes),
        "complete_rate": sum(x.status == RecoveryStatus.COMPLETE.value for x in outcomes) / total
        if total
        else 0.0,
        "review_rate": sum(x.status == RecoveryStatus.REVIEW.value for x in outcomes) / total
        if total
        else 0.0,
    }


__all__ = [
    "RecoveryStatus",
    "SemanticRecoveryOutcome",
    "canonicalize_evidence",
    "recover",
    "recovery_metrics",
]
