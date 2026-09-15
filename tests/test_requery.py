from pathlib import Path
import json
import pytest
from traceforge.requery import *
from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse


class _SingleWorkspaceModel:
    def complete(self, request: ModelRequest) -> ModelResponse:
        tasks = [
            "Add deterministic validation for the parser entry point using the existing fixtures and preserve current CLI behavior.",
            "Extend the configuration loader to reject malformed values with stable local error output and add offline coverage.",
            "Improve the export command so its generated artifact preserves schema compatibility and handles an empty input.",
            "Add a backward-compatible cache invalidation path for the server adapter and verify it through existing local interfaces.",
            "Implement robust handling for missing client metadata in the import pipeline and validate the observable result offline.",
        ]
        return ModelResponse(request.request_id, request.model, "fake", json.dumps(tasks), 1, 0.01)


def test_single_workspace_emits_five_and_selects_one():
    result = synthesize_single_workspace_tasks(
        workspace_inventory=["src/app.py", "pyproject.toml"],
        workspace_files={"src/app.py": "def main(): pass"},
        model=_SingleWorkspaceModel(),
        selection_seed=7,
    )
    assert result.status == "READY"
    assert len(result.candidates) == 5
    assert result.selected_index in range(5)


def test_single_workspace_rejects_short_or_duplicate_candidates():
    class Model:
        def complete(self, request):
            values = ["too short", "too short", "x" * 45, "y" * 45, "z" * 45]
            return ModelResponse(request.request_id, request.model, "fake", json.dumps(values), 1, 0.01)

    result = synthesize_single_workspace_tasks(
        workspace_inventory=["src/app.py"], model=Model(), workspace_files=None
    )
    assert result.status == "REVIEW"
    assert result.selected_index is None

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
