"""Verifier 新会话审查必须把具体语义问题送回现有修复轮。"""

from pathlib import Path

import pytest

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.verifier_recovery import run_verifier_recovery
from test_verifier_syntax_regressions import _payload


class ReviewRuntime:
    model_name = "fixture"

    def __init__(self, decision="ACCEPT", covered=True, completed=True):
        self.decision, self.covered, self.completed = decision, covered, completed
        self.calls = []

    def run(self, *, role, instruction, session, output_root):
        self.calls.append((role, session, instruction))
        if role.result_schema == "traceforge.verifier-semantic-review.v1":
            assert role.tools == ("list_dir", "read_file")
            assert session.allow_write is False
            return AgentResult(role=role.name, backend="fixture", completed=self.completed, payload={
                "decision": self.decision,
                "obligation_reviews": [{"obligation_id": "output", "covered": self.covered,
                                        "reason": "逐项核对输出行为"}],
                "issues": [] if self.decision == "ACCEPT" else [{
                    "obligation_id": "output", "problem": "只检查关键词，错误结论也会通过",
                    "counterexample": "格式齐全但虚构 APPPROVED，引用不存在的行",
                    "repair": "读取实际源文件并核对结论与引用；保持合法格式的错误结果必须失败",
                }],
            })
        return AgentResult(role=role.name, backend="fixture", completed=True, payload=_payload("echo first"))


def _run(tmp_path: Path, runtime: ReviewRuntime, task_extra=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.py").write_text("def get_value(): return 1\n")
    return run_verifier_recovery(
        task={"task_instruction": "Review input.py and write review.md", "acceptance_obligations": [
            {"id": "output", "text": "Write a correct review"}], "environment_bindings": [
            {"obligation_id": "output", "verifier_kind": "FILE", "output_paths": ["review.md"]}],
              **(task_extra or {})},
        workspace_root=workspace, agent=runtime, output_root=tmp_path / "verifier",
    )


def test_semantic_review_runs_with_fresh_session_and_binds_candidate(tmp_path):
    runtime = ReviewRuntime()
    result, candidate = _run(tmp_path, runtime)
    assert result["status"] == "READY"
    assert len(runtime.calls) == 2
    assert runtime.calls[0][1] is not runtime.calls[1][1]
    assert result["semantic_review"]["candidate_id"] == candidate.candidate_id
    assert result["semantic_review"]["pytest_evidence"]["runs"] == []
    assert (tmp_path / "verifier/semantic-review/review.json").is_file()
    from traceforge.reconstruction.verifier_recovery import verifier_input_binding
    binding = result["input_binding"]
    assert set(binding) == {"task_sha256", "source_sha256", "workspace_sha256"}
    (tmp_path / "workspace/input.py").write_text("def get_value(): return 2\n")
    assert verifier_input_binding({}, tmp_path / "workspace", None)["workspace_sha256"] != binding["workspace_sha256"]


def test_keyword_only_review_returns_concrete_repair_feedback(tmp_path):
    result, candidate = _run(tmp_path, ReviewRuntime("REVISE", covered=False))
    assert candidate is None
    assert result["status"] == "REVIEW"
    assert "VERIFIER_SEMANTIC_REPAIR_REQUIRED" in result["errors"]
    assert result["feedback"]["previous_candidate"]["test_outputs_py"]
    assert result["feedback"]["semantic_review"]["issues"][0]["counterexample"]


@pytest.mark.parametrize("covered,completed", [(False, True), (True, False)])
def test_accept_label_cannot_hide_uncovered_or_incomplete_review(tmp_path, covered, completed):
    result, candidate = _run(tmp_path, ReviewRuntime(covered=covered, completed=completed))
    assert candidate is None
    assert result["status"] == "REVIEW"


def test_file_review_cannot_certify_unreviewed_response_obligation_mapping(tmp_path):
    result, candidate = _run(tmp_path, ReviewRuntime(), {
        "source_task": {"user_texts": [
            "Finish with:\n" + chr(96) * 3 + "acceptance-report\n"
            + '{"criteriaSatisfied":[{"id":"criterion-1","status":"satisfied","evidence":"proof"}]}'
            + "\n" + chr(96) * 3,
        ]},
        "acceptance_obligations": [
            {"id": "output", "text": "Write a correct review"},
            {"id": "factual_correctness", "text": "All report statements must be true"},
        ],
        "environment_bindings": [
            {"obligation_id": "output", "verifier_kind": "FILE", "output_paths": ["review.md"]},
            {"obligation_id": "factual_correctness", "verifier_kind": "NON_FILE"},
        ],
        "response_contract": {
            "schema_version": "traceforge.response-contract.v1", "checks": [
                {"kind": "acceptance_report", "obligation_id": "factual_correctness"}],
        },
    })
    assert candidate is None
    assert result["status"] == "REVIEW"
    assert "VERIFIER_REVIEW_INVALID" in result["errors"]


@pytest.mark.parametrize("changed_script,service_failed,stale_review,changed_evidence", [
    (False, False, False, False), (True, False, False, False), (False, True, False, False),
    (False, False, True, False), (False, False, False, True),
])
def test_rejected_verifier_must_change_program_before_another_review(
    tmp_path, changed_script, service_failed, stale_review, changed_evidence,
):
    import copy

    runtime = ReviewRuntime("REVISE", covered=False)
    first, _ = _run(tmp_path, runtime)
    feedback = copy.deepcopy(first["feedback"])
    if service_failed:
        feedback["semantic_review"]["status"] = "AGENT_FAILED"
    if stale_review:
        feedback["semantic_review"]["prompt_version"] = "previous-review-version"
    proposed = copy.deepcopy(feedback["previous_candidate"])
    proposed["oracle_solutions"][0]["justification"] = "只更新说明不能修复测试"
    if changed_script:
        proposed["oracle_solutions"][0]["script"] = "echo changed"

    class NextRuntime(ReviewRuntime):
        def run(self, **kwargs):
            if kwargs["role"].result_schema == "traceforge.verifier-semantic-review.v1":
                return super().run(**kwargs)
            self.calls.append((kwargs["role"], kwargs["session"], kwargs["instruction"]))
            if changed_evidence:
                import hashlib
                import shlex
                import subprocess

                from traceforge.reconstruction.container_verification import (
                    _workspace_digest_command,
                )

                digest = subprocess.check_output(
                    shlex.split(_workspace_digest_command(str(kwargs["session"].workspace))),
                    text=True,
                ).strip()
                kwargs["session"].pytest_runs.append({
                    "name": "test_missing", "status": "FAIL", "stdout": "真实初态执行",
                    "stderr": "", "test_sha256": hashlib.sha256(
                        proposed["test_outputs_py"].encode()).hexdigest(),
                    "input_sha256": digest, "input_unchanged": True,
                })
            return AgentResult(
                role=kwargs["role"].name, backend="fixture", completed=True, payload=proposed,
            )

    next_runtime = NextRuntime("REVISE", covered=False)
    result, candidate = run_verifier_recovery(
        task={"task_instruction": "Review input.py and write review.md",
              "acceptance_obligations": [{"id": "output", "text": "Write a correct review"}],
              "environment_bindings": [{
                  "obligation_id": "output", "verifier_kind": "FILE", "output_paths": ["review.md"],
              }]},
        workspace_root=tmp_path / "workspace", agent=next_runtime,
        output_root=tmp_path / "second", feedback=feedback, round_number=2,
    )
    assert candidate is None
    no_progress = not (changed_script or service_failed or stale_review or changed_evidence)
    assert len(next_runtime.calls) == (1 if no_progress else 2)
    assert ("VERIFIER_NO_PROGRESS" in result["errors"]) is no_progress
    assert result["feedback"]["semantic_review"]["issues"][0]["counterexample"]


def test_file_semantic_mechanism_ready_keeps_actual_obligation_unverified(tmp_path):
    class CombinedRuntime(ReviewRuntime):
        def run(self, **kwargs):
            result = super().run(**kwargs)
            if kwargs["role"].result_schema != "traceforge.verifier-semantic-review.v1":
                result.payload["missing_capability_tests"] = []
                result.payload["file_semantic_checks"] = {"output": "报告结论逐项符合实际输入"}
            return result

    result, candidate = _run(tmp_path, CombinedRuntime())
    assert candidate is not None and result["status"] == "READY"
    assert result["unverified_obligations"] == ["output"]
    assert result["pending_file_semantic_obligations"] == ["output"]


def test_semantic_candidate_still_requires_matching_executed_protection():
    import hashlib
    from traceforge.reconstruction.verifier_recovery import _pytest_red_ok
    from traceforge.verifier.synthesis import candidate_from_payload

    payload = _payload("echo first")
    payload["missing_capability_tests"] = []
    payload["file_semantic_checks"] = {"output": "核查真实源码业务迁移"}
    candidate, _ = candidate_from_payload(
        payload, obligation_ids=["output"], model_name="fixture",
        prompt_sha256="request", response_sha256="response",
    )
    digest = hashlib.sha256(candidate.test_outputs_py.encode()).hexdigest()
    runs = [{"name": name, "status": "PASS", "test_sha256": digest}
            for name in candidate.protective_tests]
    assert _pytest_red_ok(runs, candidate)
    assert not _pytest_red_ok([], candidate)
    assert not _pytest_red_ok([{**row, "test_sha256": "previous"} for row in runs], candidate)
    assert not _pytest_red_ok([{**row, "status": "FAIL"} for row in runs], candidate)


class ExecutedRuntime(ReviewRuntime):
    def __init__(self, stale_field=None):
        super().__init__()
        self.stale_field = stale_field
        self.runs = []

    def run(self, **kwargs):
        import hashlib
        import json
        import shlex
        import subprocess

        from traceforge.reconstruction.container_verification import _workspace_digest_command

        role, session = kwargs["role"], kwargs["session"]
        if role.result_schema == "traceforge.verifier-semantic-review.v1":
            supplied = json.loads(kwargs["instruction"].splitlines()[-1])["pytest_evidence"]
            assert supplied["runs"] == self.runs
            assert supplied["test_sha256"] == self.runs[0]["test_sha256"]
            return super().run(**kwargs)
        result = super().run(**kwargs)
        test_sha = hashlib.sha256(result.payload["test_outputs_py"].encode()).hexdigest()
        input_sha = subprocess.check_output(
            shlex.split(_workspace_digest_command(str(session.workspace))), text=True,
        ).strip()
        self.runs = [
            {"name": name, "status": status, "stdout": "真实完整输出\n" * 200,
             "stderr": "", "error_code": None, "test_sha256": test_sha,
             "input_sha256": input_sha, "input_unchanged": True}
            for name, status in [("test_protective", "PASS"), ("test_missing", "FAIL")]
        ]
        if self.stale_field == "workspace":
            (session.workspace / "input.py").write_text("def get_value(): return 2\n")
        elif self.stale_field:
            self.runs[0][self.stale_field] = (
                False if self.stale_field == "input_unchanged" else "old"
            )
        session.pytest_runs.extend(self.runs)
        return result


def test_semantic_review_receives_complete_executed_pytest_results(tmp_path):
    runtime = ExecutedRuntime()
    result, candidate = _run(tmp_path, runtime)
    evidence = result["semantic_review"]["pytest_evidence"]
    assert result["status"] == "READY" and candidate is not None
    assert evidence["candidate_id"] == candidate.candidate_id
    assert evidence["runs"] == runtime.runs
    assert (
        evidence["input_binding"]["workspace_sha256"]
        == result["input_binding"]["workspace_sha256"]
    )
    assert len(evidence["runs"][0]["stdout"]) > 512


@pytest.mark.parametrize("field", ["test_sha256", "input_sha256", "input_unchanged", "workspace"])
def test_semantic_review_rejects_expired_or_changed_pytest_evidence(tmp_path, field):
    runtime = ExecutedRuntime(stale_field=field)
    with pytest.raises(ValueError, match="VERIFIER_PYTEST_EVIDENCE_MISMATCH"):
        _run(tmp_path, runtime)
    assert len(runtime.calls) == 1
