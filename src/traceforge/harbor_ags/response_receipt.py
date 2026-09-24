"""Rollout assistant-response receipt for NON_FILE acceptance contracts.

The receipt is control-plane metadata: it binds the exact trajectory bytes, final
assistant message, and terminal ``acceptance-report`` JSON without writing that
report into the public workspace.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
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


def build_response_receipt(
    trajectory_bytes: bytes, *, require_acceptance_report: bool = True
) -> dict[str, Any]:
    """Build a receipt from exact ``trajectory.full.json`` bytes."""

    try:
        trajectory = json.loads(trajectory_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResponseReceiptError("TRAJECTORY_INVALID_JSON") from exc
    if not isinstance(trajectory, dict):
        raise ResponseReceiptError("TRAJECTORY_MUST_BE_OBJECT")
    index, response = final_assistant_response(trajectory)
    report, report_body = (
        parse_acceptance_report(response)
        if require_acceptance_report or chr(96) * 3 + "acceptance-report" in response
        else (None, None)
    )
    return {
        "schema_version": RESPONSE_RECEIPT_SCHEMA,
        "trajectory_sha256": hashlib.sha256(trajectory_bytes).hexdigest(),
        "assistant_message_index": index,
        "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
        "acceptance_report_sha256": (
            hashlib.sha256(report_body.encode("utf-8")).hexdigest() if report_body is not None else None
        ),
        "report": report,
        "verification_scope": "REPORT_STRUCTURE_ONLY" if report is not None else "FINAL_RESPONSE_BINDING_ONLY",
        "semantic_verified": False,
    }


def verify_response_receipt(receipt: dict[str, Any], trajectory_bytes: bytes) -> dict[str, Any]:
    """Rebuild and compare a receipt; return the verified receipt."""

    if not isinstance(receipt, dict) or receipt.get("schema_version") != RESPONSE_RECEIPT_SCHEMA:
        raise ResponseReceiptError("RECEIPT_SCHEMA_INVALID")
    expected = build_response_receipt(
        trajectory_bytes, require_acceptance_report=receipt.get("report") is not None
    )
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


def build_response_receipt_from_path(
    path: str | Path, *, require_acceptance_report: bool = True
) -> dict[str, Any]:
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ResponseReceiptError("TRAJECTORY_READ_FAILED") from exc
    return build_response_receipt(data, require_acceptance_report=require_acceptance_report)


RESPONSE_CONTRACT_SCHEMA = "traceforge.response-contract.v1"


def _check_acceptance_report(response: str, check: dict[str, Any]) -> dict[str, Any]:
    """检查用户声明的字段和编号，不将自报状态当作事实证明。"""

    report, _ = parse_acceptance_report(response)
    fields = check.get("required_fields")
    expected_ids = check.get("criterion_ids")
    if (
        not isinstance(fields, dict) or not fields
        or any(not isinstance(key, str) or not key for key in fields)
        or not isinstance(expected_ids, list) or not expected_ids
        or any(not isinstance(value, str) or not value for value in expected_ids)
        or len(set(expected_ids)) != len(expected_ids)
    ):
        raise ResponseReceiptError("RESPONSE_CONTRACT_REPORT_INVALID")
    types = {
        "array": list, "object": dict, "string": str, "boolean": bool,
        "integer": int, "number": (int, float), "null": type(None),
    }
    for field, kind in fields.items():
        if not isinstance(kind, str) or kind not in types:
            raise ResponseReceiptError(f"RESPONSE_CONTRACT_FIELD_TYPE_UNSUPPORTED:{field}")
        if field not in report or not isinstance(report[field], types[kind]):
            raise ResponseReceiptError(f"RESPONSE_CONTRACT_FIELD_MISMATCH:{field}")
        if kind in {"integer", "number"} and isinstance(report[field], bool):
            raise ResponseReceiptError(f"RESPONSE_CONTRACT_FIELD_MISMATCH:{field}")
    if {item["id"] for item in report["criteriaSatisfied"]} != set(expected_ids):
        raise ResponseReceiptError("RESPONSE_CONTRACT_CRITERIA_MISMATCH")
    return {"verification_scope": "REPORT_STRUCTURE_ONLY"}


def _summary_values(
    text: str, verdicts: list[str], levels: list[str]
) -> tuple[str, dict[str, int], str]:
    """只解析契约中的结论枚举与带标签的计数，不猜测未声明内容。"""

    verdict_pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(value) for value in verdicts) + r")(?!\w)")
    found = set(verdict_pattern.findall(text))
    if len(found) != 1:
        raise ResponseReceiptError("RESPONSE_SUMMARY_VERDICT_AMBIGUOUS")
    rest = verdict_pattern.sub("", text)
    counts: dict[str, int] = {}
    for level in levels:
        label = re.escape(level)
        pattern = re.compile(
            rf"(?<!\w){label}\s*(?:[:=|]\s*|\(\s*)(\d+)\)?(?!\w)"
            rf"|(?<!\w)(\d+)\s+{label}(?!\w)", re.IGNORECASE,
        )
        values = {int(match.group(1) or match.group(2)) for match in pattern.finditer(rest)}
        if len(values) != 1:
            raise ResponseReceiptError(f"RESPONSE_SUMMARY_COUNT_AMBIGUOUS:{level}")
        counts[level] = values.pop()
        rest = pattern.sub("", rest)
    return found.pop(), counts, rest


def _bound_report(trial_root: Path | None, report_path: str) -> tuple[str, str]:
    """只读取同一 trial 下经控制端 manifest 绑定的最终 workspace 文件。"""

    relative = PurePosixPath(report_path)
    if (
        trial_root is None or relative.is_absolute() or ".." in relative.parts
        or str(relative) != report_path or "\\" in report_path or not relative.parts
    ):
        raise ResponseReceiptError("RESPONSE_REPORT_PATH_INVALID")
    artifact_root = trial_root / "artifacts"
    manifest_path = artifact_root / "manifest.json"
    mirrored = PurePosixPath("logs/artifacts/traceforge/workspace") / relative
    target = artifact_root / mirrored
    for parent in (manifest_path, target, *target.parents):
        if parent.is_symlink():
            raise ResponseReceiptError("RESPONSE_REPORT_PATH_UNSAFE")
        if parent == trial_root:
            break
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw = target.read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResponseReceiptError("RESPONSE_REPORT_ARTIFACT_UNAVAILABLE") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "traceforge-harbor-artifacts/v1":
        raise ResponseReceiptError("RESPONSE_REPORT_MANIFEST_INVALID")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ResponseReceiptError("RESPONSE_REPORT_MANIFEST_INVALID")
    entries = [item for item in files if isinstance(item, dict) and item.get("path") == str(mirrored)]
    digest = hashlib.sha256(raw).hexdigest()
    if len(entries) != 1 or entries[0].get("sha256") != digest or entries[0].get("size_bytes") != len(raw):
        raise ResponseReceiptError("RESPONSE_REPORT_ARTIFACT_MISMATCH")
    try:
        return raw.decode("utf-8"), digest
    except UnicodeError as exc:
        raise ResponseReceiptError("RESPONSE_REPORT_NOT_TEXT") from exc


def _check_basic_summary(
    response: str, check: dict[str, Any], trial_root: Path | None
) -> dict[str, Any]:
    """核对简要回复与真实文件产物的结论、计数和路径一致性。"""

    verdicts, levels = check.get("verdicts"), check.get("finding_levels")
    report_path = check.get("report_path")
    if (
        not isinstance(verdicts, list) or not verdicts
        or not isinstance(levels, list) or not levels
        or any(not isinstance(value, str) or not value for value in [*verdicts, *levels])
        or len(set(verdicts)) != len(verdicts) or len(set(levels)) != len(levels)
        or not isinstance(report_path, str) or not report_path
        or check.get("match_report") is not True
    ):
        raise ResponseReceiptError("RESPONSE_CONTRACT_SUMMARY_INVALID")
    report, digest = _bound_report(trial_root, report_path)
    block = _ACCEPTANCE_RE.search(response)
    summary = response[:block.start()] if block else response
    path_pattern = re.compile(r"(?:/home/user/workspace/)?" + re.escape(report_path))
    if len(path_pattern.findall(summary)) != 1:
        raise ResponseReceiptError("RESPONSE_SUMMARY_REPORT_PATH_MISMATCH")
    summary = path_pattern.sub("", summary)
    verdict, counts, rest = _summary_values(summary, verdicts, levels)
    rest = re.sub(r"\b(?:verdict|finding counts|findings|report path|report)\b", "", rest, flags=re.IGNORECASE)
    if re.sub(r"[\s\x60*_#|:;,()\-]", "", rest):
        raise ResponseReceiptError("RESPONSE_SUMMARY_EXTRA_CONTENT")
    actual_verdict, actual_counts, _ = _summary_values(report, verdicts, levels)
    if (verdict, counts) != (actual_verdict, actual_counts):
        raise ResponseReceiptError("RESPONSE_SUMMARY_REPORT_MISMATCH")
    return {
        "verification_scope": "REPORT_CONSISTENCY_ONLY",
        "report_path": report_path, "report_sha256": digest,
        "verdict": verdict, "finding_counts": counts,
    }


def evaluate_response_contract(
    trajectory_bytes: bytes, contract: dict[str, Any], *, trial_root: Path | None = None
) -> dict[str, Any]:
    """用明确机器契约后验最终响应；未支持的检查保持未验证。"""

    if not isinstance(contract, dict) or contract.get("schema_version") != RESPONSE_CONTRACT_SCHEMA:
        raise ResponseReceiptError("RESPONSE_CONTRACT_SCHEMA_INVALID")
    checks = contract.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ResponseReceiptError("RESPONSE_CONTRACT_CHECKS_REQUIRED")
    ids: set[str] = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("obligation_id"), str) or not check["obligation_id"]:
            raise ResponseReceiptError("RESPONSE_CONTRACT_OBLIGATION_INVALID")
        if check["obligation_id"] in ids:
            raise ResponseReceiptError("RESPONSE_CONTRACT_OBLIGATION_DUPLICATE")
        ids.add(check["obligation_id"])
    try:
        trajectory = json.loads(trajectory_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResponseReceiptError("TRAJECTORY_INVALID_JSON") from exc
    if not isinstance(trajectory, dict):
        raise ResponseReceiptError("TRAJECTORY_MUST_BE_OBJECT")
    _, response = final_assistant_response(trajectory)
    outcomes: list[dict[str, Any]] = []
    for check in checks:
        row: dict[str, Any] = {"obligation_id": check["obligation_id"], "kind": check.get("kind")}
        try:
            if check.get("kind") == "acceptance_report":
                evidence = _check_acceptance_report(response, check)
            elif check.get("kind") == "basic_summary":
                evidence = _check_basic_summary(response, check, trial_root)
            else:
                raise ResponseReceiptError("RESPONSE_CONTRACT_CHECK_UNSUPPORTED")
            row.update(status="PASS", errors=[], **evidence)
        except ResponseReceiptError as exc:
            row.update(status="REVIEW", errors=[str(exc)])
        outcomes.append(row)
    return {
        "schema_version": RESPONSE_CONTRACT_SCHEMA,
        "contract_sha256": hashlib.sha256(
            json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "trajectory_sha256": hashlib.sha256(trajectory_bytes).hexdigest(),
        "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
        "checks": outcomes,
        "verified_obligation_ids": [item["obligation_id"] for item in outcomes if item["status"] == "PASS"],
        "semantic_verified": False,
    }


__all__ = [
    "RESPONSE_RECEIPT_SCHEMA",
    "RESPONSE_CONTRACT_SCHEMA",
    "evaluate_response_contract",
    "ResponseReceiptError",
    "build_response_receipt",
    "build_response_receipt_from_path",
    "final_assistant_response",
    "parse_acceptance_report",
    "verify_response_receipt",
]
