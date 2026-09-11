from __future__ import annotations

import json

from traceforge.reconstruction.environment_completion import run_environment_completion
from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse
from traceforge.reconstruction.task_recovery import run_task_recovery


class FakeModel:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request.request_id, request.model, "fake", json.dumps(self.payload), 1, 0.01
        )


def test_task_runner_persists_private_exchange(tmp_path):
    payload = {
        "candidates": [
            {
                "task_title": "t",
                "task_instruction": "do",
                "user_intent": "i",
                "acceptance_obligations": [],
                "explicit_constraints": [],
                "ambiguities": [],
                "do_not_infer": [],
                "evidence": [{"evidence_ref_id": "e", "role": "observed", "source_pointer": "x"}],
                "confidence": 0.8,
                "decision": "READY",
            }
        ],
        "open_questions": [],
    }
    out = run_task_recovery(
        attempt_ref="a",
        source_report_id="r",
        report={},
        evidence=[{"evidence_ref_id": "e"}],
        model=FakeModel(payload),
        output_root=tmp_path,
    )
    assert (out / "task_recovery.json").is_file()
    private = json.loads((out / "private/model_exchange.json").read_text())
    assert private["credentials_embedded"] is False
    assert private["request"]["prompt_sha256"]


def test_environment_completion_materializes_content(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "README.md").write_text("partial", encoding="utf-8")
    payload = {
        "candidates": [
            {
                "files": [
                    {
                        "path": "config.json",
                        "content": "{}",
                        "provenance": "MODEL_COMPLETED",
                        "evidence_ref_ids": ["e"],
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
    out = run_environment_completion(
        task={"recovery_id": "t"},
        attempt_ref="a",
        replay_workspace=replay,
        replay_files=[{"path": "README.md", "completeness": "PARTIAL"}],
        evidence=[{"evidence_ref_id": "e"}],
        model=FakeModel(payload),
        output_root=tmp_path / "out",
    )
    record = json.loads((out / "environment_completion.json").read_text())
    assert record["candidates"][0]["status"] == "READY"
    assert record["candidates"][0]["files"][1]["content"] == "{}"


def test_environment_completion_rejects_complete_overwrite(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "a.txt").write_text("original", encoding="utf-8")
    payload = {
        "candidates": [
            {
                "files": [{"path": "a.txt", "content": "changed", "evidence_ref_ids": ["e"]}],
                "decision": "READY",
            }
        ]
    }
    out = run_environment_completion(
        task={},
        attempt_ref="a",
        replay_workspace=replay,
        replay_files=[{"path": "a.txt", "completeness": "COMPLETE"}],
        evidence=[{"evidence_ref_id": "e"}],
        model=FakeModel(payload),
        output_root=tmp_path / "out",
    )
    record = json.loads((out / "environment_completion.json").read_text())
    assert record["candidates"][0]["status"] == "REVIEW"
    assert "PROTECTED_FILE_OVERWRITE:a.txt" in record["candidates"][0]["errors"]
