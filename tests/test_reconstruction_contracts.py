from pathlib import Path
import pytest
from traceforge.reconstruction.contracts import *

def test_recovery_validation():
    x=TaskRecoveryV1(TASK_RECOVERY_SCHEMA,'r','a','s','title','do x','intent',({'id':'o'},),(),(),('secrets',),(),.9,'READY')
    validate_task_recovery(x)
    with pytest.raises(ValueError): validate_task_recovery(TaskRecoveryV1(TASK_RECOVERY_SCHEMA,'r','a','s','t','','i',(),(),(),(),(),.9,'READY'))

def test_manifest_visibility(tmp_path):
    (tmp_path/'task.toml').write_text('x'); (tmp_path/'instruction.md').write_text('x')
    for d in ('workspace','environment','solution','tests/control'): (tmp_path/d).mkdir(parents=True,exist_ok=True)
    (tmp_path/'workspace/a').write_text('1'); (tmp_path/'environment/e').write_text('2'); (tmp_path/'solution/s').write_text('3'); (tmp_path/'tests/grader.py').write_text('g'); (tmp_path/'tests/control/g').write_text('h')
    m=harbor_bundle_manifest(bundle_root=tmp_path,bundle_id='b',task_name='t',task_recovery_id='tr',environment_recovery_id='er',source_attempt_ref='a')
    assert 'workspace/a' in m.public_paths and 'tests/control/g' in m.hidden_control_paths and 'tests/grader.py' in m.hidden_verifier_paths
    assert len(m.bundle_sha256)==64

def test_rollout_and_verifier_bounds():
    req=RolloutRequestV1(ROLLOUT_REQUEST_SCHEMA,'r','b','hermes','m','p',2,0.2,60,(1,2)); validate_rollout_request(req)
    with pytest.raises(ValueError): validate_rollout_request(RolloutRequestV1(ROLLOUT_REQUEST_SCHEMA,'r','b','h','m','p',2,-1,60,(1,2)))
    v=VerificationResultV1(VERIFICATION_RESULT_SCHEMA,'v','b','t','PASS',1.0,{'task':1.0},(),(),(),None,None); validate_verification_result(v)
