"""无响应义务时的空对象不阻断文件验证器，非空或必需合同继续校验。"""

import copy

import pytest
from test_reconstruction_verification import VerifierRuntime
from test_verifier_response_completion import run_recovery, task_fixture


class EmptyContractRuntime(VerifierRuntime):
    def __init__(self, proposal):
        super().__init__()
        self.proposal = proposal
        self.semantic_calls = 0

    def run(self, **kwargs):
        result = super().run(**kwargs)
        if kwargs["role"].result_schema == "traceforge.verifier-semantic-review.v1":
            self.semantic_calls += 1
        else:
            result.payload["response_contract"] = self.proposal
        return result


def file_task():
    return {
        "task_instruction": "更新 main.py 中的功能",
        "acceptance_obligations": [{"id": "obl-001", "text": "功能符合要求"}],
        "environment_bindings": [{
            "obligation_id": "obl-001", "verifier_kind": "FILE",
            "initial_required_paths": ["main.py"], "output_paths": [],
            "required_paths": ["main.py"],
        }],
        "response_contract": None,
    }


@pytest.mark.parametrize("proposal", [None, {}])
def test_absent_response_contract_allows_file_semantic_review(tmp_path, proposal):
    task = file_task()
    before = copy.deepcopy(task)
    runtime = EmptyContractRuntime(proposal)
    result, candidate = run_recovery(tmp_path, task, runtime)
    assert result["status"] == "READY"
    assert candidate is not None
    assert result["response_contract"] is None
    assert runtime.semantic_calls == 1
    assert task == before


@pytest.mark.parametrize("proposal", [{"checks": []}, {"unknown": True}])
def test_nonempty_ungrounded_contract_still_rejected(tmp_path, proposal):
    runtime = EmptyContractRuntime(proposal)
    result, candidate = run_recovery(tmp_path, file_task(), runtime)
    assert result["status"] == "REVIEW"
    assert candidate is None
    assert "RESPONSE_CONTRACT_UNGROUNDED" in result["errors"]
    assert runtime.semantic_calls == 0


def test_empty_contract_cannot_erase_required_response_checks(tmp_path):
    task = task_fixture()
    before = copy.deepcopy(task)
    result, candidate = run_recovery(tmp_path, task, EmptyContractRuntime({}))
    assert result["status"] == "REVIEW"
    assert candidate is None
    assert "RESPONSE_CONTRACT_UNGROUNDED" in result["errors"]
    assert any(error.startswith("RESPONSE_VERIFIER_MISSING:") for error in result["errors"])
    assert task == before
