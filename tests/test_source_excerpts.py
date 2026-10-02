import json
from dataclasses import replace
from pathlib import Path

import pytest

from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.terminal_universe_environment import (
    EnvironmentReconstructionError,
    materialize_environment,
)


def _read(event: str, start: int, end: int, total: int, *, body: str | None = None) -> dict:
    return {
        "call_id": event, "name": "read",
        "arguments": {"path": "/work/main.py", "offset": start, "limit": end - start + 1},
        "result_text": (body or "\n".join(f"{n}#AB:source-{n}" for n in range(start, end + 1)))
        + f"\n\n[Showing lines {start}-{end} of {total}.]",
    }


def _materialize(tmp_path: Path, timeline: list[dict]) -> tuple[object, Path]:
    replay = replay_from_timeline(timeline)
    materialize_environment(
        replay, {"files": [], "decision": "READY"}, tmp_path / "env",
        evidence_refs={item["call_id"] for item in timeline},
    )
    return replay, tmp_path / "env/workspace"


def test_public_source_excerpts_keep_later_lines_and_explicit_gaps(tmp_path: Path) -> None:
    replay, workspace = _materialize(tmp_path, [_read("first", 1, 2, 6), _read("later", 4, 5, 6)])
    source = json.loads((workspace / ".traceforge/source-excerpts.json").read_text())["files"][0]
    assert source["path"] == replay.files[0].path
    assert source["total_lines"] == 6
    assert source["missing_line_ranges"] == [[3, 3], [6, 6]]
    later = source["segments"][1]
    assert later["source_event_id"] == "later"
    assert (later["line_start"], later["line_end"]) == (4, 5)
    assert (workspace / later["excerpt_path"]).read_text().endswith("4: source-4\n5: source-5\n")
    assert (workspace / replay.files[0].path).read_text() == "source-1\nsource-2\n"
    manifest = json.loads((workspace.parent / "env_manifest.json").read_text())
    assert manifest["provenance"][".traceforge/source-excerpts.json"]["evidence_ref_ids"] == ["first", "later"]
    assert manifest["provenance"][later["excerpt_path"]]["evidence_ref_ids"] == ["later"]


@pytest.mark.parametrize("barrier", [
    {"call_id": "write", "name": "write", "arguments": {"path": "/work/main.py", "content": "HIDDEN_ANSWER"}},
    {"call_id": "unknown", "name": "bash", "arguments": {"command": "python mutate.py"}},
])
def test_public_source_excerpts_exclude_after_mutation_and_hidden_answer(tmp_path: Path, barrier: dict) -> None:
    replay, workspace = _materialize(tmp_path, [
        _read("first", 1, 2, 6), barrier, _read("after", 4, 5, 6, body="4#AB:HIDDEN_ANSWER\n5#AB:HIDDEN_ANSWER"),
    ])
    public = (workspace / ".traceforge/source-excerpts.json").read_text()
    assert all("HIDDEN_ANSWER" not in item.read_text() for item in (workspace / ".traceforge").rglob("*") if item.is_file())
    assert "after" not in public
    assert json.loads(public)["files"][0]["missing_line_ranges"] == [[3, 6]]


@pytest.mark.parametrize("later", [
    _read("conflict", 2, 3, 4, body="2#AB:DIFFERENT\n3#AB:source-3"),
    _read("total_conflict", 3, 4, 5),
])
def test_conflicting_source_excerpts_are_not_published(tmp_path: Path, later: dict) -> None:
    _, workspace = _materialize(tmp_path, [_read("first", 1, 2, 4), later])
    assert not (workspace / ".traceforge/source-excerpts.json").exists()


def test_complete_sources_need_no_public_partial_artifact(tmp_path: Path) -> None:
    _, workspace = _materialize(tmp_path, [_read("full", 1, 2, 2)])
    assert not (workspace / ".traceforge/source-excerpts.json").exists()


def test_untrusted_segment_reference_is_not_published(tmp_path: Path) -> None:
    replay = replay_from_timeline([_read("unknown", 1, 2, 4)])
    materialize_environment(replay, {"files": []}, tmp_path / "env", evidence_refs=set())
    assert not (tmp_path / "env/workspace/.traceforge/source-excerpts.json").exists()


def test_public_source_excerpt_path_collision_fails(tmp_path: Path) -> None:
    replay = replay_from_timeline([_read("first", 1, 2, 4)])
    candidate = {"files": [{
        "path": ".traceforge/source-excerpts.json", "content": "existing user content",
        "evidence_ref_ids": ["first"],
    }]}
    with pytest.raises(EnvironmentReconstructionError, match="SOURCE_EXCERPTS_PATH_COLLISION"):
        materialize_environment(replay, candidate, tmp_path / "env", evidence_refs={"first"})


def test_hidden_control_cannot_enter_public_source_excerpts(tmp_path: Path) -> None:
    replay = replay_from_timeline([_read("first", 1, 2, 4)])
    hidden = "tests/control/answer.py"
    replay = replace(
        replay,
        files=(replace(replay.files[0], path=hidden),),
        partial_evidence=tuple({**row, "path": hidden} for row in replay.partial_evidence),
    )
    with pytest.raises(EnvironmentReconstructionError, match="hidden path"):
        materialize_environment(replay, {"files": []}, tmp_path / "env", evidence_refs={"first"})
    assert not (tmp_path / "env").exists()


def test_source_excerpt_index_allows_direct_read_of_later_range(tmp_path: Path) -> None:
    from traceforge.reconstruction.agents.session import AgentSession, execute_tool

    _, workspace = _materialize(tmp_path, [
        _read("first", 1, 1500, 3000), _read("later", 2000, 2100, 3000),
    ])
    session = AgentSession(workspace=workspace, allow_write=False)
    index = json.loads(execute_tool("read_file", {"path": ".traceforge/source-excerpts.json"}, session))
    later = next(segment for segment in index["files"][0]["segments"] if segment["line_start"] == 2000)
    text = execute_tool("read_file", {"path": later["excerpt_path"]}, session)
    assert "2000: source-2000" in text
    assert "2100: source-2100" in text
    assert "continued; use offset" not in text


def test_replayed_provenance_locates_all_observed_ranges(tmp_path: Path) -> None:
    replay, workspace = _materialize(tmp_path, [
        _read("first", 3, 4, 10), _read("later", 8, 9, 10),
    ])
    manifest = json.loads((workspace.parent / "env_manifest.json").read_text())
    row = manifest["provenance"][replay.files[0].path]
    assert row["replay_completeness"] == "PARTIAL"
    index = json.loads((workspace / row["source_excerpts_ref"]).read_text())
    source = next(item for item in index["files"] if item["path"] == replay.files[0].path)
    segments = source["segments"]
    assert [(item["line_start"], item["line_end"]) for item in segments] == [(3, 4), (8, 9)]
    assert (workspace / replay.files[0].path).read_text() == "source-3\nsource-4\n"
    assert "8: source-8" in (workspace / segments[1]["excerpt_path"]).read_text()


def test_completed_candidate_keeps_original_observation_scope(tmp_path: Path) -> None:
    timeline = [_read("first", 3, 4, 6), _read("later", 5, 6, 6)]
    replay = replay_from_timeline(timeline)
    content = "\n".join(f"source-{n}" for n in range(1, 7)) + "\n"
    manifest = materialize_environment(
        replay, {"files": [{
            "path": replay.files[0].path, "content": content,
            "evidence_ref_ids": ["first", "later"],
        }], "decision": "READY"}, tmp_path / "env", evidence_refs={"first", "later"},
    )
    row = manifest["provenance"][replay.files[0].path]
    assert row["kind"] == "MODEL_COMPLETED"
    assert row["replay_completeness"] == "PARTIAL"
    workspace = tmp_path / "env/workspace"
    assert (workspace / replay.files[0].path).read_text() == content
    index = json.loads((workspace / row["source_excerpts_ref"]).read_text())
    assert index["files"][0]["missing_line_ranges"] == [[1, 2]]


def test_conflicting_observations_have_no_fabricated_source_reference(tmp_path: Path) -> None:
    replay, workspace = _materialize(tmp_path, [
        _read("first", 1, 2, 4), _read("conflict", 2, 3, 4, body="2#AB:DIFFERENT\n3#AB:source-3"),
    ])
    manifest = json.loads((workspace.parent / "env_manifest.json").read_text())
    row = manifest["provenance"][replay.files[0].path]
    assert row["replay_completeness"] == "PARTIAL"
    assert "source_excerpts_ref" not in row
    assert not (workspace / ".traceforge/source-excerpts.json").exists()


def test_complete_provenance_does_not_imply_missing_excerpts(tmp_path: Path) -> None:
    replay, workspace = _materialize(tmp_path, [_read("full", 1, 2, 2)])
    manifest = json.loads((workspace.parent / "env_manifest.json").read_text())
    row = manifest["provenance"][replay.files[0].path]
    assert row["replay_completeness"] == "COMPLETE"
    assert "source_excerpts_ref" not in row
