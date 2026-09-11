"""M2 到 M4 的 TaskEpisode/Attempt 事实映射契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from traceforge.trajectory.contracts import SerializableContract
from traceforge.trajectory.json_codec import stable_id

M4_MAPPING_CONTRACT_VERSION = "failure-analysis-mapping-v1"
EPISODE_REF_SCHEMA = "traceforge.failure-analysis-episode-ref.v1"
ATTEMPT_REF_SCHEMA = "traceforge.failure-analysis-attempt-ref.v1"
MAPPING_INDEX_SCHEMA = "traceforge.failure-analysis-mapping-index.v1"
EPISODE_REF_NAMESPACE = "m4-episode-ref-v1"
ATTEMPT_REF_NAMESPACE = "m4-attempt-ref-v1"

MappingSession: TypeAlias = dict[str, str]


class MappingBasis(StrEnum):
    """建立映射的事实来源。"""

    QUERY_TURN = "QUERY_TURN"
    AGENT_STEP = "AGENT_STEP"
    TURN_FALLBACK = "TURN_FALLBACK"


class SemanticStatus(StrEnum):
    """语义解析状态；结构映射完成不等于任务语义已确认。"""

    STRUCTURAL_ONLY = "STRUCTURAL_ONLY"
    MODEL_PENDING = "MODEL_PENDING"


@dataclass(frozen=True, slots=True)
class EpisodeRefV1(SerializableContract):
    schema_version: str
    episode_ref: str
    session_ref: str
    query_turn_id: str
    capture_occurrence_id: str
    turn_ordinal: int
    user_block_id: str | None
    boundary_ids_spanned: tuple[str, ...]
    mapping_basis: str
    semantic_status: str


@dataclass(frozen=True, slots=True)
class AttemptRefV1(SerializableContract):
    schema_version: str
    attempt_ref: str
    episode_ref: str
    session_ref: str
    query_turn_id: str
    agent_step_id: str | None
    assistant_event_id: str | None
    step_ordinal: int
    turn_status: str
    mapping_basis: str
    semantic_status: str


@dataclass(frozen=True, slots=True)
class MappingIndexV1(SerializableContract):
    schema_version: str
    m4_mapping_contract_version: str
    m1d_run_id: str
    m1d_artifact_manifest_sha256: str
    m1b_run_id: str
    sessions: tuple[MappingSession, ...]
    episodes: tuple[EpisodeRefV1, ...]
    attempts: tuple[AttemptRefV1, ...]


def episode_ref_id(*, m1d_run_id: str, query_turn_id: str) -> str:
    """为一个已存在的 QueryTurn 创建稳定 episode 外键。"""

    return stable_id(
        EPISODE_REF_NAMESPACE,
        {
            "contract_version": M4_MAPPING_CONTRACT_VERSION,
            "m1d_run_id": m1d_run_id,
            "query_turn_id": query_turn_id,
        },
    )


def attempt_ref_id(
    *,
    m1d_run_id: str,
    query_turn_id: str,
    agent_step_id: str | None,
    step_ordinal: int,
) -> str:
    """为一个已存在的 agent step 创建稳定 attempt 外键。"""

    return stable_id(
        ATTEMPT_REF_NAMESPACE,
        {
            "contract_version": M4_MAPPING_CONTRACT_VERSION,
            "m1d_run_id": m1d_run_id,
            "query_turn_id": query_turn_id,
            "agent_step_id": agent_step_id,
            "step_ordinal": step_ordinal,
        },
    )


__all__ = [
    "ATTEMPT_REF_SCHEMA",
    "EPISODE_REF_SCHEMA",
    "M4_MAPPING_CONTRACT_VERSION",
    "MAPPING_INDEX_SCHEMA",
    "AttemptRefV1",
    "EpisodeRefV1",
    "MappingBasis",
    "MappingIndexV1",
    "MappingSession",
    "SemanticStatus",
    "attempt_ref_id",
    "episode_ref_id",
]
