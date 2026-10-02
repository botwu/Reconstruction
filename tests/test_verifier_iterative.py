import json
import pytest
from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse
from traceforge.verifier.iterative import synthesize_verifier_iterative
class M:
 def complete(self,r:ModelRequest)->ModelResponse:
  payload={"status":"READY","test_outputs_py":"def test_missing(): pass\ndef test_protective(): pass\ndef test_result(): pass","oracle_solutions":[{"name":"a","script":"echo a","justification":"a"},{"name":"b","script":"echo b","justification":"b"}],"mutation_solutions":[{"name":"m","script":"echo m","justification":"m"}],"missing_capability_tests":["test_missing"],"protective_tests":["test_protective"],"obligation_coverage":{"x":["test_result"]},"expected_value_strategy":"independent","open_questions":[]}
  return ModelResponse(r.request_id,r.model,'fake',json.dumps(payload),1,.01)
class E:
 def __init__(self): self.n=0
 def run(self,c): self.n+=1; return {"status":"FAIL","feedback":"fix"} if self.n==1 else {"status":"PASS","feedback":"ok"}
def test_loop_retries_with_feedback():
 r=synthesize_verifier_iterative(task={"acceptance_obligations":[{"id":"x","description":"x"}]},workspace_files={},model=M(),executor=E(),max_rounds=2)
 assert r.status=="READY" and len(r.attempts)==2


def test_explicit_budget_allows_four_calibration_rounds():
    class FourthPass(E):
        def run(self, candidate):
            self.n += 1
            return {"status": "PASS" if self.n == 4 else "FAIL",
                    "feedback": f"remaining {4-self.n}"}
    executor = FourthPass()
    result = synthesize_verifier_iterative(
        task={"acceptance_obligations": [{"id": "x", "description": "x"}]},
        workspace_files={}, model=M(), executor=executor, max_rounds=4,
    )
    assert result.status == "READY"
    assert executor.n == 4


@pytest.mark.parametrize("max_rounds", [0, -1, True, 1.5, "4"])
def test_iterative_verifier_rejects_invalid_round_budget(max_rounds):
    with pytest.raises(ValueError, match="正整数"):
        synthesize_verifier_iterative(
            task={}, workspace_files={}, model=M(), executor=E(), max_rounds=max_rounds
        )


def test_default_iterative_budget_allows_progress_past_six_rounds():
    class Progress(E):
        def run(self, candidate):
            self.n += 1
            return {"status": "PASS" if self.n == 8 else "FAIL", "feedback": f"case {self.n}"}
    executor = Progress()
    result = synthesize_verifier_iterative(
        task={"acceptance_obligations": [{"id": "x", "description": "x"}]},
        workspace_files={}, model=M(), executor=executor,
    )
    assert result.status == "READY"
    assert executor.n == 8


@pytest.mark.parametrize("status,expected_calls,error", [
    ("FAIL", 2, "VERIFIER_NO_PROGRESS"),
    ("INFRA_ERROR", 1, "VERIFIER_CALIBRATION_INFRA_ERROR"),
])
def test_iterative_stops_repeating_failure_or_infrastructure(status, expected_calls, error):
    class Failure(E):
        def run(self, candidate):
            self.n += 1
            return {"status": status, "feedback": "same actual failure"}
    executor = Failure()
    result = synthesize_verifier_iterative(
        task={"acceptance_obligations": [{"id": "x", "description": "x"}]},
        workspace_files={}, model=M(), executor=executor, max_rounds=4,
    )
    assert result.status == "REVIEW"
    assert executor.n == expected_calls
    assert error in result.open_questions


def test_iterative_explicit_budget_stops_progressing_repairs():
    class Progress(E):
        def run(self, candidate):
            self.n += 1
            return {"status": "FAIL", "feedback": f"case {self.n}"}
    executor = Progress()
    result = synthesize_verifier_iterative(
        task={"acceptance_obligations": [{"id": "x", "description": "x"}]},
        workspace_files={}, model=M(), executor=executor, max_rounds=3,
    )
    assert result.status == "REVIEW"
    assert executor.n == 3


def test_iterative_unstructured_synthesis_failure_blocks_without_false_no_progress():
    class Invalid(M):
        def complete(self, request):
            return ModelResponse(request.request_id, request.model, "fake",
                                 '{"status":"READY"}', 1, .01)
    result = synthesize_verifier_iterative(
        task={"acceptance_obligations": [{"id": "x", "description": "x"}]},
        workspace_files={}, model=Invalid(), executor=E(), max_rounds=4,
    )
    assert len(result.attempts) == 1
    assert result.open_questions == ("缺少 test_outputs_py",)
    assert "VERIFIER_NO_PROGRESS" not in result.open_questions
