"""采集修复在两条写入入口一致校验，原始证据不随候选改变。"""

import copy
import hashlib
from pathlib import Path

import pytest

from traceforge.reconstruction.agents.runtime import AgentResult, merge_completion_files
from traceforge.reconstruction.agents.session import AgentSession, execute_tool, tool_schemas
from traceforge.reconstruction.terminal_universe_environment import (
    ReplayedFile,
    ReplayResult,
    validate_completion_candidate,
)
from traceforge.reconstruction.workspace_completion import (
    complete_from_replayed,
    completion_evidence_context,
    repair_workspace_completion,
)

ORIGINAL = 'from [PII_LIB] import Counter\nHEADER = "店铺"\n'
REPAIRS = [{"old_text": "[PII_LIB]", "new_text": "collections", "reason": "采集占位符损坏导入名"}]
CORRECTED = ORIGINAL.replace("[PII_LIB]", "collections")


def _replay(original=ORIGINAL, completeness="PARTIAL"):
    return ReplayResult((ReplayedFile("parser.py", original, "read-original", completeness),),
                        (), (), ())


def _file(content=CORRECTED, repairs=REPAIRS, *, refs=None):
    return {
        "path": "parser.py", "content": content, "provenance": "MODEL_COMPLETED",
        "evidence_ref_ids": ["read-original"] if refs is None else refs,
        "capture_repairs": copy.deepcopy(repairs),
    }


def _candidate(item):
    return {"files": [item], "dependencies": [], "runtime_constraints": [],
            "uncertainties": [], "decision": "READY"}


def _session(original=ORIGINAL):
    return AgentSession(
        allow_write=True, partial_files={"parser.py": original},
        replay_files={"parser.py": original}, evidence=[{"evidence_ref_id": "read-original"}],
    )


def _both_errors(original, item):
    session = _session(original)
    tool = execute_tool("write_file", item, session)
    valid, errors = validate_completion_candidate(_candidate(item), _replay(original),
                                                 {"read-original"})
    return tool, valid, errors, session


def test_declared_local_correction_is_accepted_by_tool_and_final_json():
    tool, valid, errors, session = _both_errors(ORIGINAL, _file())
    assert tool == "wrote parser.py"
    assert valid, errors
    assert session.writes[0]["capture_repairs"] == REPAIRS
    assert session.writes[0]["provenance"] == "MODEL_COMPLETED"
    assert session.partial_files["parser.py"] == ORIGINAL


def test_capture_command_applies_declared_changes_and_preserves_unedited_body():
    session = _session()
    result = execute_tool("repair_capture", {
        "path": "parser.py", "capture_repairs": REPAIRS,
        "append_content": "TAIL = 1\n", "evidence_ref_ids": ["read-original"],
    }, session)
    assert result == "wrote parser.py"
    assert session.writes[0]["content"] == CORRECTED + "TAIL = 1\n"
    assert session.writes[0]["capture_repairs"] == REPAIRS
    assert session.partial_files["parser.py"] == ORIGINAL
    valid, errors = validate_completion_candidate(
        _candidate(session.writes[0]), _replay(), {"read-original"},
    )
    assert valid, errors


def test_capture_command_keeps_complete_file_and_evidence_guards():
    session = _session()
    session.complete_files["parser.py"] = ORIGINAL
    args = {"path": "parser.py", "capture_repairs": REPAIRS,
            "evidence_ref_ids": ["read-original"]}
    assert "CAPTURE_REPAIR_CONTENT_MISMATCH" in execute_tool(
        "repair_capture", {**args, "append_content": "SOLVED = True\n"}, session,
    )
    assert "unknown evidence_ref_id" in execute_tool(
        "repair_capture", {**args, "evidence_ref_ids": ["missing"]}, session,
    )
    session.allow_write = False
    assert "cannot write files" in execute_tool("repair_capture", args, session)
    assert session.writes == []


def test_capture_command_retains_prior_repairs_and_tail_between_edits():
    session = _session()
    first = {"path": "parser.py", "capture_repairs": REPAIRS,
             "append_content": "TAIL = 1\n", "evidence_ref_ids": ["read-original"]}
    assert execute_tool("repair_capture", first, session) == "wrote parser.py"
    update = {"old_text": 'HEADER = "店铺"', "new_text": "HEADER = '店铺'",
              "reason": "修复采集引号，保持表头语义"}
    assert execute_tool("repair_capture", {
        "path": "parser.py", "capture_repairs": [update],
        "evidence_ref_ids": ["read-original"],
    }, session) == "wrote parser.py"
    latest = session.writes[-1]
    assert latest["content"] == CORRECTED.replace('"店铺"', "'店铺'") + "TAIL = 1\n"
    assert latest["capture_repairs"] == [*REPAIRS, update]
    assert session.partial_files["parser.py"] == ORIGINAL
    valid, errors = validate_completion_candidate(_candidate(latest), _replay(), {"read-original"})
    assert valid, errors


def test_capture_command_updates_same_anchor_and_preserves_seed_context():
    session = _session()
    session.prior_capture_repairs = {"parser.py": copy.deepcopy(REPAIRS)}
    session.replay_files["parser.py"] = "# prefix\n" + CORRECTED + "TAIL = 1\n"
    update = {**REPAIRS[0], "new_text": "collections", "reason": "新证据确认导入来源"}
    assert execute_tool("repair_capture", {
        "path": "parser.py", "capture_repairs": [update],
        "append_content": "TAIL = 2\n", "evidence_ref_ids": ["read-original"],
    }, session) == "wrote parser.py"
    assert session.writes[-1]["content"] == "# prefix\n" + CORRECTED + "TAIL = 2\n"
    assert session.writes[-1]["capture_repairs"] == [update]


def test_capture_command_rejects_conflicting_update_without_losing_prior_write():
    session = _session()
    args = {"path": "parser.py", "capture_repairs": REPAIRS,
            "evidence_ref_ids": ["read-original"]}
    assert execute_tool("repair_capture", args, session) == "wrote parser.py"
    previous = copy.deepcopy(session.writes)
    overlapping = {"old_text": "from [PII_LIB]", "new_text": "from os",
                   "reason": "与上一次修复范围冲突"}
    assert "CAPTURE_REPAIR_OVERLAP" in execute_tool(
        "repair_capture", {**args, "capture_repairs": [overlapping]}, session,
    )
    assert session.writes == previous


def test_rejected_write_identifies_undeclared_change_without_landing_it():
    tool, valid, errors, session = _both_errors(
        ORIGINAL, _file(CORRECTED.replace('HEADER = "店铺"', 'SOLVED = True')),
    )
    assert '-HEADER = "店铺"' in tool
    assert '+SOLVED = True' in tool
    assert not valid and session.writes == []
    assert "CAPTURE_REPAIR_CONTENT_MISMATCH:parser.py" in errors


@pytest.mark.parametrize("old,detail", [("missing", "没有找到"), ("a", "出现多次")])
def test_rejected_repair_identifies_missing_or_repeated_old_text(old, detail):
    tool, valid, _, session = _both_errors(
        "a a", _file("b b", [{"old_text": old, "new_text": "b", "reason": "采集损坏"}]),
    )
    assert detail in tool
    assert not valid and session.writes == []


@pytest.mark.parametrize("original,repairs,content,error", [
    (ORIGINAL, [], CORRECTED, "PARTIAL_OBSERVED_CONTENT_LOST"),
    (ORIGINAL, None, CORRECTED, "CAPTURE_REPAIRS_INVALID"),
    (ORIGINAL, [{"old_text": "", "new_text": "x", "reason": "采集损坏"}],
     CORRECTED, "CAPTURE_REPAIRS_INVALID"),
    (ORIGINAL, [{"old_text": "[PII_LIB]", "new_text": 1, "reason": "采集损坏"}],
     CORRECTED, "CAPTURE_REPAIRS_INVALID"),
    (ORIGINAL, [{"old_text": "[PII_LIB]", "new_text": "collections", "reason": " "}],
     CORRECTED, "CAPTURE_REPAIRS_INVALID"),
    (ORIGINAL, [{"old_text": "unknown", "new_text": "x", "reason": "采集损坏"}],
     CORRECTED, "CAPTURE_REPAIR_OLD_TEXT_NOT_UNIQUE"),
    ("aaa", [{"old_text": "aa", "new_text": "x", "reason": "采集损坏"}],
     "xa", "CAPTURE_REPAIR_OLD_TEXT_NOT_UNIQUE"),
    ("abcd", [{"old_text": "abc", "new_text": "x", "reason": "采集损坏"},
              {"old_text": "bcd", "new_text": "y", "reason": "采集损坏"}],
     "xy", "CAPTURE_REPAIR_OVERLAP"),
    ("bad", [{"old_text": "bad", "new_text": "", "reason": "采集损坏"}],
     "unrelated", "CAPTURE_REPAIR_EMPTY_RESULT"),
    (ORIGINAL, REPAIRS, CORRECTED.replace("HEADER", "SOLVED"), "CAPTURE_REPAIR_CONTENT_MISMATCH"),
])
def test_invalid_or_undeclared_changes_are_rejected_consistently(original, repairs, content, error):
    tool, valid, errors, session = _both_errors(original, _file(content, repairs))
    assert error in tool
    assert not valid
    assert f"{error}:parser.py" in errors
    assert session.writes == []


def test_unique_nonoverlapping_replacements_use_original_coordinates():
    original = "AAA beta CCC"
    repairs = [{"old_text": "CCC", "new_text": "AAA", "reason": "修复末段采集"},
               {"old_text": "AAA", "new_text": "alpha", "reason": "修复首段采集"}]
    tool, valid, errors, _ = _both_errors(
        original, _file("prefix\nalpha beta AAA\nsuffix", repairs),
    )
    assert tool == "wrote parser.py"
    assert valid, errors


def test_unknown_evidence_and_non_model_provenance_are_not_authorized():
    tool, valid, errors, _ = _both_errors(ORIGINAL, _file(refs=["unknown"]))
    assert "unknown evidence_ref_id" in tool
    assert not valid and "EVIDENCE_REF_UNKNOWN:parser.py" in errors
    item = _file()
    item["provenance"] = "NEIGHBOR"
    valid, errors = validate_completion_candidate(_candidate(item), _replay(), {"read-original"})
    assert not valid and "CAPTURE_REPAIR_PROVENANCE_INVALID:parser.py" in errors


@pytest.mark.parametrize("kind", ["UNKNOWN", "ABSENT"])
def test_capture_repairs_never_unlock_protected_paths(kind):
    replay = _replay(completeness=kind) if kind != "ABSENT" else ReplayResult(
        (), (), ({"path": "parser.py", "reason": "initial_read_not_found"},), (),
    )
    session = _session()
    session.protected_paths.add("parser.py")
    tool = execute_tool("write_file", _file(), session)
    valid, errors = validate_completion_candidate(_candidate(_file()), replay, {"read-original"})
    assert "PROTECTED_FILE_OVERWRITE" in tool
    assert not valid and "PROTECTED_FILE_OVERWRITE:parser.py" in errors


def test_capture_repairs_require_an_original_observation():
    session = AgentSession(allow_write=True, evidence=[{"evidence_ref_id": "read-original"}])
    tool = execute_tool("write_file", _file(), session)
    valid, errors = validate_completion_candidate(
        _candidate(_file()), ReplayResult((), (), (), ()), {"read-original"},
        env_origin="DEFAULT_EMPTY",
    )
    assert "CAPTURE_REPAIR_REQUIRES_OBSERVATION" in tool
    assert not valid and "CAPTURE_REPAIR_REQUIRES_OBSERVATION:parser.py" in errors


def test_complete_capture_repair_preserves_original_and_only_changes_declared_text(tmp_path):
    original = ORIGINAL.replace("\n", "\r\n")
    session = AgentSession(
        workspace=tmp_path, allow_write=True, protected_paths={"parser.py"},
        complete_files={"parser.py": original}, replay_files={"parser.py": original},
        evidence=[{"evidence_ref_id": "read-original"}],
    )
    assert execute_tool("write_file", _file(), session) == "wrote parser.py"
    valid, errors = validate_completion_candidate(
        _candidate(_file()), _replay(original, "COMPLETE"), {"read-original"},
    )
    assert valid, errors
    assert (tmp_path / "parser.py").read_text() == CORRECTED
    assert session.complete_files["parser.py"] == original
    assert session.writes[0]["capture_repairs"] == REPAIRS


def test_complete_revision_is_checked_against_original_not_previous_write():
    original = "left = [BAD_LEFT]\nright = [BAD_RIGHT]\n"
    first = [{"old_text": "[BAD_LEFT]", "new_text": "1", "reason": "修复左侧采集占位符"}]
    both = first + [{"old_text": "[BAD_RIGHT]", "new_text": "2", "reason": "修复右侧采集占位符"}]
    session = AgentSession(
        allow_write=True, protected_paths={"parser.py"}, complete_files={"parser.py": original},
        evidence=[{"evidence_ref_id": "read-original"}],
    )
    assert execute_tool("write_file", _file("left = 1\nright = [BAD_RIGHT]\n", first),
                        session) == "wrote parser.py"
    assert execute_tool("write_file", _file("left = 1\nright = 2\n", both),
                        session) == "wrote parser.py"
    assert "CAPTURE_REPAIR_CONTENT_MISMATCH" in execute_tool(
        "write_file", _file("left = 1\nright = 2\nSOLVED = True\n", both), session,
    )
    assert len(session.writes) == 2
    assert session.complete_files["parser.py"] == original


@pytest.mark.parametrize("content,repairs,error", [
    (CORRECTED, [], "PROTECTED_FILE_OVERWRITE"),
    (CORRECTED + "FEATURE_SOLVED = True\n", REPAIRS, "CAPTURE_REPAIR_CONTENT_MISMATCH"),
    (CORRECTED.replace('"店铺"', '"already solved"'), REPAIRS, "CAPTURE_REPAIR_CONTENT_MISMATCH"),
])
def test_complete_capture_rejects_undeclared_changes(content, repairs, error):
    session = AgentSession(
        allow_write=True, protected_paths={"parser.py"}, complete_files={"parser.py": ORIGINAL},
        evidence=[{"evidence_ref_id": "read-original"}],
    )
    item = _file(content, repairs)
    assert error in execute_tool("write_file", item, session)
    valid, errors = validate_completion_candidate(
        _candidate(item), _replay(completeness="COMPLETE"), {"read-original"},
    )
    assert not valid and f"{error}:parser.py" in errors
    assert session.writes == []


def test_capture_matching_handles_windows_newlines_without_altering_evidence():
    original = ORIGINAL.replace("\n", "\r\n")
    repairs = [{"old_text": ORIGINAL.splitlines()[0] + "\n",
                "new_text": CORRECTED.splitlines()[0] + "\n", "reason": "修复采集损伤"}]
    tool, valid, errors, session = _both_errors(original, _file(CORRECTED, repairs))
    assert tool == "wrote parser.py"
    assert valid, errors
    assert session.partial_files["parser.py"] == original


@pytest.mark.parametrize("same_content", [False, True])
@pytest.mark.parametrize("missing_metadata", [False, True])
def test_tool_capture_metadata_wins_when_merged_with_final_json(same_content, missing_metadata):
    session = _session()
    assert execute_tool("write_file", _file(), session) == "wrote parser.py"
    item = _file(CORRECTED if same_content else "stale body", [], refs=["unknown"])
    item["provenance"] = "NEIGHBOR"
    if missing_metadata:
        for key in ("provenance", "evidence_ref_ids", "capture_repairs"):
            item.pop(key)
    merged = merge_completion_files({"candidates": [_candidate(item)]}, session)
    actual = merged["candidates"][0]["files"][0]
    assert actual["capture_repairs"] == REPAIRS
    assert actual["content"] == CORRECTED
    assert validate_completion_candidate(merged["candidates"][0], _replay(), {"read-original"})[0]


class CompletionFixture:
    backend = "hermes-sandbox"
    model_name = "fixture"

    def __init__(self, mode, *, inherit=False):
        self.mode = mode
        self.inherit = inherit

    def run(self, *, role, instruction, session, output_root):
        self.instruction = instruction
        if self.mode == "noop":
            files = []
        else:
            item = _file(CORRECTED + ("SUPPORT_VERSION = 1\n" if self.inherit else ""))
            if self.inherit:
                item.pop("capture_repairs")
            if self.mode == "tool":
                result = execute_tool("write_file", item, session)
                assert result == "wrote parser.py", result
                files = []
            else:
                files = [item]
        return AgentResult(
            role=role.name, backend=self.backend, completed=True,
            payload={"candidates": [{"files": files, "dependencies": [], "runtime_constraints": [],
                                     "uncertainties": [], "decision": "READY"}]},
        )


@pytest.mark.parametrize("stale", [False, True])
def test_completion_hands_real_checks_to_sufficiency_with_version_binding(tmp_path: Path, stale: bool):
    class CheckedCompletion(CompletionFixture):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            checked_content = "previous version" if stale else CORRECTED
            kwargs["session"].environment_probes.append({
                "status": "FAIL", "purpose": "load", "python_code": "assert output.exists()",
                "error_code": "ENVIRONMENT_PROBE_NONZERO_EXIT", "record_path": "/checks/check-1.json",
                "candidate_sha256": "checked-version",
                "candidate_files": {"parser.py": hashlib.sha256(checked_content.encode()).hexdigest()},
                "executions": [{"exit_code": 1, "stdout": "", "stderr": "AssertionError: output missing"}],
            })
            return result

    result = complete_from_replayed(
        task={"task_instruction": "新增表头关键字匹配功能", "core_objective": "表头匹配"},
        replay=_replay(),
        timeline=[{"call_id": "read-original", "name": "read_file",
                   "arguments": {"path": "parser.py"}, "result_text": ORIGINAL}],
        agent=CheckedCompletion("tool"), output_root=tmp_path / "completion",
    )
    candidate = result["candidates"][0]
    checks = completion_evidence_context(_replay(), candidate)["candidate_execution_checks"]
    assert checks[0]["status"] == "FAIL"
    assert checks[0]["python_code"] == "assert output.exists()"
    assert checks[0]["candidate_sha256"] == "checked-version"
    assert checks[0]["checked_files_match"] is (not stale)
    assert checks[0]["changed_checked_paths"] == (["parser.py"] if stale else [])
    assert "output missing" in checks[0]["executions"][0]["stderr"]


@pytest.mark.parametrize("initial_mode", ["tool", "json"])
@pytest.mark.parametrize("repair_mode", ["noop", "tool", "json"])
def test_capture_metadata_survives_materialization_repair_and_judge_context(
    tmp_path: Path, initial_mode: str, repair_mode: str,
):
    replay = _replay()
    task = {"task_instruction": "新增表头关键字匹配功能", "core_objective": "表头匹配"}
    timeline = [{"call_id": "read-original", "name": "read_file",
                 "arguments": {"path": "parser.py"}, "result_text": ORIGINAL}]
    first = complete_from_replayed(
        task=task, replay=replay, timeline=timeline, agent=CompletionFixture(initial_mode),
        output_root=tmp_path / "initial",
    )
    assert first["status"] == "READY", first["errors"]
    candidate = first["candidates"][0]
    assert (Path(candidate["workspace"]) / "parser.py").read_text() == CORRECTED
    repaired = repair_workspace_completion(
        task=task, replay=replay, timeline=timeline, candidate=candidate,
        agent=CompletionFixture(repair_mode, inherit=True), output_root=tmp_path / "repair",
        feedback={"missing_context": ["检查基础依赖上下文"]},
    )
    assert repaired["status"] == "READY", repaired["errors"]
    result = repaired["candidates"][0]
    assert result["file_provenance"][0]["capture_repairs"] == REPAIRS
    context = completion_evidence_context(replay, result)
    assert context["candidate_completed_files"][0]["capture_repairs"] == REPAIRS
    assert context["candidate_provenance"]["parser.py"]["kind"] == "MODEL_COMPLETED"
    assert context["candidate_provenance"]["parser.py"]["capture_repairs"] == REPAIRS
    assert replay.files[0].content == ORIGINAL
    assert 'HEADER = "店铺"' in (Path(result["workspace"]) / "parser.py").read_text()


def test_write_schema_exposes_optional_capture_repair_declaration():
    schema = tool_schemas(("write_file",))[0]["function"]["parameters"]
    assert "capture_repairs" not in schema["required"]
    item = schema["properties"]["capture_repairs"]["items"]
    assert set(item["required"]) == {"old_text", "new_text", "reason"}


def test_omitting_declaration_preserves_legacy_substring_guard():
    item = _file()
    item.pop("capture_repairs")
    tool, valid, errors, _ = _both_errors(ORIGINAL, item)
    assert "PARTIAL_OBSERVED_CONTENT_LOST" in tool
    assert not valid and "PARTIAL_OBSERVED_CONTENT_LOST:parser.py" in errors


def test_cancelled_inherited_repairs_must_restore_original_excerpt():
    session = _session()
    session.prior_capture_repairs = {"parser.py": copy.deepcopy(REPAIRS)}
    assert "PARTIAL_OBSERVED_CONTENT_LOST" in execute_tool(
        "write_file", _file(CORRECTED, []), session,
    )
    assert execute_tool("write_file", _file(ORIGINAL, []), session) == "wrote parser.py"
    assert session.writes[0]["capture_repairs"] == []


def test_json_cannot_attach_unapplied_repairs_to_authoritative_tool_write():
    session = _session()
    item = _file(ORIGINAL)
    item.pop("capture_repairs")
    assert execute_tool("write_file", item, session) == "wrote parser.py"
    merged = merge_completion_files({"candidates": [_candidate(_file())]}, session)
    assert merged["candidates"][0]["files"][0]["content"] == ORIGINAL
    assert "capture_repairs" not in merged["candidates"][0]["files"][0]
