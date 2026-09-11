"""M1B reader for deterministic Failure Analysis (先校验，再消费)。"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from traceforge.trajectory.json_codec import StrictJsonError, sha256_bytes, strict_json_loads
from traceforge.trajectory.validation import validate_compiled_run

class FailureAnalysisInputError(RuntimeError):
    pass

@dataclass(frozen=True, slots=True)
class EventFact:
    event_id: str
    capture_id: str
    event_kind: str
    scope: str
    sequence_number: int

@dataclass(frozen=True, slots=True)
class PairingFact:
    pairing_id: str
    capture_id: str
    call_event_ids: tuple[str, ...]
    result_event_ids: tuple[str, ...]
    matched_call_event_id: str | None
    matched_result_event_id: str | None
    statuses: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class CaptureFact:
    capture_id: str
    terminal_status: str
    reason_codes: tuple[str, ...]
    missing_result_count: int
    event_count: int

@dataclass(frozen=True, slots=True)
class FailureInputView:
    m1b_run_id: str
    m1b_manifest_sha256: str
    source_schema: str
    events_by_capture: dict[str, tuple[EventFact, ...]]
    pairings_by_capture: dict[str, tuple[PairingFact, ...]]
    captures: tuple[CaptureFact, ...]

def _iter(root: Path, rel: str) -> Iterator[dict[str, Any]]:
    path = root / rel
    if not path.is_file():
        raise FailureAnalysisInputError(f"输入 M1B 缺少必需表：{rel}")
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise FailureAnalysisInputError(f"无法读取 M1B 表：{rel}") from exc
    for raw in lines:
        try:
            value = strict_json_loads(raw)
        except StrictJsonError as exc:
            raise FailureAnalysisInputError(f"M1B 表无法解析：{rel}") from exc
        if not isinstance(value, dict):
            raise FailureAnalysisInputError(f"M1B 表记录不是对象：{rel}")
        yield value

def _str(value: Any, field: str, rel: str) -> str:
    if not isinstance(value, str):
        raise FailureAnalysisInputError(f"字段类型非法：{rel}/{field}")
    return value

def _int(value: Any, field: str, rel: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FailureAnalysisInputError(f"字段类型非法：{rel}/{field}")
    return value

def _tuple_str(value: Any, field: str, rel: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise FailureAnalysisInputError(f"字段类型非法：{rel}/{field}")
    return tuple(_str(item, field, rel) for item in value)

def load_failure_input(m1b_run_dir: str | Path) -> FailureInputView:
    root = Path(m1b_run_dir)
    if not root.is_dir():
        raise FailureAnalysisInputError("输入 M1B run 不是目录")
    result = validate_compiled_run(root)
    if not result.ok:
        codes = ",".join(sorted({issue.code for issue in result.issues}))
        raise FailureAnalysisInputError(f"输入 M1B run 未通过完整性校验：{codes}")
    manifest_path = root / "artifact_manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = strict_json_loads(manifest_bytes)
    except (OSError, StrictJsonError) as exc:
        raise FailureAnalysisInputError("无法读取 M1B manifest") from exc
    if not isinstance(manifest, dict):
        raise FailureAnalysisInputError("M1B manifest 不是对象")
    m1b_run_id = _str(manifest.get("run_id"), "run_id", "artifact_manifest.json")
    if root.name != m1b_run_id:
        raise FailureAnalysisInputError("M1B run 目录名与 run_id 不一致")
    source_schema = _str(manifest.get("source_schema"), "source_schema", "artifact_manifest.json")
    events: dict[str, list[EventFact]] = defaultdict(list)
    for row in _iter(root, "private/event_occurrences.jsonl"):
        cid = _str(row.get("capture_occurrence_id"), "capture_occurrence_id", "event_occurrences")
        events[cid].append(EventFact(
            event_id=_str(row.get("event_occurrence_id"), "event_occurrence_id", "event_occurrences"),
            capture_id=cid,
            event_kind=_str(row.get("event_kind"), "event_kind", "event_occurrences"),
            scope=_str(row.get("event_scope"), "event_scope", "event_occurrences"),
            sequence_number=_int(row.get("sequence_number"), "sequence_number", "event_occurrences"),
        ))
    pairings: dict[str, list[PairingFact]] = defaultdict(list)
    for row in _iter(root, "private/tool_pairings.jsonl"):
        cid = _str(row.get("capture_occurrence_id"), "capture_occurrence_id", "tool_pairings")
        pairings[cid].append(PairingFact(
            pairing_id=_str(row.get("pairing_id"), "pairing_id", "tool_pairings"),
            capture_id=cid,
            call_event_ids=_tuple_str(row.get("call_event_ids"), "call_event_ids", "tool_pairings"),
            result_event_ids=_tuple_str(row.get("result_event_ids"), "result_event_ids", "tool_pairings"),
            matched_call_event_id=row.get("matched_call_event_id") if isinstance(row.get("matched_call_event_id"), str) else None,
            matched_result_event_id=row.get("matched_result_event_id") if isinstance(row.get("matched_result_event_id"), str) else None,
            statuses=_tuple_str(row.get("statuses"), "statuses", "tool_pairings"),
        ))
    captures: list[CaptureFact] = []
    for row in _iter(root, "private/capture_quality.jsonl"):
        cid = row.get("capture_occurrence_id")
        if cid is None:
            continue
        cid = _str(cid, "capture_occurrence_id", "capture_quality")
        captures.append(CaptureFact(
            capture_id=cid,
            terminal_status=_str(row.get("terminal_status"), "terminal_status", "capture_quality"),
            reason_codes=_tuple_str(row.get("reason_codes"), "reason_codes", "capture_quality"),
            missing_result_count=_int(row.get("missing_result_count"), "missing_result_count", "capture_quality"),
            event_count=_int(row.get("event_count"), "event_count", "capture_quality"),
        ))
    return FailureInputView(
        m1b_run_id=m1b_run_id,
        m1b_manifest_sha256=sha256_bytes(manifest_bytes),
        source_schema=source_schema,
        events_by_capture={key: tuple(sorted(value, key=lambda item: item.sequence_number)) for key, value in events.items()},
        pairings_by_capture={key: tuple(sorted(value, key=lambda item: item.pairing_id)) for key, value in pairings.items()},
        captures=tuple(sorted(captures, key=lambda item: item.capture_id)),
    )
