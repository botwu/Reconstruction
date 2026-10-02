"""组合 RED 使用真实文件快照与原始 pytest 结果；模型和执行均为明确的离线替身。"""

import json
import shutil
from types import SimpleNamespace

import pytest

from traceforge.harbor_ags.results import build_file_artifact_snapshot
from traceforge.harbor_ags.response_acceptance import apply_file_semantic_receipts
from traceforge.reconstruction.agents.runtime import AgentResult, HermesNativeRuntime
from traceforge.reconstruction.researcher import ReconstructionRuntime
from traceforge.reconstruction.agents.session import execute_tool
from traceforge.reconstruction.artifact_review import review_file_artifact
from traceforge.reconstruction.verification import (
    HarborCalibrationExecutor, VerificationConfig, initial_red_check, _record_unverified_obligations,
)
from traceforge.verifier.bundle import compile_bundle
from traceforge.verifier.red_check import evaluate_red_check
from traceforge.verifier.synthesis import VerifierCandidate, SolutionVariant

TASK = {
    "task_id": "extract", "task_instruction": "将计算提取为新模块，由主程序调用，行为保持一致。",
    "acceptance_obligations": [{"id": "extract", "text": "提取实际计算，由主程序消费结果。"}],
    "environment_bindings": [{"obligation_id": "extract", "verifier_kind": "FILE",
                              "required_paths": ["main.py"], "initial_required_paths": ["main.py"],
                              "output_paths": [], "observable": "主程序调用新模块中的实际计算。"}],
}
CHECKS = {"extract": "新模块实际实现计算，主程序调用并消费其结果，不只是包装旧业务回调。"}


def candidate():
    return VerifierCandidate(
        schema_version="traceforge.verifier-candidate.v1", candidate_id="fixture",
        status="UNVALIDATED", test_outputs_py="def test_preserved(): assert 3 * 2 == 6\n",
        oracle_solutions=(SolutionVariant("a", "print(1)", "离线替身"),
                          SolutionVariant("b", "print(2)", "离线替身")),
        mutation_solutions=(SolutionVariant("m", "print(3)", "离线替身"),),
        missing_capability_tests=(), protective_tests=("test_preserved",),
        obligation_coverage={"extract": ()}, expected_value_strategy="独立计算",
        open_questions=(), model="fixture", prompt_version="fixture",
        prompt_sha256="fixture", response_sha256="fixture", file_semantic_checks=CHECKS,
    )


def trial_fixture(tmp_path, final_files=None):
    seed = tmp_path / "seed"
    seed.mkdir(parents=True)
    (seed / "main.py").write_text("def calculate(value): return value * 2\ndef main(): return calculate(3)\n")
    bundle = compile_bundle(task=TASK, workspace_root=seed, verifier=candidate(), output_root=tmp_path / "bundles")
    task = bundle / "task"
    trial = tmp_path / "trial"
    (trial / "verifier").mkdir(parents=True)
    (trial / "config.json").write_text(json.dumps({"task": {"path": str(task)}}))
    (trial / "result.json").write_text(json.dumps({"exception_info": None, "finished_at": "fixture"}))
    verdict = {"exit_code": 0, "tests": [{"name": "test_preserved", "status": "PASS"}]}
    (trial / "verifier/verdict.json").write_text(json.dumps(verdict))
    final = trial / "artifacts/logs/artifacts/traceforge/workspace"
    shutil.copytree(task / "workspace", final)
    for name, content in (final_files or {}).items():
        (final / name).write_text(content)
    row = {"status": "PASS", "reward": 1.0, "content_valid": True,
           "result_path": str(trial / "result.json"), "verdict_path": str(trial / "verifier/verdict.json")}
    run = {"execution": {"status": "COMPLETED"}, "file_semantic_checks": CHECKS,
           "results": {"quality_gate": {"ok": True}, "trials": [row]}}
    return trial, run, verdict


class Reviewer:
    model_name = "fixture"

    def __init__(self, decision="ACCEPT", completed=True, expected_log=None):
        self.decision, self.completed = decision, completed
        self.expected_log = expected_log
        self.sessions = []

    def run(self, *, role, instruction, session, output_root):
        assert not role.allow_write
        assert set(role.tools) <= {"list_dir", "read_file", "read_session_message", "read_session_context"}
        assert session.conversation is None
        text = execute_tool("read_file", {"path": "final/main.py"}, session)
        assert "def main" in text
        assert "test_preserved" in execute_tool(
            "read_file", {"path": "execution/verifier/test_outputs.py"}, session)
        if self.expected_log:
            assert self.expected_log in execute_tool(
                "read_file", {"path": "execution/agent/oracle.txt"}, session)
        self.sessions.append(session)
        return AgentResult(
            role=role.name, backend="fixture", completed=self.completed, payload={
                "decision": self.decision, "obligations": [{
                    "obligation_id": "extract", "covered": self.decision == "ACCEPT",
                    "reason": "离线模型替身的既定判断；用于核验编排与证据绑定。",
                    "evidence": [{"path": "final/main.py", "quote": "def main()"}],
                }],
            },
        )


def executor(tmp_path, reviewer):
    return HarborCalibrationExecutor(
        task=TASK, workspace=tmp_path / "seed", root=tmp_path / "runs",
        config=VerificationConfig(harbor_root=tmp_path, model_name="fixture",
                                  rollout_model="anthropic/fixture"),
        agent=reviewer,
    )


def test_combined_red_distinguishes_refactoring_without_changing_behavior_reward(tmp_path):
    cases = []
    nop_review, nop_verdict = None, None
    for label, kind, decision, files in [
        ("nop", "nop_fail", "REVISE", {}),
        ("oracle-a", "oracle_pass", "ACCEPT", {
            "main.py": "from calculation import twice\ndef main(): return twice(3)\n",
            "calculation.py": "def twice(value): return value * 2\n"}),
        ("oracle-b", "oracle_pass", "ACCEPT", {
            "main.py": "from arithmetic import Worker\ndef main(): return Worker().compute(3)\n",
            "arithmetic.py": "class Worker:\n    def compute(self, value): return value * 2\n"}),
        ("mutation", "mutation_fail", "REVISE", {
            "wrapper.py": "def run(callback, value): return callback(value)\n"}),
    ]:
        trial, run, verdict = trial_fixture(tmp_path / label, files)
        worker = executor(tmp_path / label, Reviewer(decision))
        worker._review_artifacts(run)
        expected = "PASS" if decision == "ACCEPT" else "FAIL"
        cases.append(worker._case(label, kind, run, expected))
        assert run["results"]["trials"][0]["status"] == "PASS"
        assert run["results"]["trials"][0]["reward"] == 1.0
        assert run["combined_verdicts"][0]["status"] == expected
        assert (trial / "verifier/file-semantic-review.json").is_file()
        if label == "nop":
            nop_review, nop_verdict = run["file_semantic_reviews"], verdict
    assert evaluate_red_check(tuple(cases)).passed
    assert initial_red_check(candidate(), [nop_verdict], nop_review)["passed"]


@pytest.mark.parametrize("defect", ["missing_snapshot", "incomplete_model", "crashing_mutation"])
def test_incomplete_or_crashing_execution_cannot_become_semantic_red_success(tmp_path, defect):
    trial, run, _ = trial_fixture(tmp_path)
    reviewer = Reviewer("REVISE", completed=defect != "incomplete_model")
    if defect == "missing_snapshot":
        shutil.rmtree(trial / "artifacts")
    if defect == "crashing_mutation":
        (trial / "agent").mkdir()
        (trial / "agent/exit-code.txt").write_text("1")
    worker = executor(tmp_path, reviewer)
    worker._review_artifacts(run)
    case = worker._case("mutation", "mutation_fail", run, "FAIL")
    assert case.status != "FAIL"
    assert not evaluate_red_check((case,)).passed
    assert run["results"]["trials"][0]["reward"] == 1.0


def test_final_semantic_receipt_clears_only_exact_file_obligation(tmp_path):
    trial, run, _ = trial_fixture(tmp_path)
    worker = executor(tmp_path, Reviewer())
    worker._review_artifacts(run)
    result = {"status": "READY", "errors": [], "unverified_obligations": ["extract", "analysis"],
              "pending_file_semantic_obligations": ["extract"]}
    apply_file_semantic_receipts(result, run, {**TASK, "file_semantic_checks": CHECKS}, 1)
    assert result["unverified_obligations"] == ["analysis"]
    (trial / "artifacts/logs/artifacts/traceforge/workspace/main.py").write_text("changed")
    apply_file_semantic_receipts(result, run, {**TASK, "file_semantic_checks": CHECKS}, 1)
    assert result["status"] == "REVIEW" and "extract" in result["unverified_obligations"]


def test_semantic_pending_requires_a_reviewed_mechanism_and_stays_unverified():
    audit = {
        "status": "READY", "verifier": {"file_semantic_checks": CHECKS},
        "unverified_obligations": ["extract"], "pending_file_semantic_obligations": ["extract"],
    }
    result = {}
    assert _record_unverified_obligations(result, task=TASK, audit=audit)
    assert result["unverified_obligations"] == ["extract"]
    audit["semantic_review"] = {"status": "ACCEPT", "errors": []}
    result = {}
    assert not _record_unverified_obligations(result, task=TASK, audit=audit)
    assert result["unverified_obligations"] == result["pending_file_semantic_obligations"] == ["extract"]


def test_binary_difference_is_a_hash_change_not_replacement_text(tmp_path):
    trial, _, _ = trial_fixture(tmp_path)
    final = trial / "artifacts/logs/artifacts/traceforge/workspace"
    (final / "binary.dat").write_bytes(b"\xff\x00\xfe")
    snapshot = build_file_artifact_snapshot(trial)
    output = tmp_path / "review"
    receipt = review_file_artifact(snapshot=snapshot, task=TASK, checks=CHECKS, agent=Reviewer(),
                                   output_root=output, execution_evidence={})
    assert receipt["status"] == "ACCEPT"
    assert "binary.dat" not in (output / "workspace/changes.diff").read_text()
    changes = json.loads((output / "workspace/changes.json").read_text())
    assert next(row for row in changes if row["path"] == "binary.dat")["binary"] is True
    assert (output / "workspace/final/binary.dat").read_bytes() == b"\xff\x00\xfe"


def test_legacy_executor_cannot_certify_semantics_with_only_pytest_pass(monkeypatch):
    from traceforge.verifier import iterative

    monkeypatch.setattr(iterative, "synthesize_verifier", lambda **kwargs: (candidate(), {}))
    result = iterative.synthesize_verifier_iterative(
        task=TASK, workspace_files={}, model=None, executor=SimpleNamespace(run=lambda _: {"status": "PASS"}),
        max_rounds=1,
    )
    assert result.status == "REVIEW"
    assert result.attempts[0]["status"] == "INFRA_ERROR"
    assert result.attempts[0]["feedback"] == "FILE_SEMANTIC_CALIBRATION_MISSING"


def test_reviewer_can_read_actual_oracle_log_and_bound_pytest(tmp_path):
    trial, run, _ = trial_fixture(tmp_path)
    (trial / "agent").mkdir()
    (trial / "agent/oracle.txt").write_text("真实捕获的执行正文")
    (trial / "agent/exit-code.txt").write_text("0")
    worker = executor(tmp_path, Reviewer(expected_log="真实捕获的执行正文"))
    worker._review_artifacts(run)
    assert run["file_semantic_reviews"][0]["status"] == "ACCEPT"
    assert run["combined_verdicts"][0]["status"] == "PASS"


def test_semantic_candidate_without_reviewer_fails_before_cloud_execution(tmp_path, monkeypatch):
    worker = executor(tmp_path, None)
    monkeypatch.setattr(worker, "_run_bundle", lambda *args, **kwargs: pytest.fail("不得启动云执行"))
    result = worker.run(candidate())
    assert result["status"] == "INFRA_ERROR"
    assert "FILE_SEMANTIC_REVIEW_AGENT_MISSING" in result["feedback"]


@pytest.mark.parametrize("outside_read", [False, True])
def test_readonly_artifact_review_uses_bound_snapshot_through_real_runtime_wrappers(tmp_path, outside_read):
    trial, run, _ = trial_fixture(tmp_path)
    (trial / "agent").mkdir()
    (trial / "agent/oracle.txt").write_text("真实执行日志替身")
    calls = []

    class NativeAgent:
        def __init__(self, **kwargs):
            assert kwargs["enabled_toolsets"] == []

        def run_conversation(self, instruction, system_message=None, task_id=None, **kwargs):
            assert "conversation_history" not in kwargs
            calls.append(task_id)
            assert task_id == "file_artifact_review"
            assert {item["function"]["name"] for item in self.tools} == {"list_dir", "read_file"}
            assert "initial" in self._invoke_tool("list_dir", {}, task_id)
            for path, expected in {
                "initial/main.py": "def calculate",
                "final/main.py": "def main",
                "execution/verifier/test_outputs.py": "test_preserved",
                "execution/agent/oracle.txt": "真实执行日志替身",
                "execution.json": "PASS",
            }.items():
                assert expected in self._invoke_tool("read_file", {"path": path}, task_id)
            if outside_read:
                assert self._invoke_tool("read_file", {"path": "../outside.txt"}, task_id).startswith("error:")
            return {"completed": True, "messages": [], "final_response": json.dumps({
                "decision": "ACCEPT", "obligations": [{
                    "obligation_id": "extract", "covered": True,
                    "reason": "确定性替身仅验证真实代理接线及读权限。",
                    "evidence": [{"path": "final/main.py", "quote": "def main()"}],
                }],
            })}

    def sandbox_forbidden():
        pytest.fail("只读受控快照审查不应创建 AGS")

    native = HermesNativeRuntime(factory=NativeAgent, base_url="https://example.test",
                               api_key="fixture", model_name="fixture", provider="custom")
    author = ReconstructionRuntime(native, source={}, task=TASK, runtime_factory=sandbox_forbidden)
    author.conversation.messages = [{"role": "user", "content": "作者历史不能进入独立审查"}]
    (tmp_path / "outside.txt").write_text("不可读取的宿主材料")
    worker = executor(tmp_path, author)
    assert worker.review_agent is author.agent
    worker._review_artifacts(run)
    receipt = json.loads((trial / "verifier/file-semantic-review.json").read_text())
    assert calls == ["file_artifact_review"]
    assert receipt["status"] == "ACCEPT"
    assert (tmp_path / "outside.txt").read_text() == "不可读取的宿主材料"
    assert author.conversation.messages == [{"role": "user", "content": "作者历史不能进入独立审查"}]


def test_completed_trial_is_reviewed_when_another_trial_fails_capture(tmp_path):
    valid_dir, valid, _ = trial_fixture(tmp_path / "valid")
    _, invalid, _ = trial_fixture(tmp_path / "invalid")
    invalid_row = invalid["results"]["trials"][0]
    invalid_row.update(status="INFRA_ERROR", reward=None, content_valid=False,
                       content_errors=["FINAL_WORKSPACE_MISSING"])
    valid["results"]["trials"].append(invalid_row)
    valid["results"]["quality_gate"] = {"ok": False, "reasons": ["TRIAL_INCOMPLETE_OR_INFRA_ERROR"]}
    reviewer = Reviewer()
    worker = executor(tmp_path / "valid", reviewer)
    worker._review_artifacts(valid)
    assert len(reviewer.sessions) == 1
    assert [row["status"] for row in valid["file_semantic_reviews"]] == ["ACCEPT", "REVIEW"]
    assert [row["status"] for row in valid["combined_verdicts"]] == ["PASS", "REVIEW"]
    assert "FINAL_WORKSPACE_MISSING" in valid["file_semantic_reviews"][1]["errors"][0]
    assert valid["results"]["quality_gate"]["ok"] is False
    assert invalid_row["reward"] is None
    assert (valid_dir / "verifier/file-semantic-review.json").is_file()
