import json

import pytest

from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse
from traceforge.verifier.synthesis import VerifierSynthesisError, synthesize_verifier


class FakeModel:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request.request_id, request.model, "fake", json.dumps(self.payload), 1, 0.01
        )


def _task():
    return {"acceptance_obligations": [{"id": "output", "description": "生成结果"}]}


def _payload():
    return {
        "status": "READY",
        "test_outputs_py": "def test_missing(): pass\ndef test_protective(): pass\ndef test_output(): pass\n",  # noqa: E501
        "oracle_solutions": [
            {"name": "a", "script": "echo a", "justification": "a"},
            {"name": "b", "script": "echo b", "justification": "b"},
        ],
        "mutation_solutions": [{"name": "bad", "script": "echo bad", "justification": "bad"}],
        "missing_capability_tests": ["test_missing"],
        "protective_tests": ["test_protective"],
        "obligation_coverage": {"output": ["test_output"]},
        "expected_value_strategy": "独立计算",
        "open_questions": [],
    }


def test_synthesis_requires_known_test_references():
    payload = _payload()
    payload["missing_capability_tests"] = ["test_unknown"]
    with pytest.raises(VerifierSynthesisError):
        synthesize_verifier(task=_task(), workspace_files={}, model=FakeModel(payload))


def test_synthesis_returns_unvalidated_candidate_and_private_audit():
    candidate, audit = synthesize_verifier(
        task=_task(), workspace_files={"a.txt": "x"}, model=FakeModel(_payload())
    )
    assert candidate is not None and candidate.status == "UNVALIDATED"
    assert audit["raw_response"] and audit["prompt_sha256"]
