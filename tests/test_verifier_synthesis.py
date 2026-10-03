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


def test_expected_value_strategy_reports_type_without_discarding_explanation():
    payload = _payload()
    payload["expected_value_strategy"] = {"test_output": "依据输入独立计算预期"}
    with pytest.raises(VerifierSynthesisError, match="expected_value_strategy 必须是非空字符串"):
        synthesize_verifier(task=_task(), workspace_files={}, model=FakeModel(payload))
    payload["expected_value_strategy"] = "test_output：依据输入独立计算预期"
    candidate, _ = synthesize_verifier(
        task=_task(), workspace_files={}, model=FakeModel(payload)
    )
    assert candidate.expected_value_strategy == payload["expected_value_strategy"]


def test_synthesis_returns_unvalidated_candidate_and_private_audit():
    candidate, audit = synthesize_verifier(
        task=_task(), workspace_files={"a.txt": "x"}, model=FakeModel(_payload())
    )
    assert candidate is not None and candidate.status == "UNVALIDATED"
    assert audit["raw_response"] and audit["prompt_sha256"]


def test_ready_may_omit_questions_but_explicit_invalid_questions_are_rejected():
    payload = _payload()
    del payload["open_questions"]
    candidate, _ = synthesize_verifier(task=_task(), workspace_files={}, model=FakeModel(payload))
    assert candidate.open_questions == ()
    payload["open_questions"] = "尚有问题"
    with pytest.raises(VerifierSynthesisError, match="open_questions"):
        synthesize_verifier(task=_task(), workspace_files={}, model=FakeModel(payload))
    with pytest.raises(VerifierSynthesisError, match="open_questions"):
        synthesize_verifier(task=_task(), workspace_files={}, model=FakeModel({"status": "REVIEW"}))


@pytest.mark.parametrize(
    ("field", "count", "expected"),
    [
        ("oracle_solutions", 1, 2),
        ("oracle_solutions", 3, 2),
        ("mutation_solutions", 0, 1),
        ("mutation_solutions", 2, 1),
    ],
)
def test_synthesis_requires_exact_solution_counts(field, count, expected):
    payload = _payload()
    payload[field] = [
        {"name": str(index), "script": f"echo {index}", "justification": "独立实现"}
        for index in range(count)
    ]
    with pytest.raises(VerifierSynthesisError, match=f"{field} 必须恰好包含 {expected} 个"):
        synthesize_verifier(task=_task(), workspace_files={}, model=FakeModel(payload))


@pytest.mark.parametrize("field", ["oracle_solutions", "mutation_solutions"])
@pytest.mark.parametrize("path", [
    "crates/fred-core/tests/task_identity.rs",
    "tests/test_output.py",
    "examples/solutions/main.py",
    "/home/user/workspace/crates/fred-core/tests/task_identity.rs",
    "/tests-data/input.txt",
    "/solution.py",
    "/solutions/main.py",
])
def test_solution_report_may_reference_public_paths(field, path):
    payload = _payload()
    payload[field][0]["script"] = (
        "from pathlib import Path\n"
        f"Path('review.md').write_text({path!r})\n"
    )
    candidate, _ = synthesize_verifier(
        task=_task(), workspace_files={}, model=FakeModel(payload)
    )
    assert candidate is not None
    variants = getattr(candidate, field)
    assert variants[0].script == payload[field][0]["script"]


@pytest.mark.parametrize("field", ["oracle_solutions", "mutation_solutions"])
@pytest.mark.parametrize("script", [
    "cat /tests",
    "cat /tests/test_outputs.py",
    "cat /solution",
    "python3 /solution/solve.py",
    "INPUT=/tests/control/truth.txt",
    "cat /./tests/test_outputs.py",
    "cat //solution/solve.py",
    "from pathlib import Path\nPath('/tests/test_outputs.py').read_text()",
    "print('/solution/solve.py')",
])
def test_solution_rejects_explicit_hidden_root_literals(field, script):
    payload = _payload()
    payload[field][0]["script"] = script
    with pytest.raises(VerifierSynthesisError, match="隐藏"):
        synthesize_verifier(task=_task(), workspace_files={}, model=FakeModel(payload))


def test_refactor_can_defer_structure_to_calibrated_file_semantics():
    payload = _payload()
    payload["missing_capability_tests"] = []
    payload["obligation_coverage"] = {"output": []}
    payload["file_semantic_checks"] = {
        "output": "实际业务移入新增模块，原入口消费该模块结果；空包装不满足要求。",
    }
    candidate, _ = synthesize_verifier(task=_task(), workspace_files={}, model=FakeModel(payload))
    assert candidate.missing_capability_tests == ()
    assert candidate.file_semantic_checks == payload["file_semantic_checks"]
    assert candidate.to_dict()["file_semantic_checks"] == payload["file_semantic_checks"]


@pytest.mark.parametrize("checks", [None, [], {"unknown": "检查"}, {"output": ""}, {"output": 1}])
def test_file_semantics_require_known_obligation_and_nonempty_criterion(checks):
    payload = _payload()
    payload["file_semantic_checks"] = checks
    with pytest.raises(VerifierSynthesisError, match="file_semantic_checks"):
        synthesize_verifier(task=_task(), workspace_files={}, model=FakeModel(payload))


def test_uncovered_obligation_cannot_use_another_obligations_semantics():
    task = {"acceptance_obligations": [{"id": "output"}, {"id": "other"}]}
    payload = _payload()
    payload["file_semantic_checks"] = {"output": "核对真正的模块提取"}
    payload["obligation_coverage"] = {"output": [], "other": []}
    with pytest.raises(VerifierSynthesisError, match="obligation_coverage"):
        synthesize_verifier(task=task, workspace_files={}, model=FakeModel(payload))


def test_behavior_only_candidate_still_requires_observable_missing_test():
    payload = _payload()
    payload["missing_capability_tests"] = []
    with pytest.raises(VerifierSynthesisError, match="missing_capability_tests"):
        synthesize_verifier(task=_task(), workspace_files={}, model=FakeModel(payload))
