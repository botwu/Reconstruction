from pathlib import Path
import json
import pytest
from traceforge.requery import *

def test_profile_and_directional_pair(tmp_path: Path):
    a=tmp_path/'a'; b=tmp_path/'b'; a.mkdir(); b.mkdir()
    (a/'api.py').write_text('api parser server config', encoding='utf8')
    (b/'client.py').write_text('client config', encoding='utf8')
    pa,pb=profile_workspace('a',a),profile_workspace('b',b)
    pairs=retrieve_directional_pairs([pa,pb],min_overlap=1)
    assert any(x[0].workspace_id=='a' and x[1].workspace_id=='b' for x in pairs)
    assert '/app/reference' in build_cross_workspace_prompt(pa,pb,('api',))

def test_profile_missing_workspace():
    with pytest.raises(ValueError): profile_workspace('missing','/no/such/path')

def test_tracker_and_verified_rounds():
    t=RequirementTracker(); t.apply([{'id':'r1','text':'add api'}])
    p=build_followup_prompt(t,RoundResult(0,'FAIL','fix it',('r1',)))
    assert 'add api' in p and 'fix it' in p
    assert retain_verified_session([RoundResult(0,'FAIL','',()),RoundResult(1,'PASS','',()),RoundResult(2,'PASS','',())])
    assert not retain_verified_session([RoundResult(1,'PASS','',())])

def test_sft_export_only_hard_pass(tmp_path: Path):
    out=tmp_path/'sft.jsonl'
    rows=[{'eligibility':'REVIEW','candidate_id':'x'}, {'eligibility':'ELIGIBLE','candidate_id':'c','task':{'q':'x'},'trajectory':[{'role':'assistant'}],'solution_leakage':False,'reproducible':True,'bundle_id':'b','rollout_id':'r','trial_id':'t'}]
    m=export_sft_jsonl(rows,out); assert m['exported_count']==1
    assert json.loads(out.read_text())['id']=='c'

def test_sft_export_rejects_unsafe_eligible(tmp_path: Path):
    with pytest.raises(SFTExportError): export_sft_jsonl([{'eligibility':'ELIGIBLE','candidate_id':'c','task':{},'trajectory':[],'solution_leakage':True,'reproducible':True}],tmp_path/'x')
