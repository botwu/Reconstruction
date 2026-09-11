from __future__ import annotations

import json

from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse
from traceforge.reconstruction.sufficiency_judge import run_sufficiency_judge


class FakeModel:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request.request_id, request.model, "fake", json.dumps(self.payload), 1, 0.01
        )


def test_unknown_is_review(tmp_path):
    (tmp_path / "README.md").write_text("context", encoding="utf-8")
    out = run_sufficiency_judge(
        task={"objective": "x"},
        workspace_root=tmp_path,
        evidence=[{"evidence_ref_id": "e"}],
        model=FakeModel(
            {
                "label": "UNKNOWN",
                "reason": "unclear",
                "missing_context": ["x"],
                "evidence_ref_ids": ["e"],
                "confidence": 0.2,
                "decision": "REVIEW",
            }
        ),
        output_root=tmp_path / "out",
    )
    result = json.loads((out / "sufficiency_judgement.json").read_text())
    assert result["label"] == "UNKNOWN"
    assert result["decision"] == "REVIEW"


def test_unknown_evidence_forces_review(tmp_path):
    out = run_sufficiency_judge(
        task={},
        workspace_root=tmp_path,
        evidence=[{"evidence_ref_id": "e"}],
        model=FakeModel(
            {
                "label": "SUFFICIENT",
                "reason": "ok",
                "evidence_ref_ids": ["bad"],
                "confidence": 1,
                "decision": "READY",
            }
        ),
        output_root=tmp_path / "out",
    )
    result = json.loads((out / "sufficiency_judgement.json").read_text())
    assert result["decision"] == "REVIEW"
    assert "EVIDENCE_REF_UNKNOWN" in result["errors"]
