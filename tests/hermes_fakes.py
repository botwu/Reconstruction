"""单测用的 Hermes AIAgent 替身。生产路径仍构造真实 run_agent.AIAgent。"""

from __future__ import annotations

import contextlib
import json
from typing import Any


def raw_source(raw_line: str, *, selected: list[int] | None = None,
               groups: list[list[int]] | None = None, task_ids: list[str] | None = None,
               domain: str = "terminal") -> dict[str, Any]:
    """固定任务分组通过真实原始会话入口，保留完整会话和上下文。"""
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace
    from traceforge.reconstruction.raw_session import build_raw_session_source
    from traceforge.reconstruction.session_spans import build_spans

    spans, _ = build_spans(json.loads(raw_line)["messages"])
    groups = groups if groups is not None else [[i] for i in ([0] if selected is None else selected)]
    assigned = {i for group in groups for i in group}
    payload = {
        "tasks": [{"span_ids": [spans[i].span_id for i in group],
                   "evidence_refs": {"message_indices": [j for i in group for j in spans[i].user_message_indices]},
                   "task_kind": "fixture"} for group in groups],
        "context_span_ids": [span.span_id for i, span in enumerate(spans) if i not in assigned],
        "relations": [], "label_status": "COMPLETE",
    }
    agent = SimpleNamespace(run=lambda **_: SimpleNamespace(
        payload=payload, completed=True, errors=[], final_text=json.dumps(payload), turns=[]))
    with TemporaryDirectory() as output:
        source = build_raw_session_source(raw_line=raw_line, line_number=4,
                                         source_ref="fixture:session", agent=agent, output_root=output)
    for task, task_id in zip(source["tasks"], task_ids or [f"fixture-task-{i}" for i in range(len(groups))], strict=True):
        task["task_id"] = task_id
    source["selected_task_ids"] = [task["task_id"] for task in source["tasks"]]
    source["domain_route"] = domain
    return source


class FakeHermesAgent:
    def __init__(
        self,
        *,
        intent_ok: bool = True,
        intent_bindings: list[dict] | None = None,
        completion: dict | None = None,
        sufficiency: dict | None = None,
        **kwargs: Any,
    ) -> None:
        self.intent_ok = intent_ok
        self.intent_bindings = intent_bindings
        self.completion = completion
        self.sufficiency = sufficiency
        self.kwargs = kwargs

    def run_conversation(self, instruction: str, system_message=None, task_id=None):
        if instruction.startswith("VERIFIER_SEMANTIC_REVIEW\n"):
            specification = json.loads(instruction.splitlines()[-1])
            payload = {
                "decision": "ACCEPT", "issues": [],
                "obligation_reviews": [{"obligation_id": oid, "covered": True,
                                        "reason": "固定 fixture 模拟语义审查通过"}
                                       for oid in [*specification["file_obligation_ids"],
                                                   *specification.get("response_obligation_ids", [])]],
            }
        elif task_id == "session_tasks":
            catalog = next(json.loads(line.split("=", 1)[1]) for line in instruction.splitlines()
                           if line.startswith("SPAN_CATALOG="))
            payload = {"tasks": [{"span_ids": [item["span_id"]],
                                  "evidence_refs": {"message_indices": item["user_message_indices"]},
                                  "task_kind": "fixture"} for item in catalog],
                       "context_span_ids": [], "relations": [], "label_status": "COMPLETE"}
        elif task_id == "intent":
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
                "environment_bindings": (
                    self.intent_bindings if self.intent_bindings is not None else [{
                    "obligation_id": "obl-001", "verifier_kind": "FILE",
                    "required_paths": ["foo.py"], "initial_required_paths": ["foo.py"],
                    "output_paths": [], "observable": "入口函数原文已提取",
                    }]
                ) if self.intent_ok else [],
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
            probe_ids = []
            if task_id == "sufficiency" and hasattr(self, "_invoke_tool"):
                probe_specs = (
                    (
                        "load",
                        "from pathlib import Path; assert Path('foo.py').is_file()",
                    ),
                    (
                        "reset",
                        "import os; from pathlib import Path; "
                        "p=Path(os.environ['TRACEFORGE_PROBE_SCRATCH'])/'state'; "
                        "assert not p.exists(); p.write_text('ok')",
                    ),
                    (
                        "dependency",
                        "import ast; from pathlib import Path; "
                        "ast.parse(Path('foo.py').read_text())",
                    ),
                )
                for purpose, python_code in probe_specs:
                    summary = self._invoke_tool(
                        "run_environment_probe",
                        {"python_code": python_code, "purpose": purpose, "timeout_seconds": 30},
                        task_id,
                    )
                    with contextlib.suppress(TypeError, KeyError, json.JSONDecodeError):
                        probe_ids.append(json.loads(summary)["probe_id"])
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
            if task_id == "sufficiency" and probe_ids:
                payload = dict(payload)
                payload["environment_checks"] = [
                    {"kind": purpose, "probe_ids": [probe_id], "reason": "regression probe"}
                    for (purpose, _), probe_id in zip(probe_specs, probe_ids, strict=False)
                ]
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
        intent_bindings: list[dict] | None = None,
        completion: dict | None = None,
        sufficiency: dict | None = None,
    ) -> None:
        self.intent_ok = intent_ok
        self.intent_bindings = intent_bindings
        self.completion = completion
        self.sufficiency = sufficiency
        self.last_kwargs: dict[str, Any] = {}
        self.last_agent: FakeHermesAgent | None = None

    def __call__(self, **kwargs: Any) -> FakeHermesAgent:
        self.last_kwargs = kwargs
        self.last_agent = FakeHermesAgent(
            intent_ok=self.intent_ok,
            intent_bindings=self.intent_bindings,
            completion=self.completion,
            sufficiency=self.sufficiency,
            **kwargs,
        )
        return self.last_agent
