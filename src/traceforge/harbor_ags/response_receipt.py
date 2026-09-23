"""Rollout assistant-response receipt for NON_FILE acceptance contracts.

The receipt is control-plane metadata: it binds the exact trajectory bytes, final
assistant message, and terminal ``acceptance-report`` JSON without writing that
report into the public workspace.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

RESPONSE_RECEIPT_SCHEMA = "traceforge.response-receipt.v1"
_ACCEPTANCE_RE = re.compile(
    r"```acceptance-report\r?\n(?P<body>\{.*?\})\r?\n```\s*\Z",
    re.DOTALL,
)
_ALLOWED_CRITERIA_STATUS = {"satisfied", "not-satisfied", "not-applicable"}
_ALLOWED_COMMAND_RESULT = {"passed", "failed", "not-run"}
_REQUIRED_REPORT_FIELDS = (
    "criteriaSatisfied",
    "changedFiles",
    "testsAddedOrUpdated",
    "commandsRun",
    "validationOutput",
    "residualRisks",
    "noStagedFiles",
    "diffSummary",
    "reviewFindings",
)


class ResponseReceiptError(ValueError):
    """Trajectory or acceptance-report does not satisfy the receipt contract."""


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
        return "".join(chunks)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return ""


def final_assistant_response(trajectory: dict[str, Any]) -> tuple[int, str]:
    """Return the terminal assistant text; reject a tool/user after it."""

    messages = trajectory.get("messages")
    if not isinstance(messages, list):
        raise ResponseReceiptError("TRAJECTORY_MESSAGES_REQUIRED")
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if not isinstance(item, dict) or item.get("role") not in {"assistant", "tool", "user"}:
            continue
        if item.get("role") != "assistant":
            raise ResponseReceiptError("FINAL_ASSISTANT_RESPONSE_NOT_TERMINAL")
        text = _text_content(item.get("content"))
        if not text.strip():
            raise ResponseReceiptError("FINAL_ASSISTANT_RESPONSE_MISSING")
        return index, text
    raise ResponseReceiptError("FINAL_ASSISTANT_RESPONSE_MISSING")


def _string_list(value: Any, field: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ResponseReceiptError(f"INVALID_REPORT_FIELD:{field}")


def parse_acceptance_report(response_text: str) -> tuple[dict[str, Any], str]:
    """Parse exactly one terminal acceptance-report fenced JSON object."""

    if not isinstance(response_text, str) or not response_text.strip():
        raise ResponseReceiptError("EMPTY_ASSISTANT_RESPONSE")
    if response_text.count("```acceptance-report") != 1:
        raise ResponseReceiptError("ACCEPTANCE_REPORT_BLOCK_COUNT")
    match = _ACCEPTANCE_RE.search(response_text)
    if match is None:
        raise ResponseReceiptError("ACCEPTANCE_REPORT_NOT_TERMINAL")
    body = match.group("body")
    try:
        report = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ResponseReceiptError("ACCEPTANCE_REPORT_INVALID_JSON") from exc
    if not isinstance(report, dict):
        raise ResponseReceiptError("ACCEPTANCE_REPORT_MUST_BE_OBJECT")
    for field in _REQUIRED_REPORT_FIELDS:
        if field not in report:
            raise ResponseReceiptError(f"ACCEPTANCE_REPORT_FIELD_REQUIRED:{field}")
    criteria = report["criteriaSatisfied"]
    if (
        not isinstance(criteria, list)
        or not criteria
        or any(not isinstance(item, dict) for item in criteria)
    ):
        raise ResponseReceiptError("INVALID_REPORT_FIELD:criteriaSatisfied")
    criterion_ids: set[str] = set()
    for item in criteria:
        criterion_id = item.get("id")
        if not isinstance(criterion_id, str) or not criterion_id.strip():
            raise ResponseReceiptError("INVALID_CRITERION_ID")
        if criterion_id in criterion_ids:
            raise ResponseReceiptError("DUPLICATE_CRITERION_ID")
        criterion_ids.add(criterion_id)
        if item.get("status") not in _ALLOWED_CRITERIA_STATUS:
            raise ResponseReceiptError("INVALID_CRITERION_STATUS")
        if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            raise ResponseReceiptError("INVALID_CRITERION_EVIDENCE")
    commands = report["commandsRun"]
    if not isinstance(commands, list) or any(not isinstance(item, dict) for item in commands):
        raise ResponseReceiptError("INVALID_REPORT_FIELD:commandsRun")
    for item in commands:
        if (
            not isinstance(item.get("command"), str)
            or item.get("result") not in _ALLOWED_COMMAND_RESULT
        ):
            raise ResponseReceiptError("INVALID_COMMAND_RESULT")
    for field in (
        "changedFiles",
        "testsAddedOrUpdated",
        "validationOutput",
        "residualRisks",
        "reviewFindings",
    ):
        _string_list(report[field], field)
    if not isinstance(report["noStagedFiles"], bool):
        raise ResponseReceiptError("INVALID_REPORT_FIELD:noStagedFiles")
    if not isinstance(report["diffSummary"], str):
        raise ResponseReceiptError("INVALID_REPORT_FIELD:diffSummary")
    return report, body


def build_response_receipt(trajectory_bytes: bytes) -> dict[str, Any]:
    """Build a receipt from exact ``trajectory.full.json`` bytes."""

    try:
        trajectory = json.loads(trajectory_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResponseReceiptError("TRAJECTORY_INVALID_JSON") from exc
    if not isinstance(trajectory, dict):
        raise ResponseReceiptError("TRAJECTORY_MUST_BE_OBJECT")
    index, response = final_assistant_response(trajectory)
    report, report_body = parse_acceptance_report(response)
    return {
        "schema_version": RESPONSE_RECEIPT_SCHEMA,
        "trajectory_sha256": hashlib.sha256(trajectory_bytes).hexdigest(),
        "assistant_message_index": index,
        "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
        "acceptance_report_sha256": hashlib.sha256(report_body.encode("utf-8")).hexdigest(),
        "report": report,
    }


def verify_response_receipt(receipt: dict[str, Any], trajectory_bytes: bytes) -> dict[str, Any]:
    """Rebuild and compare a receipt; return the verified receipt."""

    if not isinstance(receipt, dict) or receipt.get("schema_version") != RESPONSE_RECEIPT_SCHEMA:
        raise ResponseReceiptError("RECEIPT_SCHEMA_INVALID")
    expected = build_response_receipt(trajectory_bytes)
    for field in (
        "trajectory_sha256",
        "assistant_message_index",
        "response_sha256",
        "acceptance_report_sha256",
    ):
        if receipt.get(field) != expected[field]:
            raise ResponseReceiptError(f"RECEIPT_BINDING_MISMATCH:{field}")
    if receipt.get("report") != expected["report"]:
        raise ResponseReceiptError("RECEIPT_REPORT_MISMATCH")
    return expected


def build_response_receipt_from_path(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ResponseReceiptError("TRAJECTORY_READ_FAILED") from exc
    return build_response_receipt(data)


__all__ = [
    "RESPONSE_RECEIPT_SCHEMA",
    "ResponseReceiptError",
    "build_response_receipt",
    "build_response_receipt_from_path",
    "final_assistant_response",
    "parse_acceptance_report",
    "verify_response_receipt",
]
