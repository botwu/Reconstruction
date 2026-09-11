"""读取 M1D QueryTurn 图并建立 M4 可消费的事实外键。"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from traceforge.query_turns.validation import validate_query_turn_run
from traceforge.trajectory.json_codec import (
    StrictJsonError,
    sha256_bytes,
    strict_json_loads,
)

from .mapping_contracts import (
    ATTEMPT_REF_SCHEMA,
    EPISODE_REF_SCHEMA,
    MAPPING_INDEX_SCHEMA,
    M4_MAPPING_CONTRACT_VERSION,
    AttemptRefV1,
    EpisodeRefV1,
    MappingBasis,
    MappingIndexV1,
    MappingSession,
    SemanticStatus,
    attempt_ref_id,
    episode_ref_id,
)


class MappingInputError(RuntimeError):
    """M1D 缺失、未通过绑定校验或结构字段非法。"""


def _read_jsonl(root: Path, relative_path: str) -> list[dict[str, Any]]:
    path = root / relative_path
    if not path.is_file():
        raise MappingInputError(f"输入 M1D 缺少必需表：{relative_path}")

    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise MappingInputError(f"无法读取 M1D 表：{relative_path}") from exc

    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        try:
            value = strict_json_loads(raw_line)
        except StrictJsonError as exc:
            raise MappingInputError(
                f"M1D 表 JSON 非法：{relative_path}:{line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise MappingInputError(
                f"M1D 表记录不是对象：{relative_path}:{line_number}"
            )
        rows.append(value)
    return rows


def _required_string(row: dict[str, Any], key: str, relative_path: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise MappingInputError(f"字段类型或内容非法：{relative_path}/{key}")
    return value


def _optional_string(row: dict[str, Any], key: str, relative_path: str) -> str | None:
    value = row.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise MappingInputError(f"字段类型或内容非法：{relative_path}/{key}")
    return value


def _non_negative_integer(row: dict[str, Any], key: str, relative_path: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MappingInputError(f"字段类型或内容非法：{relative_path}/{key}")
    return value


def _string_tuple(row: dict[str, Any], key: str, relative_path: str) -> tuple[str, ...]:
    value = row.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MappingInputError(f"字段类型非法：{relative_path}/{key}")
    return tuple(value)


def _validate_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise MappingInputError(f"M1D {label} 存在重复 ID")


def load_mapping_index(
    *,
    m1d_run_dir: str | Path,
    m1b_run_dir: str | Path,
) -> MappingIndexV1:
    """读取并验证 M1D，然后生成只包含真实上游引用的映射索引。"""

    root = Path(m1d_run_dir)
    if not root.is_dir():
        raise MappingInputError("输入 M1D run 不是目录")

    validation = validate_query_turn_run(root, Path(m1b_run_dir))
    if not validation.ok:
        codes = ",".join(sorted({issue.code for issue in validation.issues}))
        raise MappingInputError(f"输入 M1D run 未通过绑定校验：{codes}")

    manifest_path = root / "artifact_manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = strict_json_loads(manifest_bytes)
    except (OSError, StrictJsonError) as exc:
        raise MappingInputError("无法读取 M1D artifact_manifest") from exc
    if not isinstance(manifest, dict):
        raise MappingInputError("M1D artifact_manifest 不是对象")

    m1d_run_id = _required_string(manifest, "query_turn_run_id", "artifact_manifest.json")
    if root.name != m1d_run_id:
        raise MappingInputError("M1D 目录名与 query_turn_run_id 不一致")
    m1b_run_id = _required_string(manifest, "m1b_run_id", "artifact_manifest.json")

    turns = _read_jsonl(root, "private/query_turns.jsonl")
    steps = _read_jsonl(root, "private/agent_steps.jsonl")
    step_rows: dict[str, dict[str, Any]] = {}
    for row in steps:
        step_id = _required_string(row, "agent_step_id", "agent_steps")
        if step_id in step_rows:
            raise MappingInputError(f"agent_steps 存在重复 agent_step_id：{step_id}")
        step_rows[step_id] = row

    def turn_sort_key(row: dict[str, Any]) -> tuple[str, int]:
        capture_id = _required_string(row, "capture_occurrence_id", "query_turns")
        return capture_id, _non_negative_integer(row, "turn_ordinal", "query_turns")

    turns = sorted(turns, key=turn_sort_key)
    query_turn_ids = [
        _required_string(row, "query_turn_id", "query_turns") for row in turns
    ]
    _validate_unique(query_turn_ids, "query_turn_id")

    episodes: list[EpisodeRefV1] = []
    attempts: list[AttemptRefV1] = []
    sessions_by_ref: dict[str, MappingSession] = {}

    for turn in turns:
        query_turn_id = _required_string(turn, "query_turn_id", "query_turns")
        capture_id = _required_string(turn, "capture_occurrence_id", "query_turns")
        turn_ordinal = _non_negative_integer(turn, "turn_ordinal", "query_turns")
        user_block_id = _optional_string(turn, "user_block_id", "query_turns")
        boundaries = _string_tuple(turn, "boundary_ids_spanned", "query_turns")
        session_ref = f"session:{capture_id}"
        episode_id = episode_ref_id(
            m1d_run_id=m1d_run_id,
            query_turn_id=query_turn_id,
        )

        episode = EpisodeRefV1(
            schema_version=EPISODE_REF_SCHEMA,
            episode_ref=episode_id,
            session_ref=session_ref,
            query_turn_id=query_turn_id,
            capture_occurrence_id=capture_id,
            turn_ordinal=turn_ordinal,
            user_block_id=user_block_id,
            boundary_ids_spanned=boundaries,
            mapping_basis=MappingBasis.QUERY_TURN.value,
            semantic_status=SemanticStatus.STRUCTURAL_ONLY.value,
        )
        episodes.append(episode)

        raw_step_ids = turn.get("agent_step_ids")
        if not isinstance(raw_step_ids, list) or any(
            not isinstance(step_id, str) for step_id in raw_step_ids
        ):
            raise MappingInputError("query_turns/agent_step_ids 类型非法")

        turn_steps = []
        for step_id in raw_step_ids:
            if step_id not in step_rows:
                raise MappingInputError(
                    f"query_turns 引用了不存在的 agent_step_id：{step_id}"
                )
            turn_steps.append(step_rows[step_id])

        if not turn_steps:
            # 没有 agent step 时只保留 QueryTurn 级事实，生成显式 fallback
            # attempt，后续语义层必须将其标为未观测，而不能当作真实执行步骤。
            turn_steps = [{"agent_step_id": None, "assistant_event_id": None}]

        for step_ordinal, step in enumerate(turn_steps):
            agent_step_id = _optional_string(step, "agent_step_id", "agent_steps")
            assistant_event_id = _optional_string(
                step, "assistant_event_id", "agent_steps"
            )
            attempts.append(
                AttemptRefV1(
                    schema_version=ATTEMPT_REF_SCHEMA,
                    attempt_ref=attempt_ref_id(
                        m1d_run_id=m1d_run_id,
                        query_turn_id=query_turn_id,
                        agent_step_id=agent_step_id,
                        step_ordinal=step_ordinal,
                    ),
                    episode_ref=episode_id,
                    session_ref=session_ref,
                    query_turn_id=query_turn_id,
                    agent_step_id=agent_step_id,
                    assistant_event_id=assistant_event_id,
                    step_ordinal=step_ordinal,
                    turn_status=_required_string(turn, "turn_status", "query_turns"),
                    mapping_basis=(
                        MappingBasis.AGENT_STEP.value
                        if agent_step_id is not None
                        else MappingBasis.TURN_FALLBACK.value
                    ),
                    semantic_status=SemanticStatus.STRUCTURAL_ONLY.value,
                )
            )

        sessions_by_ref[session_ref] = {
            "session_ref": session_ref,
            "capture_occurrence_id": capture_id,
            "semantic_status": SemanticStatus.STRUCTURAL_ONLY.value,
        }

    return MappingIndexV1(
        schema_version=MAPPING_INDEX_SCHEMA,
        m4_mapping_contract_version=M4_MAPPING_CONTRACT_VERSION,
        m1d_run_id=m1d_run_id,
        m1d_artifact_manifest_sha256=sha256_bytes(manifest_bytes),
        m1b_run_id=m1b_run_id,
        sessions=tuple(
            sessions_by_ref[session_ref]
            for session_ref in sorted(sessions_by_ref)
        ),
        episodes=tuple(episodes),
        attempts=tuple(attempts),
    )


__all__ = ["MappingInputError", "load_mapping_index"]
