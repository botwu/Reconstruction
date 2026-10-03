"""真实依赖源码导入与探针续读不能丢正文，也不能绕过候选保护。"""
import hashlib
import json
import zipfile

import pytest

from traceforge.reconstruction.agents.session import AgentSession, execute_tool
from traceforge.reconstruction.python_runtime import freeze_wheels


def bundle_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "requirements.txt").write_text("sample-package==1.2\n")
    bundle = tmp_path / "python_runtime"
    (bundle / "wheels").mkdir(parents=True)
    body = "import sys\nlabel = 'public'\nvalue = 3\n"
    wheel = bundle / "wheels/sample_package-1.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("sample_package-1.2.dist-info/METADATA", "Name: sample-package\nVersion: 1.2\n")
        archive.writestr("sample/source.py", body)
        archive.writestr("sample/__init__.py", "")
    freeze_wheels(bundle, workspace / "requirements.txt")
    observed = "label = '原占位'\n"
    state = AgentSession(
        workspace=workspace, allow_write=True,
        replay_files={"requirements.txt": "sample-package==1.2\n", "vendor/source.py": observed},
        partial_files={"vendor/source.py": observed},
        evidence=[{"evidence_ref_id": "seen", "session_parse": {"file_ops": [{
            "kind": "read", "path": "vendor/source.py", "partial": True,
            "line_numbers": [2], "line_contents": [observed.rstrip("\n")],
        }]}}],
    )
    state.dependency_bundle = bundle
    args = {"distribution": "sample-package", "version": "1.2",
            "member": "sample/source.py", "path": "vendor/source.py", "evidence_ref_ids": ["seen"]}
    return state, args, wheel, body


def test_probe_output_pages_recover_middle_and_never_open_paths(tmp_path):
    output = "a" * 4000 + "不可丢失的中段" + "b" * 4000
    state = AgentSession(environment_probes=[{
        "probe_id": "p1", "executions": [{"stdout": output, "stderr": "real failure"}],
    }])
    page = execute_tool("read_probe_output", {
        "probe_id": "p1", "stream": "stdout", "offset": 4000, "limit": 8,
    }, state)
    assert page.startswith("不可丢失的中段")
    assert "offset=4008" in page
    assert execute_tool("read_probe_output", {"probe_id": "p1", "stream": "stderr"}, state) == "real failure"
    secret = tmp_path / "outside"
    secret.write_text("不可读取")
    assert execute_tool("read_probe_output", {"probe_id": str(secret)}, state).startswith("error:")
    assert execute_tool("read_probe_output", {"probe_id": "p1", "execution_index": 2}, state).startswith("error:")


def test_locked_source_preserves_observation_and_records_byte_origins(tmp_path):
    state, args, wheel, body = bundle_session(tmp_path)
    result = execute_tool("restore_dependency_source", args, state)
    assert not result.startswith("error:"), result
    content = body.replace("'public'", "'原占位'")
    assert (state.workspace / args["path"]).read_text() == content
    provenance = state.writes[-1]["dependency_source"]
    assert provenance["wheel_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert provenance["member_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert provenance["content_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert provenance["overlaid_line_numbers"] == [2]
    assert provenance["observed_line_count"] == 1
    assert provenance["scope"] == "LOCKED_DEPENDENCY_WITH_SOURCE_OBSERVATIONS"
    assert state.partial_files[args["path"]] == "label = '原占位'\n"


@pytest.mark.parametrize("change", ["wheel", "requirements", "version", "member_escape", "target_escape", "unknown_ref", "readonly", "protected"])
def test_dependency_source_rejects_changed_or_unpermitted_inputs(tmp_path, change):
    state, args, wheel, _ = bundle_session(tmp_path)
    if change == "wheel":
        wheel.write_bytes(b"tampered")
    elif change == "requirements":
        state.replay_files["requirements.txt"] = "sample-package==9\n"
    elif change == "version":
        args["version"] = "2"
    elif change == "member_escape":
        args["member"] = "../source.py"
    elif change == "target_escape":
        args["path"] = "../outside.py"
    elif change == "unknown_ref":
        args["evidence_ref_ids"] = ["other"]
    elif change == "readonly":
        state.allow_write = False
    else:
        state.protected_paths.add(args["path"])
    result = execute_tool("restore_dependency_source", args, state)
    assert result.startswith("error:"), result
    assert not state.writes


def test_dependency_source_rejects_conflicting_observations_without_cherry_picking(tmp_path):
    state, args, _, _ = bundle_session(tmp_path)
    state.evidence.append({"evidence_ref_id": "conflict", "session_parse": {"file_ops": [{
        "kind": "read", "path": args["path"], "line_numbers": [2], "line_contents": ["different"],
    }]}})
    result = execute_tool("restore_dependency_source", args, state)
    assert result.startswith("error:") and "冲突" in result
    assert not state.writes


def test_dependency_source_can_restore_an_empty_package_marker(tmp_path):
    state, args, wheel, _ = bundle_session(tmp_path)
    args.update(member="sample/__init__.py", path="vendor/__init__.py")
    result = execute_tool("restore_dependency_source", args, state)
    assert not result.startswith("error:"), result
    assert (state.workspace / args["path"]).read_bytes() == b""
    assert state.writes[-1]["dependency_source"]["overlaid_line_numbers"] == []



def test_dependency_provenance_survives_environment_materialization(tmp_path):
    from traceforge.reconstruction.env_replay import ReplayResult, ReplayedFile
    from traceforge.reconstruction.terminal_universe_environment import materialize_environment

    state, args, _, _ = bundle_session(tmp_path)
    result = execute_tool("restore_dependency_source", args, state)
    assert not result.startswith("error:"), result
    candidate = {"files": state.writes, "dependencies": [], "runtime_constraints": [], "uncertainties": []}
    replay = ReplayResult(
        files=(ReplayedFile(args["path"], state.partial_files[args["path"]], "seen", "PARTIAL"),),
        withheld_changes=(), partial_evidence=(), unknown_mutation_barriers=(),
    )
    manifest = materialize_environment(replay, candidate, tmp_path / "materialized",
                                       evidence_refs={"seen"})
    actual = manifest["provenance"][args["path"]]
    assert actual["dependency_source"] == state.writes[-1]["dependency_source"]


def test_complete_capture_cannot_be_expanded_by_upstream(tmp_path):
    state, args, _, _ = bundle_session(tmp_path)
    state.complete_files[args["path"]] = state.partial_files.pop(args["path"])
    result = execute_tool("restore_dependency_source", args, state)
    assert result.startswith("error:") and "完整原始" in result
    assert not state.writes



def test_tool_source_receipt_survives_model_redeclaring_same_file(tmp_path):
    from traceforge.reconstruction.agents.runtime import merge_completion_files

    state, args, _, _ = bundle_session(tmp_path)
    assert not execute_tool("restore_dependency_source", args, state).startswith("error:")
    payload = {"candidates": [{"files": [{"path": args["path"], "content": "model copy",
                                         "dependency_source": {"unverified": True}}]}]}
    merged = merge_completion_files(payload, state)["candidates"][0]["files"][0]
    assert merged["content"] == state.writes[-1]["content"]
    assert merged["dependency_source"] == state.writes[-1]["dependency_source"]


def test_formal_completion_excludes_post_mutation_reads_from_dependency_overlay(tmp_path):
    from traceforge.reconstruction.agents.runtime import AgentResult
    from traceforge.reconstruction.env_replay import replay_from_timeline
    from traceforge.reconstruction.workspace_completion import run_workspace_completion

    original, args, _, _ = bundle_session(tmp_path)
    def read(call_id, content, partial, numbers=None):
        op = {"kind": "read", "path": args["path"], "content": content, "partial": partial}
        if numbers is not None:
            op.update(line_numbers=numbers, line_contents=content.splitlines())
        return {"call_id": call_id, "name": "read", "result_text": content,
                "session_parse": {"file_ops": [op], "reason": "固定输入"}}
    timeline = [
        read("seen", "label = '原占位'\n", True, [2]),
        {"call_id": "requirements", "name": "read", "result_text": "sample-package==1.2\n",
         "session_parse": {"file_ops": [{"kind": "read", "path": "requirements.txt",
                                         "content": "sample-package==1.2\n", "partial": False}]}},
        {"call_id": "patch", "name": "write", "result_text": "written",
         "session_parse": {"file_ops": [{"kind": "write", "path": args["path"], "content": "ANSWER"}]}},
        read("after", "label = 'ANSWER'\n", True, [2]),
    ]
    from traceforge.reconstruction.session_parser import PARSER_SCHEMA

    for item in timeline:
        item["session_parse"]["schema_version"] = PARSER_SCHEMA
        for op in item["session_parse"]["file_ops"]:
            op["event_id"] = item["call_id"]
    replay = replay_from_timeline(timeline)

    class Author:
        model_name = "fixture"
        backend = "hermes-sandbox"

        def run(self, *, role, instruction, session, output_root):
            after = next(item for item in session.evidence if item["evidence_ref_id"] == "after")
            assert after["initial_state_eligible"] is False
            assert "ANSWER" in execute_tool(
                "read_evidence", {"id": "after", "path": args["path"]}, session)
            assert execute_tool("restore_observed_file", {
                "path": args["path"], "evidence_ref_ids": ["after"],
            }, session).startswith("error:")
            session.dependency_bundle = original.dependency_bundle
            result = execute_tool("restore_dependency_source", args, session)
            assert not result.startswith("error:"), result
            assert "ANSWER" not in session.writes[-1]["content"]
            return AgentResult(
                role=role.name, backend=self.backend, completed=True,
                payload={"candidates": [{"files": session.writes, "decision": "READY",
                                         "dependencies": [], "runtime_constraints": [], "uncertainties": []}]},
            )

    result = run_workspace_completion(
        task={"task_id": "t", "task_instruction": "检查 vendor/source.py",
              "initial_required_paths": [args["path"]]},
        replay=replay, timeline=timeline, agent=Author(), output_root=tmp_path / "completion",
        workspace_root=original.workspace,
    )
    assert result["status"] == "READY", result["errors"]
    row = result["candidates"][0]
    assert row["file_provenance"][0]["dependency_source"]["overlaid_line_numbers"] == [2]
    assert row["manifest"]["provenance"][args["path"]]["dependency_source"]["observed_line_count"] == 1


@pytest.mark.parametrize("line,expected", [
    ("value = 3", "RESTORED"), ("value = 9", "待核观察冲突"),
])
def test_dependency_checks_unknown_mutation_observation_without_promoting_it(
    tmp_path, line, expected,
):
    state, args, _, _ = bundle_session(tmp_path)
    state.evidence.append({
        "evidence_ref_id": "uncertain", "initial_state_eligible": False,
        "session_parse": {"file_ops": [], "reference_file_ops": [{
            "kind": "read", "path": args["path"], "partial": True,
            "line_numbers": [3], "line_contents": [line],
            "initial_state_blockers": [{"reason": "read_after_unparsed_mutation",
                                        "source_event_id": "uncertain", "path": args["path"]}],
        }]},
    })
    result = execute_tool("restore_dependency_source", args, state)
    assert expected in result, result
    if expected == "RESTORED":
        provenance = state.writes[-1]["dependency_source"]
        assert provenance["observed_line_count"] == 1
        assert provenance["unverified_observation_checks"][0]["evidence_ref_id"] == "uncertain"
        assert provenance["observation_evidence_ref_ids"] == ["seen"]
    else:
        assert "uncertain" in result and "3" in result and args["path"] in result
        assert not state.writes
    result = execute_tool("write_file", {
        "path": "inferred.py", "content": line + "\n", "evidence_ref_ids": ["uncertain"],
    }, state)
    assert not result.startswith("error:"), result
    assert state.writes[-1]["provenance"] == "MODEL_COMPLETED"
    assert state.writes[-1]["evidence_ref_ids"] == ["uncertain"]


def test_completion_retains_history_for_inference_without_direct_restoration(tmp_path):
    from traceforge.reconstruction.agents.runtime import AgentResult
    from traceforge.reconstruction.env_replay import replay_from_timeline
    from traceforge.reconstruction.session_parser import PARSER_SCHEMA
    from traceforge.reconstruction.workspace_completion import run_workspace_completion

    timeline = [
        {"call_id": "initial", "name": "read", "result_text": "value = 1\n",
         "session_parse": {"file_ops": [{"kind": "read", "path": "start.py",
                                         "partial": False, "content": "value = 1\n"}]}},
        {"call_id": "unknown", "name": "terminal", "result_text": "opaque execution",
         "session_parse": {"file_ops": [{"kind": "unknown", "may_mutate": True}]}},
        {"call_id": "later", "name": "read", "result_text": "value = 9\n",
         "session_parse": {"file_ops": [{"kind": "read", "path": "later.py", "partial": True,
                                         "content": "value = 9\n", "line_numbers": [3],
                                         "line_contents": ["value = 9"]}]}},
        {"name": "anonymous", "result_text": "anonymous body",
         "session_parse": {"file_ops": []}},
    ]
    for item in timeline:
        item["session_parse"]["schema_version"] = PARSER_SCHEMA
        for op in item["session_parse"]["file_ops"]:
            op["event_id"] = item.get("call_id", "anonymous")
    replay = replay_from_timeline(timeline)
    original = json.dumps(timeline, ensure_ascii=False, sort_keys=True)

    class Author:
        backend = "hermes-sandbox"
        model_name = "fixture"

        def run(self, *, role, instruction, session, output_root):
            assert len(session.evidence) == len(timeline)
            later = next(row for row in session.evidence if row["evidence_ref_id"] == "later")
            assert later["text"] == "value = 9\n"
            assert later["session_parse"]["file_ops"] == []
            page = execute_tool("read_evidence", {"id": "later", "path": "later.py"}, session)
            assert "value = 9" in page and "read_after_unparsed_mutation" in page
            assert "initial_state_eligible" in page
            assert execute_tool("restore_observed_file", {
                "path": "later.py", "evidence_ref_ids": ["later"],
            }, session).startswith("error:")
            result = execute_tool("write_file", {
                "path": "later.py", "content": "value = 9\n", "evidence_ref_ids": ["later"],
            }, session)
            assert not result.startswith("error:"), result
            # 模型推断如实引用历史，工具与最终候选采用相同引用资格。
            return AgentResult(role=role.name, backend=self.backend, completed=True, payload={
                "candidates": [{"decision": "READY", "files": [{"path": "later.py",
                    "content": "value = 9\n", "evidence_ref_ids": ["later"]}],
                    "dependencies": [], "runtime_constraints": [],
                    "uncertainties": [
                        "later.py：基于后续观察推断初态，时序尚待独立审查，未复制目标改动。",
                    ]}]})

    result = run_workspace_completion(
        task={"task_id": "t", "task_instruction": "检查源码"},
        replay=replay, timeline=timeline, agent=Author(), output_root=tmp_path / "completion")
    assert result["candidates"][0]["valid"]
    assert result["candidates"][0]["file_provenance"][0]["evidence_ref_ids"] == ["later"]
    assert result["candidates"][0]["file_provenance"][0]["provenance"] == "MODEL_COMPLETED"
    assert json.dumps(timeline, ensure_ascii=False, sort_keys=True) == original
