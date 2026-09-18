"""单测用的 Hermes AIAgent 替身。生产路径仍构造真实 run_agent.AIAgent。"""

from __future__ import annotations

import json
from typing import Any


def tagged_record(raw_line: str, *, selected: list[int] | None = None) -> dict[str, Any]:
    """带完整 v10 标签的固定筛选 fixture，不经过模型重新判定。"""
    from traceforge.screening.contracts import TRIAGE_PROMPT_VERSION
    from traceforge.screening.observable import build_spans

    payload = json.loads(raw_line)
    spans, _ = build_spans(payload["messages"])
    selected_indices = set([0] if selected is None else selected)
    tasks = [
        {
            "task_id": f"fixture-task-{index}",
            "span_ids": [span.span_id],
            "is_actionable": True,
            "outcome": "INCOMPLETE" if index in selected_indices else "SUCCESS",
            "needs_reconstruction": index in selected_indices,
            "reconstruction_eligible": index in selected_indices,
            "eligibility": {
                "decision": "ELIGIBLE" if index in selected_indices else "REJECT",
                "blocking_reason_codes": [] if index in selected_indices else ["OUTCOME_SUCCESS"],
            },
            "tags": [],
            "evidence_refs": {
                "span_ids": [span.span_id],
                "message_indices": list(range(span.message_start, span.message_end)),
            },
        }
        for index, span in enumerate(spans)
    ]
    from traceforge.screening.task_labels import apply_task_tags, build_session_tags

    for task in tasks:
        apply_task_tags(task)
    session_tags = build_session_tags(tasks=tasks, relations=[], decision="ELIGIBLE")
    return {
        "decision": "ELIGIBLE",
        "route": "ELIGIBLE_CODE_FILE",
        "source_ref": "jsonl:4:abcd",
        "line_number": 4,
        "line_sha256": "unused",
        "triage": {
            "prompt_version": TRIAGE_PROMPT_VERSION,
            "label_status": "COMPLETE",
            "tasks": tasks,
            "relations": [],
            "session_tags": session_tags,
            "selected_task_ids": [task["task_id"] for task in tasks if task["reconstruction_eligible"]],
            "selected_span_ids": [spans[index].span_id for index in sorted(selected_indices)],
        },
    }


class FakeHermesAgent:
    def __init__(
        self,
        *,
        intent_ok: bool = True,
        completion: dict | None = None,
        sufficiency: dict | None = None,
        **kwargs: Any,
    ) -> None:
        self.intent_ok = intent_ok
        self.completion = completion
        self.sufficiency = sufficiency
        self.kwargs = kwargs

    def run_conversation(self, instruction: str, system_message=None, task_id=None):
        if task_id == "intent":
            records = []
            tag = {}
            for line in instruction.splitlines():
                if line.startswith("TASK_TAG="):
                    tag = json.loads(line.split("=", 1)[1])
                if line.startswith("TASK_USER_MESSAGES="):
                    records = json.loads(line.split("=", 1)[1])
            requested = "；".join(record["text"] for record in records)
            requested = requested or "把 foo.py 里的入口函数读出来，不要改文件。"
            refs = [record["id"] for record in records] or ["user:0"]
            payload = {
                "task_id": tag.get("task_id"),
                "task_instruction": requested if self.intent_ok else "",
                "core_objective": requested if self.intent_ok else "",
                "acceptance_obligations": (
                    [
                        {
                            "id": "obl-001",
                            "text": requested,
                            "evidence_ref_ids": refs,
                        }
                    ]
                    if self.intent_ok
                    else []
                ),
                "success_criteria": ([requested] if self.intent_ok else []),
                "specified_output_format": None,
                "has_examples": False,
                "mandatory_constraints": ["不要改文件"] if "不要改文件" in requested else [],
                "prohibitions": [],
            }
        elif task_id == "completion":
            payload = self.completion or {
                "candidates": [
                    {
                        "files": [],
                        "dependencies": [],
                        "runtime_constraints": [],
                        "uncertainties": [],
                        "decision": "READY",
                    }
                ],
                "open_questions": [],
            }
        elif task_id == "verifier":
            payload = {
                "status": "READY",
                "test_outputs_py": (
                    "def test_missing(): assert False\n"
                    "def test_protective(): assert True\n"
                    "def test_output(): assert True\n"
                ),
                "oracle_solutions": [
                    {"name": "oracle-a", "script": "echo a", "justification": "a"},
                    {"name": "oracle-b", "script": "echo b", "justification": "b"},
                ],
                "mutation_solutions": [
                    {"name": "mutation", "script": "echo m", "justification": "m"}
                ],
                "missing_capability_tests": ["test_missing"],
                "protective_tests": ["test_protective"],
                "obligation_coverage": {"obl-001": ["test_output"]},
                "expected_value_strategy": "independent calculation",
                "open_questions": [],
            }
        else:
            if task_id == "sufficiency" and hasattr(self, "_invoke_tool"):
                self._invoke_tool("list_dir", {"path": "."}, task_id)
                files = self._invoke_tool("list_dir", {"path": "."}, task_id)
                if isinstance(files, str) and files.startswith("error:"):
                    return {
                        "final_response": json.dumps({"label": "UNKNOWN", "decision": "REVIEW"}),
                        "completed": True,
                        "messages": [],
                        "api_calls": 1,
                    }
            payload = self.sufficiency or {
                "label": "SUFFICIENT",
                "reason": "replayed foo.py is enough context",
                "missing_context": [],
                "confidence": 0.8,
                "decision": "READY",
            }
        return {
            "final_response": json.dumps(payload, ensure_ascii=False),
            "completed": True,
            "messages": [{"role": "assistant", "content": "ok"}],
            "api_calls": 1,
        }


class FakeHermesFactory:
    def __init__(
        self,
        *,
        intent_ok: bool = True,
        completion: dict | None = None,
        sufficiency: dict | None = None,
    ) -> None:
        self.intent_ok = intent_ok
        self.completion = completion
        self.sufficiency = sufficiency
        self.last_kwargs: dict[str, Any] = {}
        self.last_agent: FakeHermesAgent | None = None

    def __call__(self, **kwargs: Any) -> FakeHermesAgent:
        self.last_kwargs = kwargs
        self.last_agent = FakeHermesAgent(
            intent_ok=self.intent_ok,
            completion=self.completion,
            sufficiency=self.sufficiency,
            **kwargs,
        )
        return self.last_agent
