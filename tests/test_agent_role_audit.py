"""四个重建角色的输入、输出和关闸：按角色逐项核对。"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_fakes import FakeHermesFactory
from traceforge.reconstruction.agents import (
    COMPLETION_ROLE,
    INTENT_ROLE,
    SUFFICIENCY_ROLE,
    VERIFIER_ROLE,
    SandboxedAgentRuntime,
    build_hermes_runtime,
)
from traceforge.reconstruction.agents.roles import AgentRole
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.sandbox import LocalExecRuntime
from traceforge.reconstruction.agents.session import AgentSession, execute_tool
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.intent_recovery import IntentRecoveryError, run_intent_recovery
from traceforge.reconstruction.session_source import build_reconstruction_source
from traceforge.reconstruction.verifier_recovery import run_verifier_recovery
from traceforge.reconstruction.workspace_completion import run_workspace_completion
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency
from traceforge.screening.observable import build_spans


def _session(*, extra_user: str | None = None) -> dict[str, object]:
    messages: list[dict[str, object]] = [
        {"role": "user", "content": "把 foo.py 里的入口函数读出来，不要改文件"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": {"path": "foo.py"}},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "def main():\n    return 1\n"},
        {"role": "assistant", "content": "还没展示完"},
    ]
    if extra_user:
        messages.append({"role": "user", "content": extra_user})
    return {"messages": messages, "meta": {}, "tools": [], "domain_meta": {}}


def _record(raw_line: str, *, eligible: bool = True, extra_task: bool = False) -> dict[str, object]:
    payload = json.loads(raw_line)
    spans, _ = build_spans(payload["messages"])
    tasks = [
        {
            "task_id": "t-read-foo",
            "span_ids": [spans[0].span_id],
            "is_actionable": True,
            "reconstruction_eligible": eligible,
            "outcome": "INCOMPLETE",
            "tags": ["actionable", "incomplete", "needs_reconstruction", "rubric_pass", "selected_for_reconstruction"],
            "eligibility": {"decision": "ELIGIBLE", "blocking_reason_codes": []},
            "evidence_refs": {"span_ids": [spans[0].span_id], "message_indices": [0, 1, 2, 3]},
        }
    ]
    if extra_task and len(spans) > 1:
        tasks.append(
            {
                "task_id": "t-other",
                "span_ids": [spans[1].span_id],
                "is_actionable": True,
                "reconstruction_eligible": True,
                "outcome": "INCOMPLETE",
                "tags": ["actionable", "incomplete", "needs_reconstruction", "rubric_pass", "selected_for_reconstruction"],
                "eligibility": {"decision": "ELIGIBLE", "blocking_reason_codes": []},
                "evidence_refs": {"span_ids": [spans[1].span_id], "message_indices": [4, 5]},
            }
        )
    return {
        "decision": "ELIGIBLE",
        "route": "ELIGIBLE_CODE_FILE",
        "source_ref": "jsonl:1:audit",
        "line_number": 1,
        "triage": {
            "label_status": "COMPLETE",
            "selected_span_ids": [task["span_ids"][0] for task in tasks if task["reconstruction_eligible"]],
            "selected_task_ids": [task["task_id"] for task in tasks if task["reconstruction_eligible"]],
            "relations": [],
            "tasks": tasks,
        },
    }


def _source(**kwargs) -> dict[str, object]:
    raw = json.dumps(_session(**kwargs), ensure_ascii=False)
    return build_reconstruction_source(raw_line=raw, record=_record(raw, extra_task=bool(kwargs.get("extra_user"))))


def _hermes(**kwargs):
    return build_hermes_runtime(
        model_name="claude-opus-4-6",
        factory=FakeHermesFactory(**kwargs),
        base_url="https://tokenhub.example/v1",
        api_key="sk-test",
    )


def _sandboxed(tmp_path: Path, **kwargs):
    return SandboxedAgentRuntime(_hermes(**kwargs), lambda: LocalExecRuntime(tmp_path / "ags"))


def _task() -> dict[str, object]:
    return {
        "task_id": "t-read-foo",
        "task_instruction": "把 foo.py 里的入口函数读出来，不要改文件",
        "core_objective": "展示 foo.py 入口函数的现有内容。",
        "success_criteria": ["能看到入口函数原文"],
        "acceptance_obligations": [
            {"id": "obl-001", "text": "能看到入口函数原文", "evidence_ref_ids": ["user:0"]}
        ],
    }


class RecordingRuntime:
    model_name = "test-model"
    backend = "hermes-sandbox"

    def __init__(self, payload: dict, *, completed: bool = True, errors=None, role: AgentRole | None = None):
        self.payload = payload
        self.completed = completed
        self.errors = errors or []
        self.role = role
        self.instruction = ""
        self.session = None
        self.calls = 0

    def run(self, *, role, instruction, session, output_root):
        self.calls += 1
        self.instruction = instruction
        self.session = session
        if self.role is not None:
            assert role.name == self.role.name
        return AgentResult(
            role=role.name,
            backend=self.backend,
            payload=self.payload,
            errors=list(self.errors),
            final_text=json.dumps(self.payload, ensure_ascii=False),
            completed=self.completed,
        )


def test_intent_ready_io_and_task_boundary(tmp_path: Path) -> None:
    source = _source()
    result = run_intent_recovery(source=source, agent=_hermes(), output_root=tmp_path / "intent")
    assert result["status"] == "READY"
    assert result["task"]["task_id"] == "t-read-foo"
    assert "入口" in result["task"]["core_objective"]
    assert result["task"]["acceptance_obligations"][0]["evidence_ref_ids"] == ["user:0"]
    prompt = json.loads(
        (tmp_path / "intent/tasks/t-read-foo/private/model_exchange.json").read_text(encoding="utf-8")
    )["request"]["prompt"]
    assert "TASK_TAG=" in prompt
    assert "TASK_USER_MESSAGES=" in prompt
    assert "把 foo.py 里的入口函数读出来" in prompt
    assert "evidence_join" not in prompt
    assert (tmp_path / "intent/intent.json").is_file()
    assert (tmp_path / "intent/tasks/t-read-foo/intent.json").is_file()


def test_intent_rejects_legacy_labels_without_eligible_tasks(tmp_path: Path) -> None:
    raw = json.dumps(_session(), ensure_ascii=False)
    payload = json.loads(raw)
    spans, _ = build_spans(payload["messages"])
    source = build_reconstruction_source(
        raw_line=raw,
        record={
            "decision": "ELIGIBLE",
            "triage": {"selected_span_ids": [spans[0].span_id]},
        },
    )
    assert source["label_status"] == "LEGACY_INCOMPLETE"
    assert source["tasks"][0]["reconstruction_eligible"] is False
    try:
        run_intent_recovery(source=source, agent=_hermes(), output_root=tmp_path)
    except IntentRecoveryError as exc:
        assert "没有可重建的真实任务标签" in str(exc)
    else:
        raise AssertionError("legacy labels must not silently recover a task")


def test_intent_does_not_merge_two_eligible_tasks(tmp_path: Path) -> None:
    source = _source(extra_user="另外做一个完全不同的任务：重构 bar 服务")
    assert [item["task_id"] for item in source["tasks"] if item["reconstruction_eligible"]] == [
        "t-read-foo",
        "t-other",
    ]
    first = {
        "task_id": "t-read-foo",
        "task_instruction": "读 foo.py 入口",
        "core_objective": "读入口",
        "acceptance_obligations": [
            {"id": "obl-001", "text": "看到原文", "evidence_ref_ids": ["user:0"]}
        ],
        "success_criteria": ["看到原文"],
    }
    runtime = RecordingRuntime(first, role=INTENT_ROLE)
    result = run_intent_recovery(source=source, agent=runtime, output_root=tmp_path / "intent")
    assert runtime.calls == 2
    assert result["status"] == "REVIEW"
    assert [item.get("task_id") or item["task"]["task_id"] for item in result["tasks"]] == [
        "t-read-foo",
        "t-other",
    ]
    first_prompt = json.loads(
        (tmp_path / "intent/tasks/t-read-foo/private/model_exchange.json").read_text(encoding="utf-8")
    )["request"]["prompt"]
    assert "重构 bar 服务" not in first_prompt
    assert "t-read-foo" in first_prompt


def test_intent_gates_bad_obligations_and_forbids_writes(tmp_path: Path) -> None:
    source = _source()
    runtime = RecordingRuntime(
        {
            "task_instruction": "",
            "core_objective": "",
            "acceptance_obligations": [
                {"id": "obl-001", "text": "看到原文", "evidence_ref_ids": ["user:99"]}
            ],
        },
        role=INTENT_ROLE,
    )
    result = run_intent_recovery(source=source, agent=runtime, output_root=tmp_path / "intent")
    assert result["status"] == "REVIEW"
    assert "TASK_INSTRUCTION_REQUIRED" in result["errors"]
    assert "CORE_OBJECTIVE_REQUIRED" in result["errors"]
    assert "OBLIGATION_EVIDENCE_REQUIRED:obl-001" in result["errors"]
    assert INTENT_ROLE.allow_write is False
    assert "write_file" not in INTENT_ROLE.tools


def _timeline_with_support_hole(source: dict) -> list[dict]:
    return [
        *list(source["tool_timeline"]),
        {
            "call_id": "note",
            "name": "exec",
            "arguments": {"command": "echo requirements.txt"},
            "result_text": "need requirements.txt",
        },
    ]


def test_completion_host_blocked_sandbox_materializes(tmp_path: Path) -> None:
    source = _source()
    timeline = _timeline_with_support_hole(source)
    replay = replay_from_timeline(timeline, tmp_path / "bE0")
    task = {**_task(), "core_objective": "展示 foo.py 并补 requirements.txt"}
    host = run_workspace_completion(
        task=task,
        replay=replay,
        timeline=timeline,
        agent=_hermes(),
        output_root=tmp_path / "host",
        workspace_root=tmp_path / "bE0",
    )
    assert host["status"] == "REVIEW"
    assert "CONTAINER_REQUIRED" in host["errors"]
    ready = run_workspace_completion(
        task=task,
        replay=replay,
        timeline=timeline,
        agent=RecordingRuntime(
            {
                "candidates": [
                    {
                        "files": [],
                        "dependencies": [],
                        "runtime_constraints": [],
                        "uncertainties": [],
                        "decision": "READY",
                    }
                ]
            }
        ),
        output_root=tmp_path / "sandbox",
        workspace_root=tmp_path / "bE0",
    )
    assert ready["status"] == "READY"
    prompt = json.loads(
        (tmp_path / "sandbox/private/model_exchange.json").read_text(encoding="utf-8")
    )["request"]["prompt"]
    assert "list_dir" in prompt
    assert "solvable, but NOT solved" in prompt
    assert "HOLES:" in prompt
    workspace = Path(ready["candidates"][0]["workspace"])
    assert (workspace / "foo.py").read_text(encoding="utf-8").startswith("def main")
    assert COMPLETION_ROLE.allow_write is True
    assert COMPLETION_ROLE.tools == (
        "list_dir",
        "read_file",
        "list_evidence",
        "read_evidence",
        "write_file",
        "web_search",
    )


def test_completion_rejects_protected_overwrite(tmp_path: Path) -> None:
    source = _source()
    timeline = _timeline_with_support_hole(source)
    replay = replay_from_timeline(timeline, tmp_path / "bE0")
    result = run_workspace_completion(
        task={**_task(), "core_objective": "展示 foo.py 并补 requirements.txt"},
        replay=replay,
        timeline=timeline,
        agent=RecordingRuntime(
            {
                "candidates": [
                    {
                        "files": [
                            {
                                "path": "foo.py",
                                "content": "changed\n",
                                "provenance": "MODEL_COMPLETED",
                                "evidence_ref_ids": ["c1"],
                            }
                        ],
                        "dependencies": [],
                        "runtime_constraints": [],
                        "uncertainties": [],
                        "decision": "READY",
                    }
                ]
            }
        ),
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "bE0",
    )
    assert result["status"] == "REVIEW"
    assert "PROTECTED_FILE_OVERWRITE:foo.py" in result["candidates"][0]["errors"]
    assert result["candidates"][0]["workspace"] is None


def test_sufficiency_host_cannot_ready_even_if_model_says_sufficient(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "foo.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    result = run_workspace_sufficiency(
        task=_task(),
        workspace_root=workspace,
        agent=_hermes(),
        output_root=tmp_path / "judge",
    )
    assert result["status"] == "REVIEW"
    assert result["decision"] == "REVIEW"
    assert "REAL_PROBE_REQUIRED" in result["errors"]
    assert "READ_ONLY_PROBE_NOT_CONFIRMED" in result["errors"]
    assert SUFFICIENCY_ROLE.allow_write is False
    assert SUFFICIENCY_ROLE.tools == ("list_dir", "read_file", "run_environment_probe")


def test_sufficiency_sandbox_ready_and_forbids_write(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "foo.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    result = run_workspace_sufficiency(
        task=_task(),
        workspace_root=workspace,
        agent=_sandboxed(tmp_path),
        output_root=tmp_path / "judge",
    )
    assert result["label"] == "SUFFICIENT"
    assert result["decision"] == "READY"
    assert result["status"] == "READY"
    assert result["file_count"] == 1
    assert "REAL_PROBE_REQUIRED" not in result["errors"]
    blocked = execute_tool(
        "write_file",
        {"path": "x.py", "content": "bad", "evidence_ref_ids": ["e1"]},
        AgentSession(workspace=workspace, allow_write=False),
    )
    assert blocked.startswith("error:")


def test_sufficiency_keeps_explicit_stub_excerpt_insufficient(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "Injector.cpp").write_text("  10: int main() {\n", encoding="utf-8")
    (workspace / "Loader.cpp").write_text("int load() { return 1; }\n", encoding="utf-8")
    (workspace / "build.bat").write_text("@echo off\n", encoding="utf-8")
    (workspace / "RobloxDLL.cpp").write_text(
        "// observed name, body unobserved\n", encoding="utf-8"
    )
    (workspace / "Config.h").write_text("#pragma once\n// observed name, body unobserved\n")
    (workspace / "GUI.h").write_text("#pragma once\n// observed name, body unobserved\n")
    (workspace / "Hotkeys.h").write_text("#pragma once\n// observed name, body unobserved\n")
    agent = _sandboxed(
        tmp_path,
        sufficiency={
            "label": "INSUFFICIENT",
            "reason": (
                "7 of 9 required files are stubs containing only "
                "'observed name, body unobserved' markers. "
                "Injector.cpp contains garbage. Loader.cpp has only a partial fragment."
            ),
            "missing_context": [
                "Injector.cpp — full source code (currently corrupt/garbage)",
                "RobloxDLL.cpp — full source code (currently stub)",
                "Loader.cpp — complete file (currently only a partial fragment)",
            ],
            "confidence": 0.97,
            "decision": "REVIEW",
        },
    )
    result = run_workspace_sufficiency(
        task={
            "task_id": "t-l22",
            "task_instruction": "读注入器并评估 2026 是否仍可用",
            "core_objective": "读代码并调研",
            "acceptance_obligations": [
                {"id": "obl-002", "text": "完全读取代码", "evidence_ref_ids": ["user:0"]}
            ],
            "environment_bindings": [
                {
                    "obligation_id": "obl-002",
                    "required_paths": [
                        "Injector.cpp",
                        "Loader.cpp",
                        "build.bat",
                        "RobloxDLL.cpp",
                        "Config.h",
                        "GUI.h",
                        "Hotkeys.h",
                    ],
                    "observable": "replayed excerpts still present",
                    "verifier_kind": "FILE",
                }
            ],
        },
        workspace_root=workspace,
        agent=agent,
        output_root=tmp_path / "judge",
    )
    assert result["label"] == "INSUFFICIENT"
    assert result["decision"] == "REVIEW"
    assert result["status"] == "REVIEW"
    assert result["missing_binding_paths"] == []
    assert result["errors"] == []


def test_sufficiency_keeps_missing_binding_insufficient(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "Loader.cpp").write_text("int load() { return 1; }\n", encoding="utf-8")
    result = run_workspace_sufficiency(
        task={
            "task_id": "t-l22",
            "task_instruction": "读注入器",
            "core_objective": "读代码",
            "acceptance_obligations": [
                {"id": "obl-002", "text": "完全读取代码", "evidence_ref_ids": ["user:0"]}
            ],
            "environment_bindings": [
                {
                    "obligation_id": "obl-002",
                    "required_paths": ["Injector.cpp", "Loader.cpp"],
                    "observable": "replayed excerpts still present",
                    "verifier_kind": "FILE",
                }
            ],
        },
        workspace_root=workspace,
        agent=_sandboxed(
            tmp_path,
            sufficiency={
                "label": "INSUFFICIENT",
                "reason": "Injector.cpp is a stub and the tree is incomplete",
                "missing_context": ["Injector.cpp — full source (currently stub)"],
                "confidence": 0.9,
                "decision": "REVIEW",
            },
        ),
        output_root=tmp_path / "judge",
    )
    assert result["label"] == "INSUFFICIENT"
    assert "MISSING_BINDING_PATH" in result["errors"]
    assert "Injector.cpp" in result["missing_binding_paths"]


def test_sufficiency_empty_task_is_not_executable(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = run_workspace_sufficiency(
        task={},
        workspace_root=workspace,
        agent=_sandboxed(tmp_path),
        output_root=tmp_path / "judge",
    )
    assert result["status"] == "REVIEW"
    assert "TASK_NOT_EXECUTABLE" in result["errors"]


def test_verifier_non_file_skips_agent(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = RecordingRuntime({"status": "READY"}, role=VERIFIER_ROLE)
    recovered, candidate = run_verifier_recovery(
        task=_task(),
        workspace_root=workspace,
        agent=runtime,
        output_root=tmp_path / "verifier",
        source={"selected_span_has_file_ops": False},
    )
    assert recovered["errors"] == ["NON_FILE_TASK"]
    assert candidate is None
    assert runtime.calls == 0
    assert VERIFIER_ROLE.tools == ("list_dir", "read_file", "write_test", "run_pytest")


def test_verifier_sandbox_requires_red_then_accepts_matching_runs(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "foo.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    class SandboxVerifier(RecordingRuntime):
        def __init__(self, payload: dict, *, pytest_runs=None):
            super().__init__(payload)
            self.pytest_runs = pytest_runs or []

        def run(self, *, role, instruction, session, output_root):
            if role.result_schema == "traceforge.verifier-semantic-review.v1":
                return AgentResult(
                    role=role.name, backend="fixture", completed=True,
                    payload={"decision": "ACCEPT", "issues": [], "obligation_reviews": [
                        {"obligation_id": "obl-001", "covered": True, "reason": "固定审查结果"},
                    ]},
                )
            session.sandbox = object()
            session.test_outputs_py = self.payload["test_outputs_py"]
            session.pytest_runs.extend(self.pytest_runs)
            self.session = session
            self.instruction = instruction
            self.calls += 1
            return AgentResult(
                role=role.name,
                backend="hermes-sandbox",
                payload=self.payload,
                final_text=json.dumps(self.payload),
                completed=True,
            )

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
        "mutation_solutions": [{"name": "mutation", "script": "echo m", "justification": "m"}],
        "missing_capability_tests": ["test_missing"],
        "protective_tests": ["test_protective"],
        "obligation_coverage": {"obl-001": ["test_output"]},
        "expected_value_strategy": "independent calculation",
        "open_questions": [],
    }
    missing = SandboxVerifier(payload)
    recovered, candidate = run_verifier_recovery(
        task=_task(),
        workspace_root=workspace,
        agent=missing,
        output_root=tmp_path / "missing",
        source={"selected_span_has_file_ops": True},
    )
    assert recovered["status"] == "REVIEW"
    assert "SANDBOX_PYTEST_RED_REQUIRED" in recovered["errors"]
    assert candidate is None

    digest = __import__("hashlib").sha256(payload["test_outputs_py"].encode()).hexdigest()

    recovered, candidate = run_verifier_recovery(
        task=_task(),
        workspace_root=workspace,
        agent=SandboxVerifier(
            payload,
            pytest_runs=[
                {"name": "test_missing", "status": "FAIL", "test_sha256": digest},
                {"name": "test_protective", "status": "PASS", "test_sha256": digest},
            ],
        ),
        output_root=tmp_path / "ok",
        source={"selected_span_has_file_ops": True},
    )
    assert recovered["status"] == "READY"
    assert candidate is not None
    assert list(recovered["verifier"]["missing_capability_tests"]) == ["test_missing"]


def test_role_tool_surfaces_are_disjoint() -> None:
    assert set(INTENT_ROLE.tools).isdisjoint({"write_file", "write_test", "run_pytest"})
    assert {"read_session_message", "read_session_context"}.issubset(INTENT_ROLE.tools)
    assert "write_file" in COMPLETION_ROLE.tools
    assert "write_test" not in COMPLETION_ROLE.tools
    assert set(SUFFICIENCY_ROLE.tools) == {"list_dir", "read_file", "run_environment_probe"}
    assert "write_test" in VERIFIER_ROLE.tools
    assert "write_file" not in VERIFIER_ROLE.tools
