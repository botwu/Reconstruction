"""独立 Verifier 生成与 RED-check。"""

from .red_check import RedCheckCase, RedCheckReport, evaluate_red_check
from .synthesis import (
    SolutionVariant,
    VerifierCandidate,
    VerifierSynthesisError,
    synthesize_verifier,
)

__all__ = [
    "RedCheckCase",
    "RedCheckReport",
    "SolutionVariant",
    "VerifierCandidate",
    "VerifierSynthesisError",
    "evaluate_red_check",
    "synthesize_verifier",
]
