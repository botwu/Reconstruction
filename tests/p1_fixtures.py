from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.session import execute_tool
from traceforge.reconstruction.session_spans import build_spans


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
