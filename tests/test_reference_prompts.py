import traceforge.failure_analysis.reference_prompts as rp
from traceforge.failure_analysis.reference_prompts import (
    PROMPT_ADAPTER_VERSION,
    prompt_context,
    provenance,
    taxonomy_data,
)


def test_reference_assets_are_loaded_and_provenance_is_stable():
    context = prompt_context()
    provenance_data = provenance()
    assert PROMPT_ADAPTER_VERSION in context
    assert "Step 1 — Locate the first failure" in context
    assert "NA" in context and "PRESENT" in context and "LACKING" in context
    assert len(provenance_data["agentrx_taxonomy_sha256"]) == 64
    assert len(taxonomy_data()) >= 5


def test_reference_context_preserves_unknown_outcome_boundary():
    context = prompt_context()
    assert "must not be converted into success or failure" in context
    assert "zero denominator" in context


def test_invariant_prompts_are_extracted_and_provenance_bound():
    static = rp.static_invariant_guidance()
    dynamic = rp.dynamic_invariant_guidance()
    provenance = rp.provenance()
    assert "STATIC invariant" in static or "static assertions" in static
    assert "DYNAMIC" in dynamic or "dynamic invariants" in dynamic
    assert len(static) > 20_000
    assert len(dynamic) > 20_000
    assert provenance["agentrx_invariants_commit"] == provenance["agentrx_commit"]
    assert len(provenance["agentrx_static_source_sha256"]) == 64
    assert len(provenance["agentrx_dynamic_source_sha256"]) == 64
    assert "AGENTRX_DYNAMIC_INVARIANT_PROMPT:" in rp.prompt_context()
