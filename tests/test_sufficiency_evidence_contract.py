"""从真实 Sufficiency 产物到环境合同验证任务证据和沙盒坐标传递。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.task_fit import build_environment_contract
from traceforge.reconstruction.workspace_integrity import classify_integrity_issues
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency


class _EvidenceRuntime:
    """固定分类响应，实际执行诊断、分类、持久化及下游合同代码。"""

    backend = "hermes-sandbox"
    model_name = "fixture"

    def __init__(self, kind: str, evidence: dict[str, Any]) -> None:
        self.kind = kind
        self.evidence = evidence
        self.instruction = ""
        self.workspace: Path | None = None

    def run(self, *, role: Any, instruction: str, session: Any, output_root: Path) -> AgentResult:
        self.instruction = instruction
        self.workspace = session.workspace
        report = json.loads(instruction.split("STATIC_INTEGRITY_REPORT:\n", 1)[1])
        assert len(report["issues"]) == 1
        issue = report["issues"][0]
        assert issue["code"] == "REFERENCED_ASSET_MISSING"
        session.sandbox_started = True
        session.sandbox_stopped = True
        session.read_only_probe_blocked = True
        session.tool_events.append({"name": "read_file", "ok": True})
        reason = (
            "任务要求移除 scene.xml 的悬空 include；缺失资源就是待修复基线。"
            if self.kind == "BASELINE_TASK_DEFECT"
            else "任务只分析 README.md 的标题，无需读取或加载 scene.xml。"
        )
        payload = {
            "label": "SUFFICIENT", "decision": "READY", "reason": reason,
            "missing_context": [], "confidence": 0.9,
            "integrity_classifications": [{
                "issue_id": issue["id"], "path": issue["path"],
                "classification": self.kind, "reason": reason, **self.evidence,
            }],
            # 模型声明的已知引用和任务不能扩展调用方提供的证据集合。
            "task_evidence_ref_ids": ["user:forged"],
            "task": {"acceptance_obligations": [{"evidence_ref_ids": ["user:forged"]}]},
        }
        return AgentResult(
            role=role.name, backend=self.backend, payload=payload,
            completed=True, final_text=json.dumps(payload, ensure_ascii=False),
        )


def _run_contract(
    tmp_path: Path, kind: str, evidence: dict[str, Any], *, task_has_refs: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], _EvidenceRuntime]:
    workspace = tmp_path / "host-only-candidate"
    workspace.mkdir()
    (workspace / "scene.xml").write_text(
        '<mujoco><include file="missing.xml"/></mujoco>', encoding="utf-8",
    )
    (workspace / "README.md").write_text("# 任务项目\n", encoding="utf-8")
    baseline = kind == "BASELINE_TASK_DEFECT"
    objective = "移除 scene.xml 中悬空的 include" if baseline else "分析 README.md 的标题"
    task = {
        "task_instruction": objective, "core_objective": objective,
        "acceptance_obligations": [{
            "id": "scope", "evidence_ref_ids": ["user:scope"] if task_has_refs else [],
        }],
        "environment_bindings": [{
            "obligation_id": "scope", "verifier_kind": "FILE",
            "required_paths": ["scene.xml" if baseline else "README.md"],
            "observable": objective,
        }],
    }
    original_task = copy.deepcopy(task)
    runtime = _EvidenceRuntime(kind, evidence)
    result = run_workspace_sufficiency(
        task=task, workspace_root=workspace, agent=runtime, output_root=tmp_path / "judge",
    )
    persisted = json.loads((tmp_path / "judge/sufficiency.json").read_text(encoding="utf-8"))
    assert persisted == result
    assert task == original_task
    assert "task" not in persisted
    contract = build_environment_contract(workspace_root=workspace, sufficiency=persisted)
    return persisted, contract, runtime


@pytest.mark.parametrize("kind", ["BASELINE_TASK_DEFECT", "IRRELEVANT"])
def test_task_evidence_reaches_persisted_environment_contract(tmp_path: Path, kind: str) -> None:
    refs = ["user:scope"]
    result, contract, _ = _run_contract(
        tmp_path, kind, {"classification_evidence_ref_ids": refs},
    )
    assert contract["context_status"] == "READY"
    assert result["prompt_version"] == "workspace-sufficiency-agent-v10-evidence-contract"
    assert result["task_evidence_ref_ids"] == refs
    assert result["integrity_report"]["issues"][0]["classification_evidence_ref_ids"] == refs
    assert result["execution_preflight"]["status"] == "REVIEW"


@pytest.mark.parametrize("kind", ["BASELINE_TASK_DEFECT", "IRRELEVANT"])
@pytest.mark.parametrize("evidence", [
    {},
    {"classification_evidence_ref_ids": []},
    {"classification_evidence_ref_ids": None},
    {"classification_evidence_ref_ids": "user:scope"},
    {"classification_evidence_ref_ids": ["user:forged"]},
    {"classification_evidence_ref_ids": ["user:scope", "user:forged"]},
    {"classification_evidence_ref_ids": ["user:scope", {"ref": "user:scope"}]},
])
def test_ungrounded_exception_stays_review(
    tmp_path: Path, kind: str, evidence: dict[str, Any],
) -> None:
    result, contract, _ = _run_contract(tmp_path, kind, evidence)
    assert contract["context_status"] == "REVIEW"
    assert "RECONSTRUCTABILITY_EXCEPTION_UNGROUNDED:integrity-001" in contract["errors"]
    issue = result["integrity_report"]["issues"][0]
    if "classification_evidence_ref_ids" in evidence:
        assert (
            issue["classification_evidence_ref_ids"]
            == evidence["classification_evidence_ref_ids"]
        )
    else:
        assert "classification_evidence_ref_ids" not in issue
    assert result["task_evidence_ref_ids"] == ["user:scope"]


def test_missing_input_task_refs_cannot_be_supplied_by_model(tmp_path: Path) -> None:
    result, contract, _ = _run_contract(
        tmp_path, "IRRELEVANT", {"classification_evidence_ref_ids": ["user:forged"]},
        task_has_refs=False,
    )
    assert contract["context_status"] == "REVIEW"
    assert result["task_evidence_ref_ids"] == []


def test_prompt_uses_workspace_relative_root_and_allowed_task_refs(tmp_path: Path) -> None:
    _, _, runtime = _run_contract(tmp_path, "IRRELEVANT", {})
    assert "WORKSPACE_ROOT: .\n" in runtime.instruction
    assert str(runtime.workspace) not in runtime.instruction
    assert runtime.workspace == (tmp_path / "host-only-candidate").resolve()
    assert "TRACEFORGE_WORKSPACE" in runtime.instruction
    assert '"classification_evidence_ref_ids"' in runtime.instruction
    assert 'TASK_EVIDENCE_REF_IDS:\n["user:scope"]' in runtime.instruction
    assert "user:forged" not in runtime.instruction


def test_classification_preserves_refs_without_aliasing_model_payload() -> None:
    report = {"issues": [{"id": "integrity-001", "path": "scene.xml"}]}
    item = {
        "issue_id": "integrity-001", "path": "scene.xml", "classification": "IRRELEVANT",
        "reason": "任务只分析 README.md。", "classification_evidence_ref_ids": ["user:scope"],
    }
    result, errors, gap = classify_integrity_issues(report, [item])
    assert not errors and not gap
    item["classification_evidence_ref_ids"].append("user:forged")
    assert result["issues"][0]["classification_evidence_ref_ids"] == ["user:scope"]
    assert "classification_evidence_ref_ids" not in report["issues"][0]
