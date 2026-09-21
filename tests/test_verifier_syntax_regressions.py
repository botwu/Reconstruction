from __future__ import annotations

import pytest

from traceforge.verifier.synthesis import (
    VerifierSynthesisError,
    candidate_from_payload,
    python_script_syntax_error,
)


def _payload(script: str) -> dict[str, object]:
    return {
        "status": "READY",
        "test_outputs_py": (
            "def test_missing():\n    assert False\n"
            "def test_protective():\n    assert True\n"
            "def test_output():\n    assert True\n"
        ),
        "oracle_solutions": [
            {"name": "bad", "script": script, "justification": "fixture"},
            {"name": "second", "script": "echo second", "justification": "fixture"},
        ],
        "mutation_solutions": [
            {"name": "mutation", "script": "echo mutation", "justification": "fixture"}
        ],
        "missing_capability_tests": ["test_missing"],
        "protective_tests": ["test_protective"],
        "obligation_coverage": {"output": ["test_output"]},
        "expected_value_strategy": "independent",
        "open_questions": [],
    }


def test_invalid_python_shebang_is_rejected_before_harbor() -> None:
    script = (
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "Path('out.txt').write_text('first\n"
        "second')\n"
    )
    assert python_script_syntax_error(script) == "line=3;offset=28"
    with pytest.raises(VerifierSynthesisError, match="ORACLE_SCRIPT_SYNTAX_ERROR:bad"):
        candidate_from_payload(
            _payload(script),
            obligation_ids=["output"],
            model_name="fixture",
            prompt_sha256="prompt",
            response_sha256="response",
        )


def test_shell_heredoc_is_not_misclassified_as_python() -> None:
    script = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "Path('out.txt').write_text('ok')\n"
        "PY"
    )
    assert python_script_syntax_error(script) is None
