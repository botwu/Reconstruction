"""M4 映射索引的纯校验函数。"""

from __future__ import annotations

from .mapping_contracts import (
    AttemptRefV1,
    EpisodeRefV1,
    MappingBasis,
    MappingIndexV1,
    SemanticStatus,
)


def _ensure_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} 重复")


def _validate_episode(episode: EpisodeRefV1) -> None:
    MappingBasis(episode.mapping_basis)
    SemanticStatus(episode.semantic_status)
    if episode.turn_ordinal < 0:
        raise ValueError("episode.turn_ordinal 必须为非负整数")
    if not episode.episode_ref or not episode.query_turn_id:
        raise ValueError("episode 必须包含 episode_ref 和 query_turn_id")


def _validate_attempt(attempt: AttemptRefV1, episode_ids: set[str]) -> None:
    MappingBasis(attempt.mapping_basis)
    SemanticStatus(attempt.semantic_status)
    if attempt.episode_ref not in episode_ids:
        raise ValueError("attempt_ref 引用不存在 episode_ref")
    if attempt.step_ordinal < 0:
        raise ValueError("attempt.step_ordinal 必须为非负整数")
    if not attempt.attempt_ref or not attempt.query_turn_id:
        raise ValueError("attempt 必须包含 attempt_ref 和 query_turn_id")
    if attempt.mapping_basis == MappingBasis.AGENT_STEP.value:
        if attempt.agent_step_id is None:
            raise ValueError("AGENT_STEP 映射必须包含 agent_step_id")
    elif attempt.mapping_basis == MappingBasis.TURN_FALLBACK.value:
        if attempt.agent_step_id is not None:
            raise ValueError("TURN_FALLBACK 映射不能包含 agent_step_id")


def validate_mapping_index(index: MappingIndexV1) -> None:
    """校验 M1D→M4 映射完整性，不推断缺失的 episode 或 attempt。"""

    if index.schema_version != "traceforge.failure-analysis-mapping-index.v1":
        raise ValueError("mapping index schema_version 不匹配")
    if not index.m1d_run_id or not index.m1b_run_id:
        raise ValueError("mapping index 缺少上游 run ID")

    episode_ids = tuple(episode.episode_ref for episode in index.episodes)
    attempt_ids = tuple(attempt.attempt_ref for attempt in index.attempts)
    _ensure_unique(episode_ids, "episode_ref")
    _ensure_unique(attempt_ids, "attempt_ref")

    for episode in index.episodes:
        _validate_episode(episode)
    episode_id_set = set(episode_ids)
    for attempt in index.attempts:
        _validate_attempt(attempt, episode_id_set)

    episode_query_turns = {episode.query_turn_id for episode in index.episodes}
    for attempt in index.attempts:
        if attempt.query_turn_id not in episode_query_turns:
            raise ValueError("attempt_ref 的 query_turn_id 不存在于 episode")

    session_refs = {
        session.get("session_ref")
        for session in index.sessions
        if isinstance(session.get("session_ref"), str)
    }
    if len(session_refs) != len(index.sessions):
        raise ValueError("sessions 缺少唯一 session_ref")
    if any(episode.session_ref not in session_refs for episode in index.episodes):
        raise ValueError("episode 引用了不存在 session_ref")


__all__ = ["validate_mapping_index"]
