"""独立 Verifier 生成与 RED-check。"""

from .red_check import RedCheckCase, RedCheckReport, evaluate_red_check
from .synthesis import (
    SolutionVariant,
    VerifierCandidate,
    VerifierSynthesisError,
    python_script_syntax_error,
    synthesize_verifier,
    validate_solution_scripts,
)

__all__ = [
    "RedCheckCase",
    "RedCheckReport",
    "SolutionVariant",
    "VerifierCandidate",
    "VerifierIterationResult",
    "VerifierSynthesisError",
    "evaluate_red_check",
    "python_script_syntax_error",
    "synthesize_verifier",
    "synthesize_verifier_iterative",
    "validate_solution_scripts",
]

from .iterative import VerifierIterationResult, synthesize_verifier_iterative
