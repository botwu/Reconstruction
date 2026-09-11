import json
from traceforge.trajectory.evidence_join import build_capture_input, build_task_input, build_query_task_input

def test_capture_and_query(tmp_path):
 p=tmp_path/'e.jsonl'; rows=[{'capture_occurrence_id':'c','event_occurrence_id':'u','event_kind':'USER','sequence_number':1,'source_json_pointer':'/messages/1','payload':{'content':{'value':'q'}}},{'capture_occurrence_id':'c','event_occurrence_id':'t','event_kind':'TOOL_CALL','sequence_number':2,'source_json_pointer':'/messages/2','payload':{}}]; p.write_text('\n'.join(json.dumps(x) for x in rows)); out=build_query_task_input(event_occurrences_path=p,capture_id='c',output_path=tmp_path/'o'); assert out['task_query_candidates']==['q'] and out['selection']['pending_tool_call_count']==1

def test_missing(tmp_path):
 p=tmp_path/'e'; p.write_text(''); r=tmp_path/'r'; r.write_text(json.dumps([{'source_id':'x'}])); out=build_task_input(evidence_path=r,event_occurrences_path=p,capture_id='c',output_path=tmp_path/'o'); assert out['quality']['requires_review']

def test_pending_tool_result_requires_review(tmp_path):
    p = tmp_path / 'e'
    p.write_text(json.dumps({'capture_occurrence_id':'c','event_occurrence_id':'u','event_kind':'USER','sequence_number':1,'source_json_pointer':'/m/1','payload':{'content':{'value':'q'}}})+'\n'+json.dumps({'capture_occurrence_id':'c','event_occurrence_id':'t','event_kind':'TOOL_CALL','sequence_number':2,'source_json_pointer':'/m/2','payload':{}}))
    out = build_query_task_input(event_occurrences_path=p, capture_id='c', output_path=tmp_path/'o')
    assert out['quality']['requires_review'] is True
    assert 'PENDING_TOOL_RESULT' in out['quality']['reason_codes']
