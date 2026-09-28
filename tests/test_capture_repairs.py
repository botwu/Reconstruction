"""采集修复在两条写入入口一致校验，原始证据不随候选改变。"""

import copy
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


@pytest.mark.parametrize("kind", ["COMPLETE", "UNKNOWN", "ABSENT"])
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


def test_capture_repairs_require_an_original_partial_path():
    session = AgentSession(allow_write=True, evidence=[{"evidence_ref_id": "read-original"}])
    tool = execute_tool("write_file", _file(), session)
    valid, errors = validate_completion_candidate(
        _candidate(_file()), ReplayResult((), (), (), ()), {"read-original"},
        env_origin="DEFAULT_EMPTY",
    )
    assert "CAPTURE_REPAIR_REQUIRES_PARTIAL" in tool
    assert not valid and "CAPTURE_REPAIR_REQUIRES_PARTIAL:parser.py" in errors


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
