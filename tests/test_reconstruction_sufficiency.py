import pytest

from traceforge.reconstruction.sufficiency import SufficiencyInputError, validate_payload


def test_ready_requires_complete_surfaces():
    result = validate_payload(
        "candidate-1",
        {
            "task_sufficient": True,
            "environment_sufficient": True,
            "decision": "READY",
            "blocking_items": [],
            "reason_codes": ["OBSERVED_ENTRYPOINT"],
            "confidence": 0.9,
        },
    )
    assert result.to_dict()["schema_version"] == "traceforge.workspace-sufficiency.v1"


def test_blocked_requires_explicit_reason():
    with pytest.raises(SufficiencyInputError):
        validate_payload(
            "candidate-1",
            {
                "task_sufficient": False,
                "environment_sufficient": False,
                "decision": "BLOCKED",
                "blocking_items": [],
                "reason_codes": [],
                "confidence": 0.1,
            },
        )
