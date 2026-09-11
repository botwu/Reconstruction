import json
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
