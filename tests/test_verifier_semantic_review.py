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
    assert (tmp_path / "verifier/semantic-review/review.json").is_file()


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
    result, candidate = _run(tmp_path, ReviewRuntime(), {"response_contract": {
        "schema_version": "traceforge.response-contract.v1", "checks": [
            {"kind": "acceptance_report", "obligation_id": "factual_correctness"}]}})
    assert candidate is None
    assert result["status"] == "REVIEW"
    assert "VERIFIER_REVIEW_INVALID" in result["errors"]
