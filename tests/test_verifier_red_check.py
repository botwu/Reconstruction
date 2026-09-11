from traceforge.verifier.red_check import RedCheckCase, evaluate_red_check


def test_red_check_requires_oracle_nop_and_mutation():
    cases = (
        RedCheckCase("oracle", "oracle_pass", "PASS", 1.0, "PASS", 1.0),
        RedCheckCase("nop", "nop_fail", "FAIL", 0.0, "FAIL", 0.0),
        RedCheckCase("mutation", "mutation_fail", "FAIL", 0.0, "FAIL", 0.0),
    )
    report = evaluate_red_check(cases)
    assert report.passed is True


def test_red_check_rejects_wrong_verdict():
    report = evaluate_red_check((RedCheckCase("nop", "nop_fail", "PASS", 1.0, "FAIL", 0.0),))
    assert report.passed is False
    assert "missing:oracle_pass" in report.failed_case_ids
