"""终态业务探针单独留存，不能作为初态充分性或返修凭据。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.pipeline import _environment_feedback
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency


class _ReviewRuntime:
    """提供固定模型输出，仅验证生产代码对实际回执集合的分组和持久化。"""

    backend = "hermes-sandbox"
    model_name = "fixture"

    def __init__(self, *, initial_checks: bool) -> None:
        self.initial_checks = initial_checks
        self.probes: list[dict] = []

    def run(self, *, role, instruction, session, output_root):
        session.read_only_probe_blocked = True
        session.tool_events.append({"name": "read_file", "ok": True})
        if self.initial_checks:
            self.probes.extend(
                {"probe_id": kind, "purpose": kind, "status": "PASS"}
                for kind in ("load", "reset", "dependency")
            )
        self.probes.append({
            "probe_id": "final-business", "purpose": "rollout", "status": "FAIL",
            "python_code": "raise AssertionError('solver defect')",
            "executions": [{"exit_code": 1, "stderr": "solver defect"}],
        })
        session.environment_probes.extend(self.probes)
        return AgentResult(
            role=role.name, backend=self.backend, completed=True,
            payload={
                "label": "SUFFICIENT", "decision": "READY",
                "reason": "原入口足够，答卷实现错误不要求改原初态",
                "missing_context": [], "confidence": 1.0,
                "integrity_classifications": [],
                "rollout_review": {"requirements": [{
                    "trial": "trial-1", "obligation_id": "behavior", "status": "SOLVER_ERROR",
                    "reason": "实际终态实现的受控业务检查失败",
                    "evidence_refs": ["trial-1/files/main.py"],
                }]},
            },
        )


@pytest.mark.parametrize("initial_checks", [True, False])
def test_final_probe_is_preserved_without_certifying_or_repairing_initial_workspace(
    tmp_path: Path, initial_checks: bool,
):
    workspace = tmp_path / "initial"
    workspace.mkdir()
    original = "baseline = True\n"
    (workspace / "main.py").write_text(original, encoding="utf-8")
    task = {
        "task_id": "task", "task_instruction": "修复指定缺陷",
        "core_objective": "修复指定缺陷", "acceptance_obligations": [{"id": "behavior"}],
        "environment_bindings": [],
    }
    evidence = {
        "trials": [{"trial": "trial-1"}],
        "evidence_refs": ["trial-1/files/main.py"],
    }
    runtime = _ReviewRuntime(initial_checks=initial_checks)
    result = run_workspace_sufficiency(
        task=task, workspace_root=workspace, agent=runtime, output_root=tmp_path / "review",
        rollout_evidence=evidence,
    )
    initial = result.get("environment_probes", [])
    assert {probe["purpose"] for probe in initial} == (
        {"load", "reset", "dependency"} if initial_checks else set()
    )
    assert result["execution_preflight"]["probe_count"] == (3 if initial_checks else 0)
    assert result["execution_preflight"]["status"] == ("READY" if initial_checks else "REVIEW")
    if not initial_checks:
        assert "ENVIRONMENT_PROBES_REQUIRED" in result["execution_preflight"]["errors"]
    review = result["rollout_review"]
    assert review["status"] == "COMPLETE"
    assert review["probes"] == [runtime.probes[-1]]
    assert review["probes"][0]["status"] == "FAIL"
    assert review["acceptance"] == "NOT_ASSESSED" and review["sft_eligible"] is False
    assert _environment_feedback(result, {"status": "READY"})["failed_probes"] == []
    assert (workspace / "main.py").read_text() == original
    assert json.loads((tmp_path / "review/sufficiency.json").read_text()) == result
