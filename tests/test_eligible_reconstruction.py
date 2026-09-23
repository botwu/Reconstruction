from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_fakes import FakeHermesFactory, tagged_record
from p1_fixtures import load_jsonl_row, wrap_v9_code_file_record
from traceforge.cli import main
from traceforge.reconstruction.agents import SandboxedAgentRuntime, build_hermes_runtime
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.sandbox import LocalExecRuntime
from traceforge.reconstruction.eligible_reconstruction import (
    execution_support_route,
    run_eligible_reconstruction,
)
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.intent_recovery import _substantive_text, run_intent_recovery
from traceforge.reconstruction.session_source import build_reconstruction_source, load_raw_line
from traceforge.reconstruction.verification import VerificationConfig
from traceforge.reconstruction.workspace_completion import run_workspace_completion
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency
from traceforge.screening.observable import build_spans


def _session() -> dict[str, object]:
    return {
        "messages": [
            {"role": "user", "content": "把 foo.py 里的入口函数读出来，不要改文件"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "exec",
                            "arguments": {"command": "cat foo.py"},
                        },
                    },
                    {
                        "id": "c2",
                        "function": {
                            "name": "exec",
                            "arguments": {"command": "cat helper.py"},
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "def main():\n    return 1\n"},
            {"role": "tool", "tool_call_id": "c2", "content": "def helper():\n    return 2\n"},
            {"role": "assistant", "content": "还没展示完"},
        ],
        "meta": {},
        "tools": [],
        "domain_meta": {},
    }


def _record(raw_line: str) -> dict[str, object]:
    return tagged_record(raw_line)


def _agent(*, intent_ok: bool = True, completion: dict | None = None):
    return build_hermes_runtime(
        model_name="claude-opus-4-6",
        factory=FakeHermesFactory(intent_ok=intent_ok, completion=completion),
        base_url="https://tokenhub.example/v1",
        api_key="sk-test",
    )


def _sandboxed_agent(tmp_path: Path, **kwargs):
    return SandboxedAgentRuntime(
        _agent(**kwargs),
        lambda: LocalExecRuntime(tmp_path / "ags"),
    )


class _ResultRuntime:
    model_name = "test-model"
    backend = "hermes-sandbox"

    def __init__(self, payload: dict):
        self.payload = payload

    def run(self, *, role, instruction, session, output_root):
        del instruction, session, output_root
        return AgentResult(
            role=role.name,
            backend=self.backend,
            payload=self.payload,
            final_text=json.dumps(self.payload),
            completed=True,
        )


def test_eligible_run_reaches_sufficient_workspace(tmp_path: Path) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    manifest = run_eligible_reconstruction(
        raw_line=raw_line,
        record=_record(raw_line),
        agent=_sandboxed_agent(tmp_path),
        output_root=tmp_path / "run",
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "PENDING_EXECUTION"
    assert payload["task_count"] == 1
    task_result = payload["tasks"][0]
    assert task_result["status"] == "PENDING_EXECUTION"
    assert task_result["workspace"]
    workspace = Path(task_result["workspace"])
    assert (workspace / "foo.py").read_text(encoding="utf-8").startswith("def main")
    task_root = tmp_path / "run/tasks" / task_result["task_id"]
    assert (tmp_path / "run/intent/tasks" / task_result["task_id"] / "intent.json").is_file()
    assert (task_root / "completion/completion.json").is_file()
    assert (task_root / "replay.json").is_file()
    assert (task_root / "task_environment_pair.json").is_file()
    pair = json.loads((task_root / "task_environment_pair.json").read_text(encoding="utf-8"))
    assert pair["q"]["source_task_id"] == task_result["task_id"]
    assert pair["environment"]["verification_status"] == "PENDING_EXECUTION"
    stage_metrics = json.loads((tmp_path / "run/stage_metrics.json").read_text(encoding="utf-8"))
    assert stage_metrics["completion_ready_count"] == 1
    assert stage_metrics["verification_status_counts"]["PENDING_EXECUTION"] == 1
    assert (tmp_path / "run/reconstruction_manifest.json").is_file()
    assert task_result["stopped_at"] == "verification"
    assert task_result["execution_support_route"]["route"] == "TERMINAL_FILE"
    assert task_result["env_origin"] == "REPLAYED"
    sft = json.loads((tmp_path / "run/sft/curation.json").read_text(encoding="utf-8"))
    assert sft["schema_version"] == "traceforge.sft-curation.v1"
    assert sft["status"] in {"PENDING", "REVIEW"}
    assert sft["tasks"][0]["eligibility"] == "PENDING"


def test_eligible_run_stops_when_intent_is_review(tmp_path: Path) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    manifest = run_eligible_reconstruction(
        raw_line=raw_line,
        record=_record(raw_line),
        agent=_agent(intent_ok=False),
        output_root=tmp_path / "run",
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "REVIEW"
    assert payload["tasks"][0]["stopped_at"] == "intent"
    assert list((tmp_path / "run/tasks").glob("*/task_environment_pair.json"))
    assert not list((tmp_path / "run/tasks").glob("*/completion/completion.json"))


def test_multi_task_intent_to_environment_keeps_task_boundaries(tmp_path: Path) -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "展示 foo.py 的入口函数"},
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "exec", "arguments": {"command": "cat foo.py"}}},
                {"id": "c1b", "function": {"name": "exec", "arguments": {"command": "cat foo_util.py"}}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "def foo():\n    return 1\n"},
            {"role": "tool", "tool_call_id": "c1b", "content": "def foo_util():\n    return 10\n"},
            {"role": "user", "content": "展示 bar.py 的入口函数"},
            {"role": "assistant", "tool_calls": [
                {"id": "c2", "function": {"name": "exec", "arguments": {"command": "cat bar.py"}}},
                {"id": "c2b", "function": {"name": "exec", "arguments": {"command": "cat bar_util.py"}}},
            ]},
            {"role": "tool", "tool_call_id": "c2", "content": "def bar():\n    return 2\n"},
            {"role": "tool", "tool_call_id": "c2b", "content": "def bar_util():\n    return 20\n"},
        ],
        "meta": {},
        "tools": [],
    }
    raw_line = json.dumps(payload, ensure_ascii=False)
    record = tagged_record(raw_line, selected=[0, 1])
    manifest = run_eligible_reconstruction(
        raw_line=raw_line,
        record=record,
        agent=_sandboxed_agent(tmp_path),
        output_root=tmp_path / "multi",
    )
    result = json.loads(manifest.read_text(encoding="utf-8"))
    assert result["status"] == "PENDING_EXECUTION"
    assert result["task_count"] == 2
    assert {item["status"] for item in result["tasks"]} == {"PENDING_EXECUTION"}
    workspaces = {Path(item["workspace"]) for item in result["tasks"]}
    assert len(workspaces) == 2
    assert any((workspace / "foo.py").is_file() for workspace in workspaces)
    assert any((workspace / "bar.py").is_file() for workspace in workspaces)
    assert {item["task_id"] for item in result["tasks"]} == set(record["triage"]["selected_task_ids"])


def test_intent_drops_framework_injection() -> None:
    assert _substantive_text("<environment_context>\n<cwd>/</cwd>") is None
    assert _substantive_text("# AGENTS.md instructions\nPrefer small diffs") is None
    assert (
        _substantive_text(
            "# Files mentioned by the user:\n## x.png\n## My request for Codex:\n这个不能为null"
        )
        == "这个不能为null"
    )
    assert _substantive_text("请只读展示 P001") == "请只读展示 P001"


def test_intent_recovery_uses_user_texts_not_task_recovery(tmp_path: Path) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    source = build_reconstruction_source(raw_line=raw_line, record=_record(raw_line))
    result = run_intent_recovery(
        source=source,
        agent=_agent(),
        output_root=tmp_path / "intent",
    )
    assert result["status"] == "READY"
    assert "入口" in result["task"]["core_objective"]
    task_id = source["tasks"][0]["task_id"]
    prompt = json.loads(
        (tmp_path / "intent/tasks" / task_id / "private/model_exchange.json").read_text(encoding="utf-8")
    )["request"]["prompt"]
    assert "把 foo.py 里的入口函数读出来" in prompt
    assert "evidence_join" not in prompt
    assert '"id": "user:0"' in prompt or "user:0" in prompt


def test_completion_rejects_protected_overwrite_and_keeps_audit(tmp_path: Path) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    source = build_reconstruction_source(raw_line=raw_line, record=_record(raw_line))
    timeline = [
        *list(source["tool_timeline"]),
        {
            "call_id": "note",
            "name": "exec",
            "arguments": {"command": "echo requirements.txt"},
            "result_text": "need requirements.txt",
        },
    ]
    replay = replay_from_timeline(timeline, tmp_path / "bE0")
    overwrite = {
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
        ],
        "open_questions": [],
    }
    result = run_workspace_completion(
        task={"core_objective": "读入口并补 requirements.txt", "success_criteria": ["看到原文"]},
        replay=replay,
        timeline=timeline,
        agent=_ResultRuntime(overwrite),
        output_root=tmp_path / "completion",
    )
    assert result["status"] == "REVIEW"
    assert result["candidates"][0]["workspace"] is None
    assert "PROTECTED_FILE_OVERWRITE:foo.py" in result["candidates"][0]["errors"]


def test_completion_materializes_replayed_workspace(tmp_path: Path) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    source = build_reconstruction_source(raw_line=raw_line, record=_record(raw_line))
    replay = replay_from_timeline(list(source["tool_timeline"]), tmp_path / "bE0")
    result = run_workspace_completion(
        task={"core_objective": "读入口", "success_criteria": ["看到原文"]},
        replay=replay,
        timeline=list(source["tool_timeline"]),
        agent=_sandboxed_agent(tmp_path),
        output_root=tmp_path / "completion",
    )
    assert result["status"] == "READY"
    workspace = Path(result["candidates"][0]["workspace"])
    assert (workspace / "foo.py").read_text(encoding="utf-8").startswith("def main")
    assert (workspace.parent / "hidden_control" / "withheld_changes.json").is_file()


def test_sufficiency_reads_materialized_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "foo.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    result = run_workspace_sufficiency(
        task={"core_objective": "读入口", "success_criteria": ["看到原文"]},
        workspace_root=workspace,
        agent=_sandboxed_agent(tmp_path),
        output_root=tmp_path / "judge",
    )
    assert result["label"] == "SUFFICIENT"
    assert result["decision"] == "READY"
    assert result["file_count"] == 1


def test_intent_without_user_message_is_review_not_crash(tmp_path: Path) -> None:
    source = {
        "tasks": [
            {
                "task_id": "t-empty",
                "span_ids": ["s1"],
                "reconstruction_eligible": True,
                "needs_reconstruction": True,
                "is_actionable": True,
                "tags": ["selected_for_reconstruction"],
                "message_indices": [],
                "user_texts": [],
            }
        ],
        "raw_session": {"messages": [{"role": "assistant", "content": "还没开始"}]},
    }
    result = run_intent_recovery(
        source=source,
        agent=_agent(),
        output_root=tmp_path / "intent",
    )
    assert result["status"] == "REVIEW"
    assert "NO_ACTIONABLE_USER_MESSAGE" in result["errors"]
    assert result["task"]["task_id"] == "t-empty"


def test_intent_requires_obligations_and_instruction(tmp_path: Path) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    source = build_reconstruction_source(raw_line=raw_line, record=_record(raw_line))
    result = run_intent_recovery(
        source=source,
        agent=_agent(intent_ok=False),
        output_root=tmp_path / "intent",
    )
    assert result["status"] == "REVIEW"
    assert "TASK_INSTRUCTION_REQUIRED" in result["errors"]
    assert "ACCEPTANCE_OBLIGATIONS_REQUIRED" in result["errors"]


def test_eligible_non_file_task_stops_at_completion(tmp_path: Path) -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "解释一下这段日志为什么超时"},
            {"role": "assistant", "content": "还没看完"},
        ],
        "meta": {},
        "tools": [],
        "domain_meta": {},
    }
    raw_line = json.dumps(payload, ensure_ascii=False)
    manifest = run_eligible_reconstruction(
        raw_line=raw_line,
        record=_record(raw_line),
        agent=_agent(),
        output_root=tmp_path / "run",
        verification_config=VerificationConfig(
            harbor_root=tmp_path / "harbor",
            model_name="claude-opus-4-8",
            rollout_model="anthropic/claude-opus-4-8",
        ),
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "REVIEW"
    assert payload["intent"]["status"] == "READY"
    task_result = payload["tasks"][0]
    assert task_result["stopped_at"] == "completion"
    assert task_result["execution_support_route"]["route"] == "DEFAULT_EMPTY"
    assert task_result["execution_support_route"]["env_origin"] == "DEFAULT_EMPTY"
    assert task_result["env_origin"] == "DEFAULT_EMPTY"
    assert (tmp_path / "run/intent/intent.json").is_file()
    pair_paths = list((tmp_path / "run/tasks").glob("*/task_environment_pair.json"))
    assert pair_paths
    pair = json.loads(pair_paths[0].read_text(encoding="utf-8"))
    assert pair["environment"]["origin"] == "DEFAULT_EMPTY"
    assert pair["environment"]["initial_workspace_ref"] is not None
    assert list((tmp_path / "run/tasks").glob("*/completion/completion.json"))


def test_thin_tree_runs_intent_and_completion(tmp_path: Path) -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "看一下 build.bat"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "exec", "arguments": {"command": "cat build.bat"}},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "@echo off\n"},
        ],
        "meta": {},
        "tools": [],
        "domain_meta": {},
    }
    raw_line = json.dumps(payload, ensure_ascii=False)
    manifest = run_eligible_reconstruction(
        raw_line=raw_line,
        record=_record(raw_line),
        agent=_sandboxed_agent(tmp_path),
        output_root=tmp_path / "run",
    )
    result = json.loads(manifest.read_text(encoding="utf-8"))
    assert result["intent"]["status"] == "READY"
    task_result = result["tasks"][0]
    assert task_result["execution_support_route"]["route"] == "TERMINAL_FILE"
    assert task_result["execution_support_route"]["env_origin"] == "REPLAYED"
    assert task_result["env_origin"] == "REPLAYED"
    assert "TREE_TOO_THIN" not in (task_result.get("errors") or [])
    assert (tmp_path / "run/intent/intent.json").is_file()
    assert (tmp_path / "run/tasks" / task_result["task_id"] / "completion/completion.json").is_file()


def test_intent_skips_ags_when_runtime_is_sandboxed(tmp_path: Path) -> None:
    started = {"count": 0}

    class Boom:
        def __init__(self, *args, **kwargs):
            started["count"] += 1
            raise ConnectionError("ags should not start for intent")

    raw_line = json.dumps(_session(), ensure_ascii=False)
    source = build_reconstruction_source(raw_line=raw_line, record=_record(raw_line))
    result = run_intent_recovery(
        source=source,
        agent=SandboxedAgentRuntime(_agent(), Boom),
        output_root=tmp_path / "intent",
    )
    assert result["status"] == "READY"
    assert started["count"] == 0
    assert not any(str(item).startswith("SANDBOX_INIT") for item in result.get("errors") or [])


def test_completion_ags_failure_stops_at_sandbox_init(tmp_path: Path) -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "读 foo.py 和 helper.py，并补 requirements.txt"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "exec", "arguments": {"command": "cat foo.py"}}},
                    {"id": "c2", "function": {"name": "exec", "arguments": {"command": "cat helper.py"}}},
                    {
                        "id": "c3",
                        "function": {
                            "name": "exec",
                            "arguments": {"command": "echo requirements.txt"},
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "def main():\n    return 1\n"},
            {"role": "tool", "tool_call_id": "c2", "content": "def helper():\n    return 2\n"},
            {"role": "tool", "tool_call_id": "c3", "content": "need requirements.txt"},
        ],
        "meta": {},
        "tools": [],
        "domain_meta": {},
    }

    class Boom:
        def __init__(self, *args, **kwargs):
            raise ConnectionError("ags down")

    raw_line = json.dumps(payload, ensure_ascii=False)
    manifest = run_eligible_reconstruction(
        raw_line=raw_line,
        record=_record(raw_line),
        agent=SandboxedAgentRuntime(_agent(), Boom),
        output_root=tmp_path / "run",
    )
    result = json.loads(manifest.read_text(encoding="utf-8"))
    assert result["intent"]["status"] == "READY"
    task_result = result["tasks"][0]
    assert task_result["stopped_at"] == "sandbox_init"
    assert task_result["errors"] == ["SANDBOX_INIT:ConnectionError"]
    assert "TASK_INSTRUCTION_REQUIRED" not in task_result["errors"]
    assert (tmp_path / "run/intent/intent.json").is_file()


def test_retrieval_stops_at_support_route_before_completion(tmp_path: Path) -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "帮我查一下这所大学有哪些计算机专业"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {
                            "name": "browse_web",
                            "arguments": {"url": "https://example.edu/cs"},
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "CS, SE"},
        ],
        "meta": {},
        "tools": [],
        "domain_meta": {},
    }
    raw_line = json.dumps(payload, ensure_ascii=False)
    record = _record(raw_line)
    record["route"] = "ELIGIBLE_TASK"
    record["triage"]["tasks"][0]["domain_route"] = "retrieval"
    manifest = run_eligible_reconstruction(
        raw_line=raw_line,
        record=record,
        agent=_agent(),
        output_root=tmp_path / "run",
        verification_config=VerificationConfig(
            harbor_root=tmp_path / "harbor",
            model_name="claude-opus-4-8",
            rollout_model="anthropic/claude-opus-4-8",
        ),
    )
    result = json.loads(manifest.read_text(encoding="utf-8"))
    assert result["status"] == "REVIEW"
    assert result["intent"]["status"] == "SKIPPED"
    task_result = result["tasks"][0]
    assert task_result["stopped_at"] == "support_route"
    assert "RETRIEVAL_UNSUPPORTED" in task_result["errors"]
    assert task_result["execution_support_route"]["route"] == "RETRIEVAL_UNSUPPORTED"
    assert task_result.get("completion") is None
    assert task_result.get("verification") is None
    assert not (tmp_path / "run/intent/intent.json").is_file()
    assert not list((tmp_path / "run/tasks").glob("*/completion/completion.json"))


def test_completion_allows_empty_candidate_without_file_ops(tmp_path: Path) -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "解释一下这段日志为什么超时"},
            {"role": "assistant", "content": "还没看完"},
        ],
        "meta": {},
        "tools": [],
        "domain_meta": {},
    }
    raw_line = json.dumps(payload, ensure_ascii=False)
    source = build_reconstruction_source(raw_line=raw_line, record=_record(raw_line))
    assert source["selected_span_has_file_ops"] is False
    replay = replay_from_timeline(list(source["tool_timeline"]), tmp_path / "bE0")
    result = run_workspace_completion(
        task={"core_objective": "解释超时"},
        replay=replay,
        timeline=list(source["tool_timeline"]),
        source=source,
        agent=_sandboxed_agent(tmp_path, completion={"candidates": [], "open_questions": []}),
        output_root=tmp_path / "completion",
    )
    assert result["status"] == "REVIEW"
    assert result["env_origin"] == "REPLAYED"
    assert "EMPTY_REPLAY_TREE" in result["errors"]
    assert result["agent"]["skip_reason"] == "EMPTY_REPLAY_TREE"


def test_completion_accepts_empty_review_when_session_replay_is_empty(tmp_path: Path) -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "解释一下这段日志为什么超时"},
            {"role": "assistant", "content": "还没看完"},
        ],
        "meta": {},
        "tools": [],
        "domain_meta": {},
    }
    raw_line = json.dumps(payload, ensure_ascii=False)
    source = build_reconstruction_source(raw_line=raw_line, record=_record(raw_line))
    replay = replay_from_timeline(list(source["tool_timeline"]), tmp_path / "bE0")
    result = run_workspace_completion(
        task={"core_objective": "解释超时"},
        replay=replay,
        timeline=list(source["tool_timeline"]),
        source=source,
        agent=_sandboxed_agent(
            tmp_path,
            completion={
                "candidates": [
                    {
                        "files": [],
                        "dependencies": [],
                        "runtime_constraints": [],
                        "uncertainties": ["no prior files"],
                        "decision": "REVIEW",
                    }
                ]
            },
        ),
        output_root=tmp_path / "completion",
    )
    assert result["status"] == "REVIEW"
    assert result["candidates"][0]["workspace"] is None


_V9_RECORDS = Path(
    "/tmp/traceforge-screening-r01-v9-deepseek/80d2d1f6b99ad9fd6b23f846743d0632fbd621bcda01c5773ab2e19ad1c92ae6/private/records.jsonl"
)
_R01 = Path(
    "/mnt/afs_toolcall/wujian1/Projects/workspace/TraceRconstruction/return_data/four_batch/by-rubric/R01.jsonl"
)


def test_l22_named_dumps_unlock_terminal_file() -> None:
    v9 = load_jsonl_row(_V9_RECORDS, 22)
    if v9 is None or not _R01.is_file():
        pytest.skip("v9 L22 records or R01.jsonl not available")
    raw_line = load_raw_line(_R01, line_number=22)
    record = wrap_v9_code_file_record(v9, raw_line)
    record["line_sha256"] = hashlib.sha256(raw_line.encode("utf-8")).hexdigest()
    source = build_reconstruction_source(raw_line=raw_line, record=record)
    replay = replay_from_timeline(list(source.get("tool_timeline") or []))
    paths = {item.path for item in replay.files}
    assert "build.bat" in paths
    assert "Injector.cpp" in paths
    assert "Loader.cpp" in paths
    screening = (source.get("tasks") or [{"domain_route": "code_file"}])[0]
    support = execution_support_route(task=screening, source=source, replay=replay)
    assert support["route"] == "TERMINAL_FILE"
    assert support["allow_completion"] is True
    mixed = {
        "domain_route": "code_file",
        "environment_bindings": [
            {"obligation_id": "obl-001", "verifier_kind": "NON_FILE", "required_paths": []},
            {"obligation_id": "obl-002", "verifier_kind": "FILE", "required_paths": ["Injector.cpp"]},
        ],
    }
    after_intent = execution_support_route(task=mixed, source=source, replay=replay)
    assert after_intent["route"] == "TERMINAL_FILE"
    assert after_intent["allow_completion"] is True
    assert after_intent["unverified_obligations"] == ["obl-001"]


def test_cli_sandbox_exits_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGS_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    raw_line = json.dumps(_session(), ensure_ascii=False) + "\n"
    input_path = tmp_path / "sessions.jsonl"
    input_path.write_text(raw_line, encoding="utf-8")
    records_path = tmp_path / "records.jsonl"
    records_path.write_text("{}\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text('claude:\n  {"url": "https://tokenhub.example/v1", "key": "sk-test"}\n')
    assert (
        main(
            [
                "reconstruct",
                "run",
                "--input",
                str(input_path),
                "--records",
                str(records_path),
                "--line-number",
                "1",
                "--output",
                str(tmp_path / "out"),
                "--config",
                str(config),
                "--sandbox",
            ]
        )
        == 2
    )
    assert "AGS_API_KEY" in capsys.readouterr().err


def test_cli_reconstruct_run_writes_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False) + "\n"
    input_path = tmp_path / "sessions.jsonl"
    input_path.write_text("ignored\n" * 3 + raw_line, encoding="utf-8")
    record = _record(raw_line)
    record["line_sha256"] = hashlib.sha256(raw_line.encode("utf-8")).hexdigest()
    records_path = tmp_path / "records.jsonl"
    records_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        'claude:\n  {"url": "https://tokenhub.example/v1", "key": "sk-test"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "traceforge.cli.build_hermes_runtime",
        lambda **kwargs: _sandboxed_agent(tmp_path),
    )
    monkeypatch.setattr("traceforge.cli.resolve_model_name", lambda *args, **kwargs: "claude-opus-4-6")
    # Keep this CLI contract test offline.  Without an explicit verifier model,
    # the reconstruction must fail closed after producing the task/environment
    # artifacts instead of attempting an external model request.
    monkeypatch.setattr("traceforge.cli.build_chat_model", lambda **kwargs: None)
    output = tmp_path / "out"
    assert (
        main(
            [
                "reconstruct",
                "run",
                "--input",
                str(input_path),
                "--records",
                str(records_path),
                "--line-number",
                "4",
                "--output",
                str(output),
                "--config",
                str(config),
                "--channel",
                "claude",
            ]
        )
        == 0
    )
    published = Path(capsys.readouterr().out.strip())
    payload = json.loads(published.read_text(encoding="utf-8"))
    assert payload["status"] == "REVIEW"
    assert payload["stopped_at"] == "verification"
    assert published.name == "reconstruction_manifest.json"
    assert (output / "reconstruction_source.json").is_file()
    task_result = payload["tasks"][0]
    task_root = output / "tasks" / task_result["task_id"]
    assert task_result["status"] == "REVIEW"
    assert task_result["stopped_at"] == "verification"
    # The CLI always supplies the Hermes runtime.  In a sandbox, verifier
    # recovery therefore fails closed until a hidden RED pytest run exists.
    assert "SANDBOX_PYTEST_RED_REQUIRED" in task_result["errors"]
    assert task_result["verification"]["status"] == "REVIEW"
    assert "SANDBOX_PYTEST_RED_REQUIRED" in task_result["verification"]["errors"]
    assert (task_root / "replay.json").is_file()
    assert Path(task_result["workspace"]).joinpath("foo.py").is_file()
