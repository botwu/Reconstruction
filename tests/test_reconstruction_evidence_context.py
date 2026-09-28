"""原始分段、当前候选与判断输入必须可区分，不把历史缺口变成新准入闸。"""

import hashlib
import json
from pathlib import Path

import pytest

from traceforge.reconstruction import eligible_reconstruction as pipeline
from traceforge.reconstruction import workspace_completion as completion
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency

SOURCE = "root_session.rs"


def replay_fixture():
    # 对应真实案例的六段覆盖与总行数，仅用人工文本验证范围传递。
    ranges = [(1, 1191), (1280, 1829), (1890, 2369), (2440, 2699),
              (3300, 3429), (2360, 2464)]
    timeline = []
    for index, (start, end) in enumerate(ranges):
        text = "\n".join(f"{n}: // observed {n}" for n in range(start, end + 1))
        timeline.append({
            "call_id": f"read-{index}", "name": "read",
            "arguments": {"path": SOURCE, "offset": start},
            "result_text": (
                f"<path>{SOURCE}</path>\n<type>file</type>\n<content>\n{text}\n"
                f"\n(Showing lines {start}-{end} of 4348. Use offset={end + 1} to continue.)\n</content>"
            ),
        })
    timeline.append({
        "call_id": "answer-write", "name": "write",
        "arguments": {"path": "review.md", "content": f"Final verdict about {SOURCE}"},
        "result_text": f"wrote review for {SOURCE}",
    })
    return replay_from_timeline(timeline), timeline


def test_hole_card_exposes_trusted_segments_without_answer_write():
    replay, timeline = replay_fixture()
    card = completion._hole_cards(
        replay, [{"path": SOURCE, "kind": "PARTIAL"}], timeline
    )[0]
    assert card["replay_materialized_ranges"] == [[1, 1191]]
    assert [row["ranges"] for row in card["observed_read_segments"]] == [
        [[1, 1191]], [[1280, 1829]], [[1890, 2369]], [[2440, 2699]],
        [[3300, 3429]], [[2360, 2464]],
    ]
    assert all(row["total_lines"] == 4348 for row in card["observed_read_segments"])
    assert card["excerpt_event_ids"] == [f"read-{i}" for i in range(6)]
    assert "answer-write" not in json.dumps(card)
    assert replay.files[0].content.count("\n") == 1191
    assert replay.files[0].completeness == "PARTIAL"


@pytest.mark.parametrize("mutation", [
    {"call_id": "source-write", "name": "write",
     "arguments": {"path": SOURCE, "content": "changed source"}, "result_text": "ok"},
    {"call_id": "unknown-mutation", "name": "bash",
     "arguments": {"command": "python -c 'change_files()'"}, "result_text": "ok"},
])
def test_post_mutation_read_never_becomes_trusted_segment(mutation):
    replay, timeline = replay_fixture()
    timeline.insert(1, mutation)
    replay = replay_from_timeline(timeline)
    card = completion._hole_cards(replay, [{"path": SOURCE, "kind": "PARTIAL"}], timeline)[0]
    assert [row["evidence_ref_id"] for row in card["observed_read_segments"]] == ["read-0"]
    assert card["excerpt_event_ids"] == ["read-0"]


class SufficientRuntime:
    backend = "hermes-sandbox"
    model_name = "fixture"

    def run(self, *, role, instruction, session, output_root):
        self.context = json.loads(
            instruction.split("RECONSTRUCTION_CONTEXT:\n", 1)[1]
            .split("\nSTATIC_INTEGRITY_REPORT:", 1)[0]
        )
        session.sandbox_started = session.sandbox_stopped = True
        session.read_only_probe_blocked = True
        session.tool_events.append({"name": "read_file", "ok": True})
        return AgentResult(
            role=role.name, backend=self.backend, completed=True,
            payload={"label": "SUFFICIENT", "decision": "READY", "confidence": 0.8,
                     "reason": "当前任务所需接口已具备，其余历史缺口不相关", "missing_context": []},
        )


def candidate_fixture(tmp_path, replay):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / SOURCE).write_text(replay.files[0].content)
    return {
        "workspace": str(workspace), "decision": "READY",
        "uncertainties": ["未物化的后段需按审查范围判断"],
        "dependencies": ["python3"], "runtime_constraints": ["只读审查，不需要构建"],
        "file_provenance": [],
        "manifest": {"provenance": {SOURCE: {"kind": "REPLAYED"}}},
    }


def test_sufficiency_sees_facts_without_automatically_rejecting_partial(tmp_path):
    replay, _ = replay_fixture()
    candidate = candidate_fixture(tmp_path, replay)
    runtime = SufficientRuntime()
    context = completion.completion_evidence_context(replay, candidate)
    result = run_workspace_sufficiency(
        task={"task_instruction": "审查已知接口"}, workspace_root=candidate["workspace"],
        agent=runtime, output_root=tmp_path / "judge", reconstruction_context=context,
    )
    assert result["status"] == "READY"
    assert runtime.context["uncertainties"] == candidate["uncertainties"]
    assert runtime.context["dependencies"] == ["python3"]
    assert runtime.context["runtime_constraints"] == ["只读审查，不需要构建"]
    assert runtime.context["candidate_provenance"][SOURCE]["kind"] == "REPLAYED"
    assert runtime.context["replay_files"][0]["current_matches_replay"] is True
    assert result["reconstruction_context"] == runtime.context


def test_repaired_candidate_hash_does_not_assert_old_gap_is_still_present(tmp_path):
    replay, _ = replay_fixture()
    candidate = candidate_fixture(tmp_path, replay)
    repaired = replay.files[0].content + "// 已补入任务相关上下文\n"
    (Path(candidate["workspace"]) / SOURCE).write_text(repaired)
    candidate["uncertainties"] = ["其余未观察内容与本次任务无关"]
    candidate["file_provenance"] = [{"path": SOURCE, "provenance": "MODEL_COMPLETED",
                                    "evidence_ref_ids": ["read-1"]}]
    candidate["manifest"]["provenance"][SOURCE]["kind"] = "MODEL_COMPLETED"
    runtime = SufficientRuntime()
    result = run_workspace_sufficiency(
        task={"task_instruction": "审查已补齐接口"}, workspace_root=candidate["workspace"],
        agent=runtime, output_root=tmp_path / "judge",
        reconstruction_context=completion.completion_evidence_context(replay, candidate),
    )
    row = runtime.context["replay_files"][0]
    assert row["replay_materialized_ranges"] == [[1, 1191]]
    assert row["current_matches_replay"] is False
    assert row["current_sha256"] == hashlib.sha256(repaired.encode()).hexdigest()
    assert runtime.context["candidate_completed_files"] == candidate["file_provenance"]
    assert result["status"] == "READY"
    assert result["missing_context"] == []


def test_repair_loop_passes_each_current_candidates_metadata(tmp_path, monkeypatch):
    replay, timeline = replay_fixture()
    candidate = candidate_fixture(tmp_path, replay)
    repaired = {**candidate, "uncertainties": ["修复后的剩余限制"],
                "dependencies": [], "file_provenance": [{"path": SOURCE}]}
    contexts = []

    def judge(**kwargs):
        contexts.append(kwargs["reconstruction_context"])
        ready = len(contexts) > 1
        return {"status": "READY" if ready else "REVIEW",
                "missing_context": [] if ready else ["需要任务相关后段"], "errors": []}

    def environment(**kwargs):
        return {"status": "READY" if len(contexts) > 1 else "REVIEW",
                "execution_readiness": "PROBED", "workspace_sha256": str(len(contexts))}

    monkeypatch.setattr(pipeline, "run_workspace_sufficiency", judge)
    monkeypatch.setattr(pipeline, "build_environment_contract", environment)
    monkeypatch.setattr(pipeline, "repair_workspace_completion",
                        lambda **kwargs: {"status": "READY", "candidates": [repaired]})
    _, _, _, audit = pipeline._judge_and_repair_candidate(
        task={"task_instruction": "审查源码"}, candidate=candidate, replay=replay,
        timeline=timeline, task_source={}, agent=SufficientRuntime(), task_root=tmp_path / "task",
        index=0, origin="REPLAYED",
    )
    assert audit["stop_reason"] == "READY"
    assert contexts[0]["uncertainties"] == candidate["uncertainties"]
    assert contexts[1]["uncertainties"] == repaired["uncertainties"]
    assert contexts[1]["dependencies"] == []
    assert contexts[1]["candidate_completed_files"] == repaired["file_provenance"]
