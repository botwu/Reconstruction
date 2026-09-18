from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.session import execute_tool
from traceforge.screening.contracts import TRIAGE_PROMPT_VERSION
from traceforge.screening.observable import build_spans


class ToolCitingIntentRuntime:
    """用 list/read 工具按稳定 id 取证，再引用这些 id。"""

    backend = "hermes-sandbox"
    model_name = "test-model"

    def __init__(self) -> None:
        self.session = None
        self.listed: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []

    def run(self, *, role, instruction, session, output_root):
        del role, output_root
        self.session = session
        self.listed = json.loads(execute_tool("list_user_texts", {}, session))
        self.reads = []
        for item in self.listed:
            by_id = execute_tool("read_user_text", {"id": item["id"]}, session)
            by_index = execute_tool(
                "read_user_text", {"index": item["message_index"]}, session
            )
            self.reads.append(
                {"id": item["id"], "by_id": by_id, "by_index": by_index}
            )
        tag = {}
        for line in instruction.splitlines():
            if line.startswith("TASK_TAG="):
                tag = json.loads(line.split("=", 1)[1])
        texts = [item["by_id"] for item in self.reads if not str(item["by_id"]).startswith("error:")]
        instruction_text = "；".join(text[:240] for text in texts) or "完成用户任务"
        payload = {
            "task_id": tag.get("task_id"),
            "task_instruction": instruction_text,
            "core_objective": instruction_text[:200],
            "acceptance_obligations": [
                {
                    "id": "obl-001",
                    "text": instruction_text[:200],
                    "evidence_ref_ids": [item["id"] for item in self.listed],
                }
            ],
            "success_criteria": [instruction_text[:200]],
            "specified_output_format": None,
            "has_examples": False,
            "mandatory_constraints": [],
            "prohibitions": [],
        }
        return AgentResult(
            role="intent",
            backend=self.backend,
            payload=payload,
            final_text=json.dumps(payload, ensure_ascii=False),
            completed=True,
        )


def wrap_v9_code_file_record(v9: dict[str, Any], raw_line: str) -> dict[str, Any]:
    """把已确认的 v9 code_file ELIGIBLE 补成 v10 合同字段，不改入选判断。"""

    triage = v9.get("triage") if isinstance(v9.get("triage"), dict) else {}
    selected = [str(x) for x in (triage.get("selected_span_ids") or [])]
    if not selected:
        tasks = triage.get("tasks") or []
        if tasks and isinstance(tasks[0], dict):
            selected = [str(x) for x in (tasks[0].get("span_ids") or [])]
    payload = json.loads(raw_line)
    spans, _ = build_spans(payload["messages"])
    span_map = {span.span_id: span for span in spans}
    missing = [sid for sid in selected if sid not in span_map]
    if missing:
        raise ValueError(f"v9 selected_span_ids 对不上 raw session: {missing}")
    message_indices: list[int] = []
    for sid in selected:
        span = span_map[sid]
        message_indices.extend(range(span.message_start, span.message_end))
    task_id = "task_" + hashlib.sha256("|".join(selected).encode("utf-8")).hexdigest()[:20]
    domain = str(triage.get("domain_route") or "code_file")
    return {
        "decision": "ELIGIBLE",
        "route": "ELIGIBLE_CODE_FILE" if domain == "code_file" else v9.get("route"),
        "source_ref": v9.get("source_ref"),
        "line_number": v9.get("line_number"),
        "line_sha256": v9.get("line_sha256")
        or hashlib.sha256(raw_line.encode("utf-8")).hexdigest(),
        "triage": {
            "prompt_version": TRIAGE_PROMPT_VERSION,
            "label_status": "COMPLETE",
            "tasks": [
                {
                    "task_id": task_id,
                    "span_ids": selected,
                    "domain_route": domain,
                    "outcome": triage.get("outcome") or "INCOMPLETE",
                    "is_actionable": True,
                    "needs_reconstruction": True,
                    "reconstruction_eligible": True,
                    "eligibility": {"decision": "ELIGIBLE", "blocking_reason_codes": []},
                    "evidence_refs": {
                        "span_ids": selected,
                        "message_indices": sorted(set(message_indices)),
                    },
                }
            ],
            "relations": [],
            "selected_task_ids": [task_id],
            "selected_span_ids": selected,
        },
    }


def load_jsonl_row(path: Path, line_number: int) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        if isinstance(row, dict) and row.get("line_number") == line_number:
            return row
    return None
