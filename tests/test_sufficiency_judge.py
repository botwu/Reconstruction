from __future__ import annotations

import json

import pytest

from traceforge.reconstruction.model_gateway import (
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
)
from traceforge.reconstruction.sufficiency_judge import run_sufficiency_judge


class FakeModel:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request.request_id, request.model, "fake", json.dumps(self.payload), 1, 0.01
        )


class FailingModel:
    def __init__(self, code: str):
        self.code = code

    def complete(self, request: ModelRequest) -> ModelResponse:
        raise ModelGatewayError("模型调用失败", code=self.code)


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


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [("NETWORK_TIMEOUT", "FAILED"), ("API_KEY_MISSING", "BLOCKED")],
)
def test_gateway_failure_publishes_auditable_judgement(tmp_path, code, expected_status):
    # 模型调用本身抛错时不能打穿 workflow：应发布可审计的失败判定，
    # decision=REVIEW（候选不会被选中），errors 带错误码，manifest 记 FAILED/BLOCKED。
    out = run_sufficiency_judge(
        task={"objective": "x"},
        workspace_root=tmp_path,
        evidence=[{"evidence_ref_id": "e"}],
        model=FailingModel(code),
        output_root=tmp_path / "out",
    )
    result = json.loads((out / "sufficiency_judgement.json").read_text())
    assert result["label"] == "UNKNOWN"
    assert result["decision"] == "REVIEW"
    assert code in result["errors"]
    manifest = json.loads((out / "artifact_manifest.json").read_text())
    assert manifest["status"] == expected_status
