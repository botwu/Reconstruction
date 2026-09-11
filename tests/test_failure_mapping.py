from traceforge.failure_analysis.mapping_contracts import (
    M4_MAPPING_CONTRACT_VERSION,
    MAPPING_INDEX_SCHEMA,
    AttemptRefV1,
    MappingIndexV1,
    attempt_ref_id,
    episode_ref_id,
)
from traceforge.failure_analysis.mapping_validation import validate_mapping_index


def test_refs_are_content_addressed_and_distinct():
    e1 = episode_ref_id(m1d_run_id="r", query_turn_id="q1")
    e2 = episode_ref_id(m1d_run_id="r", query_turn_id="q2")
    assert e1 != e2 and e1 == episode_ref_id(m1d_run_id="r", query_turn_id="q1")
    assert attempt_ref_id(
        m1d_run_id="r", query_turn_id="q1", agent_step_id=None, step_ordinal=0
    ) != attempt_ref_id(m1d_run_id="r", query_turn_id="q1", agent_step_id=None, step_ordinal=1)


def test_mapping_validator_rejects_orphan_attempt():
    idx = MappingIndexV1(
        MAPPING_INDEX_SCHEMA,
        M4_MAPPING_CONTRACT_VERSION,
        "m1d",
        "sha",
        "m1b",
        (),
        (),
        (
            AttemptRefV1(
                "a",
                "x",
                "missing",
                "s",
                "q",
                None,
                None,
                0,
                "INCOMPLETE",
                "TURN_FALLBACK",
                "STRUCTURAL_ONLY",
            ),
        ),
    )
    try:
        validate_mapping_index(idx)
    except ValueError as exc:
        assert "不存在" in str(exc)
    else:
        raise AssertionError("expected orphan rejection")
