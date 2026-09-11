"""读取 M1D QueryTurn 图并建立 M4 可消费的 episode/attempt 外键。"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from traceforge.trajectory.json_codec import sha256_bytes
from traceforge.query_turns.validation import validate_query_turn_run
from .mapping_contracts import (ATTEMPT_REF_SCHEMA, EPISODE_REF_SCHEMA, MAPPING_INDEX_SCHEMA, M4_MAPPING_CONTRACT_VERSION, AttemptRefV1, EpisodeRefV1, MappingBasis, MappingIndexV1, SemanticStatus, attempt_ref_id, episode_ref_id)
class MappingInputError(RuntimeError):
    """M1D 缺失、未通过绑定校验或结构字段非法。"""
def _rows(root: Path, relative: str) -> list[dict[str, Any]]:
    path = root / relative
    if not path.is_file(): raise MappingInputError(f"输入 M1D 缺少必需表：{relative}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MappingInputError(f"无法读取 M1D 表：{relative}") from exc
    rows=[]
    for line in lines:
        try:
            value=json.loads(line)
        except json.JSONDecodeError as exc:
            raise MappingInputError(f"M1D 表 JSON 非法：{relative}") from exc
        if not isinstance(value,dict): raise MappingInputError(f"M1D 表记录不是对象：{relative}")
        rows.append(value)
    return rows
def _str(row: dict[str, Any], key: str, relative: str) -> str:
    value=row.get(key)
    if not isinstance(value,str): raise MappingInputError(f"字段类型非法：{relative}/{key}")
    return value
def _tuple(row: dict[str, Any], key: str, relative: str) -> tuple[str,...]:
    value=row.get(key)
    if not isinstance(value,list) or not all(isinstance(x,str) for x in value): raise MappingInputError(f"字段类型非法：{relative}/{key}")
    return tuple(value)
def load_mapping_index(*, m1d_run_dir: str|Path, m1b_run_dir: str|Path) -> MappingIndexV1:
    root=Path(m1d_run_dir)
    if not root.is_dir(): raise MappingInputError("输入 M1D run 不是目录")
    validation=validate_query_turn_run(root,Path(m1b_run_dir))
    if not validation.ok:
        codes=",".join(sorted({i.code for i in validation.issues})); raise MappingInputError(f"输入 M1D run 未通过绑定校验：{codes}")
    manifest_path=root/"artifact_manifest.json"
    try:
        manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MappingInputError("无法读取 M1D artifact_manifest") from exc
    if not isinstance(manifest,dict): raise MappingInputError("M1D artifact_manifest 不是对象")
    m1d_run_id=_str(manifest,"query_turn_run_id","artifact_manifest.json")
    if root.name != m1d_run_id: raise MappingInputError("M1D 目录名与 query_turn_run_id 不一致")
    m1b_run_id=_str(manifest,"m1b_run_id","artifact_manifest.json")
    turns=_rows(root,"private/query_turns.jsonl"); steps=_rows(root,"private/agent_steps.jsonl")
    steps_by_id={_str(row,"agent_step_id","agent_steps"): row for row in steps}
    episodes=[]; attempts=[]; sessions=[]
    key=lambda x: (_str(x,"capture_occurrence_id","query_turns"), x.get("turn_ordinal",0))
    for turn in sorted(turns,key=key):
        qid=_str(turn,"query_turn_id","query_turns"); capture=_str(turn,"capture_occurrence_id","query_turns"); ordinal=turn.get("turn_ordinal")
        if not isinstance(ordinal,int) or isinstance(ordinal,bool) or ordinal<0: raise MappingInputError("query_turns/turn_ordinal 类型非法")
        user_block=turn.get("user_block_id")
        if user_block is not None and not isinstance(user_block,str): raise MappingInputError("query_turns/user_block_id 类型非法")
        boundaries=_tuple(turn,"boundary_ids_spanned","query_turns"); session_ref=f"session:{capture}"; episode_id=episode_ref_id(m1d_run_id=m1d_run_id,query_turn_id=qid)
        episodes.append(EpisodeRefV1(EPISODE_REF_SCHEMA,episode_id,session_ref,qid,capture,ordinal,user_block,boundaries,MappingBasis.QUERY_TURN.value,SemanticStatus.STRUCTURAL_ONLY.value))
        ids=turn.get("agent_step_ids")
        if not isinstance(ids,list) or not all(isinstance(x,str) for x in ids): raise MappingInputError("query_turns/agent_step_ids 类型非法")
        turn_steps=[steps_by_id[x] for x in ids if x in steps_by_id]
        if len(turn_steps) != len(ids): raise MappingInputError("query_turns 引用了不存在的 agent_step_id")
        if not turn_steps: turn_steps=[{"agent_step_id":None}]
        for step_ordinal,step in enumerate(turn_steps):
            aid=step.get("agent_step_id"); assistant=step.get("assistant_event_id")
            if aid is not None and not isinstance(aid,str): raise MappingInputError("agent_steps/agent_step_id 类型非法")
            if assistant is not None and not isinstance(assistant,str): raise MappingInputError("agent_steps/assistant_event_id 类型非法")
            attempts.append(AttemptRefV1(ATTEMPT_REF_SCHEMA,attempt_ref_id(m1d_run_id=m1d_run_id,query_turn_id=qid,agent_step_id=aid,step_ordinal=step_ordinal),episode_id,session_ref,qid,aid,assistant,step_ordinal,_str(turn,"turn_status","query_turns"),MappingBasis.AGENT_STEP.value if aid else MappingBasis.TURN_FALLBACK.value,SemanticStatus.STRUCTURAL_ONLY.value))
        sessions.append({"session_ref":session_ref,"capture_occurrence_id":capture,"semantic_status":SemanticStatus.STRUCTURAL_ONLY.value})
    return MappingIndexV1(MAPPING_INDEX_SCHEMA,M4_MAPPING_CONTRACT_VERSION,m1d_run_id,sha256_bytes(manifest_path.read_bytes()),m1b_run_id,tuple(sorted(sessions,key=lambda x:str(x["session_ref"]))),tuple(episodes),tuple(attempts))
