from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_fakes import FakeHermesFactory, raw_source
from traceforge.cli import main
from traceforge.reconstruction.agents import SandboxedAgentRuntime, build_hermes_runtime
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.sandbox import LocalExecRuntime
from traceforge.reconstruction.pipeline import (
    execution_support_route,
    run_reconstruction,
)
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.intent_recovery import _substantive_text, run_intent_recovery
from traceforge.reconstruction.session_source import load_raw_line
from traceforge.reconstruction.verification import VerificationConfig
from traceforge.reconstruction.workspace_completion import run_workspace_completion
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency
from traceforge.reconstruction.session_spans import build_spans


def test_known_search_domain_cannot_be_overridden_by_task_or_files() -> None:
    route = execution_support_route(
        task={"domain_route": "terminal"}, source={"domain_route": "retrieval"},
        replay=SimpleNamespace(files=[SimpleNamespace(path="snippet.py")]),
    )
    assert route["domain_route"] == "retrieval"
    assert route["route"] == "RETRIEVAL_UNSUPPORTED"
    assert route["allow_completion"] is False


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
    return raw_source(raw_line)


def _agent(*, intent_ok: bool = True, completion: dict | None = None, sufficiency: dict | None = None):
    return build_hermes_runtime(
        model_name="claude-opus-4-6",
        factory=FakeHermesFactory(
            intent_ok=intent_ok, completion=completion, sufficiency=sufficiency
        ),
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


def test_reconstruction_reaches_sufficient_workspace(tmp_path: Path) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    manifest = run_reconstruction(
        source=_record(raw_line),
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


def test_task_fit_review_reaches_verification_stage(tmp_path: Path) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    manifest = run_reconstruction(
        source=_record(raw_line),
        agent=_sandboxed_agent(
            tmp_path,
            sufficiency={
                "label": "SUFFICIENT",
                "reason": "source is observable",
                "missing_context": [],
                "confidence": 0.8,
                "decision": "READY",
                "task_fit": {
                    "decision": "READY_ORIGINAL",
                    "requirements": [{
                        "obligation_id": "obl-001",
                        "status": "UNKNOWN",
                        "reason": "behavior is not proven by reconstruction evidence",
                        "evidence_paths": ["foo.py"],
                        "probe_ids": [],
                    }],
                },
            },
        ),
        output_root=tmp_path / "run",
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    task_result = payload["tasks"][0]
    assert task_result["task_fit"]["decision"] == "REVIEW_TASK_FIT"
    assert task_result["task_fit"]["execution_policy"] == "PROCEED_ORIGINAL"
    assert task_result["stopped_at"] == "verification"
    assert task_result["errors"] == ["VERIFICATION_NOT_CONFIGURED"]


def test_reconstruction_stops_when_intent_is_review(tmp_path: Path) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False)
    manifest = run_reconstruction(
        source=_record(raw_line),
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
    record = raw_source(raw_line, selected=[0, 1])
    manifest = run_reconstruction(
        source=record,
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
    assert {item["task_id"] for item in result["tasks"]} == set(record["selected_task_ids"])


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
    source = _record(raw_line)
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
    source = _record(raw_line)
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
    source = _record(raw_line)
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
    source = _record(raw_line)
    result = run_intent_recovery(
        source=source,
        agent=_agent(intent_ok=False),
        output_root=tmp_path / "intent",
    )
    assert result["status"] == "REVIEW"
    assert "TASK_INSTRUCTION_REQUIRED" in result["errors"]
    assert "ACCEPTANCE_OBLIGATIONS_REQUIRED" in result["errors"]


def test_reconstruction_non_file_task_stops_at_completion(tmp_path: Path) -> None:
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
    manifest = run_reconstruction(
        source=_record(raw_line),
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
    manifest = run_reconstruction(
        source=_record(raw_line),
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
    source = _record(raw_line)
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
    manifest = run_reconstruction(
        source=_record(raw_line),
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
    source = _record(raw_line)
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
    source = _record(raw_line)
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


def test_cli_sandbox_exits_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGS_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    raw_line = json.dumps(_session(), ensure_ascii=False) + "\n"
    input_path = tmp_path / "sessions.jsonl"
    input_path.write_text(raw_line, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text('claude:\n  {"url": "https://tokenhub.example/v1", "key": "sk-test"}\n')
    assert (
        main(
            [
                "reconstruct",
                "raw-run",
                "--domain",
                "terminal",
                "--input",
                str(input_path),
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


def test_cli_raw_run_writes_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_line = json.dumps(_session(), ensure_ascii=False) + "\n"
    input_path = tmp_path / "sessions.jsonl"
    input_path.write_text("ignored\n" * 3 + raw_line, encoding="utf-8")
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
    # 走真实解析与回放接口；仅替换外部模型响应，验证 DeepSeek 角色确实进入主流程。
    from test_session_parser import _Model, _event, _read

    operations = [_read("foo.py"), _read("helper.py")]
    for operation in operations:
        operation["content_ref"].update(block_index=0, json_path=[])
    parser = _Model({
        "domain": "terminal", "workspace_root": None, "unresolved": [],
        "events": [_event(i, [operation]) for i, operation in enumerate(operations)],
    })
    monkeypatch.setattr(
        "traceforge.cli.build_chat_model",
        lambda **kwargs: parser if kwargs["channel"] == "deepseek" else None,
    )
    output = tmp_path / "out"
    assert (
        main(
            [
                "reconstruct",
                "raw-run",
                "--domain",
                "terminal",
                "--input",
                str(input_path),
                "--line-number",
                "4",
                "--output",
                str(output),
                "--config",
                str(config),
                "--channel",
                "claude",
                "--no-sandbox",
            ]
        )
        == 2
    )
    published = Path(capsys.readouterr().out.strip())
    assert json.loads((output / "session_parser/receipt.json").read_text())["status"] == "READY"
    payload = json.loads(published.read_text(encoding="utf-8"))
    assert payload["status"] == "PENDING_EXECUTION"
    assert payload["stopped_at"] == "verification"
    assert published.name == "reconstruction_manifest.json"
    assert (output / "reconstruction_source.json").is_file()
    task_result = payload["tasks"][0]
    task_root = output / "tasks" / task_result["task_id"]
    assert task_result["status"] == "PENDING_EXECUTION"
    assert task_result["stopped_at"] == "verification"
    assert task_result["verification"]["status"] == "PENDING_EXECUTION"
    assert task_result["verification"]["calibration"] == "NOT_RUN"
    assert task_result["verification"]["rollout"] == "NOT_RUN"
    assert task_result["verification"]["sft_eligible"] is False
    assert (task_root / "replay.json").is_file()
    assert Path(task_result["workspace"]).joinpath("foo.py").is_file()
