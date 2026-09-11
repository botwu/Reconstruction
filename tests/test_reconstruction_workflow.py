from __future__ import annotations

# ruff: noqa: E501
import json
from pathlib import Path

from traceforge.reconstruction.model_gateway import ModelRequest, ModelResponse
from traceforge.reconstruction.workflow import run_reconstruction_workflow


class WorkflowModel:
    def complete(self, request: ModelRequest) -> ModelResponse:
        schema = request.response_schema
        if schema == "traceforge.task-recovery-candidates.v1":
            payload = {
                "candidates": [{
                    "task_title": "写结果文件",
                    "task_instruction": "在 workspace 中生成 result.txt，内容为 hello",
                    "user_intent": "生成结果文件",
                    "acceptance_obligations": [{"id": "result", "description": "result.txt 内容正确"}],
                    "explicit_constraints": [], "ambiguities": [], "do_not_infer": [],
                    "evidence": [{"evidence_ref_id": "e1", "role": "user", "source_pointer": "q"}],
                    "confidence": 0.95, "decision": "READY",
                }], "open_questions": []
            }
        elif schema == "traceforge.environment-completion-candidates.v1":
            payload = {
                "candidates": [{
                    "files": [], "dependencies": [], "runtime_constraints": [],
                    "uncertainties": [], "decision": "READY",
                }], "open_questions": []
            }
        elif schema == "traceforge.workspace-sufficiency.v1":
            payload = {
                "label": "SUFFICIENT", "reason": "empty workspace is sufficient",
                "missing_context": [], "evidence_ref_ids": ["e1"],
                "confidence": 0.9, "decision": "READY",
            }
        else:
            payload = {
                "status": "READY",
                "test_outputs_py": (
                    "import os\n"
                    "def test_missing(): assert not os.path.exists(os.path.join(os.environ['TRACEFORGE_WORKSPACE'], 'result.txt'))\n"
                    "def test_protective(): assert os.path.isdir(os.environ['TRACEFORGE_WORKSPACE'])\n"
                    "def test_result(): assert open(os.path.join(os.environ['TRACEFORGE_WORKSPACE'], 'result.txt')).read() == 'hello'\n"
                ),
                "oracle_solutions": [
                    {"name": "a", "script": "printf hello > result.txt", "justification": "write"},
                    {"name": "b", "script": "printf hello > ./result.txt", "justification": "write"},
                ],
                "mutation_solutions": [
                    {"name": "bad", "script": "printf bye > result.txt", "justification": "wrong value"}
                ],
                "missing_capability_tests": ["test_missing"],
                "protective_tests": ["test_protective"],
                "obligation_coverage": {"result": ["test_result"]},
                "expected_value_strategy": "constant independently specified by user",
                "open_questions": [],
            }
        return ModelResponse(request.request_id, request.model, "fake", json.dumps(payload), 1, 0.01)


def test_workflow_plan_only_builds_all_stages(tmp_path: Path):
    replay = tmp_path / "replay"
    replay.mkdir()
    harbor = tmp_path / "harbor"
    (harbor / "configs").mkdir(parents=True)
    (harbor / ".venv" / "bin").mkdir(parents=True)
    harbor_bin = harbor / ".venv" / "bin" / "harbor"
    harbor_bin.write_text("#!/bin/sh\nexit 0\n")
    harbor_bin.chmod(0o755)
    config = """jobs_dir: /tmp/jobs
n_concurrent_trials: 1
agents:
  hermes:
    model_name: anthropic/claude-opus-4-8
    expected_commit: abc
environment:
  kwargs:
    sandbox_timeout_sec: 900
"""
    for name in ("hermes-batch.yaml", "oracle.yaml", "nop.yaml"):
        (harbor / "configs" / name).write_text(config)
    out = run_reconstruction_workflow(
        attempt_ref="attempt-1", source_report_id="report-1",
        report={"failure": "incomplete"},
        evidence=[{"evidence_ref_id": "e1", "role": "user"}],
        replay_workspace=replay, replay_files=[], model=WorkflowModel(),
        output_root=tmp_path / "out", harbor_root=harbor,
    )
    assert (out / "workflow_manifest.json").is_file()
    assert (out / "sft_curation.json").is_file()
    execution = json.loads((out / "execution.json").read_text())
    assert execution["status"] == "PLAN_ONLY"
    assert execution["red_check"]["status"] == "PENDING_EXECUTION"
    assert Path(json.loads((out / "workflow_manifest.json").read_text())["stages"]["bundle"]).is_dir()

