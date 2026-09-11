"""M4 映射索引的纯校验函数。"""
from .mapping_contracts import MappingIndexV1

def validate_mapping_index(index: MappingIndexV1) -> None:
    episode_ids={e.episode_ref for e in index.episodes}
    if len(episode_ids)!=len(index.episodes): raise ValueError("episode_ref 重复")
    attempt_ids={a.attempt_ref for a in index.attempts}
    if len(attempt_ids)!=len(index.attempts): raise ValueError("attempt_ref 重复")
    if any(a.episode_ref not in episode_ids for a in index.attempts): raise ValueError("attempt_ref 引用不存在 episode_ref")
    if any(e.semantic_status != "STRUCTURAL_ONLY" for e in index.episodes): raise ValueError("当前版本仅允许 STRUCTURAL_ONLY episode")
