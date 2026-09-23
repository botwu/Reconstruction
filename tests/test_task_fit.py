from __future__ import annotations

from pathlib import Path

import pytest

from traceforge.reconstruction.agents.session import workspace_tree_hash
from traceforge.reconstruction.task_fit import (
    ENVIRONMENT_UNRECONSTRUCTABLE,
    build_environment_contract,
    build_task_contract,
    build_task_variant,
    fit_task_environment,
)


def _task(*, required: str = "src") -> dict:
    return {
        "task_id": "task-1",
        "task_instruction": "在项目中完成入口适配",
        "core_objective": "完成入口适配",
        "acceptance_obligations": [{"id": "obl-1", "text": "入口可运行"}],
        "environment_bindings": [
            {
                "obligation_id": "obl-1",
                "required_paths": [required],
                "observable": "入口可运行",
                "verifier_kind": "FILE",
            }
        ],
        "success_criteria": ["入口可运行"],
    }


def _probed_environment(workspace: Path) -> dict:
    inventory = workspace_tree_hash(workspace)
    probes = []
    checks = []
    for kind in ("load", "reset", "dependency"):
        probe_id = f"probe-{kind}"
        count = 2 if kind == "reset" else 1
        executions = [
            {"exit_code": 0, "timed_out": False, "workspace_after": inventory}
            for _ in range(count)
        ]
        probes.append({
            "probe_id": probe_id, "purpose": kind, "status": "PASS",
            "environment_unchanged": True, "reproducible": True,
            "workspace_before": inventory, "workspace_after": inventory,
            "executions": executions,
        })
        checks.append({"kind": kind, "probe_ids": [probe_id], "reason": "固定回归探针"})
    return build_environment_contract(
        workspace_root=workspace,
        sufficiency={
            "status": "READY", "errors": [],
            "integrity_report": {"issues": []},
            "workspace_hashes": inventory,
            "environment_probes": probes,
            "environment_checks": checks,
        },
    )


def test_missing_referenced_asset_is_unreconstructable_and_cannot_variant(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scene.xml").write_text('<include file="missing.xml"/>\n', encoding="utf-8")
    sufficiency = {
        "status": "REVIEW",
        "label": "SUFFICIENT",
        "errors": ["INTEGRITY_ISSUE_UNKNOWN"],
        "integrity_report": {
            "issues": [
                {
                    "id": "integrity-001",
                    "code": "REFERENCED_ASSET_MISSING",
                    "path": "scene.xml",
                    "reason": "missing.xml is absent",
                }
            ]
        },
    }
    environment = build_environment_contract(workspace_root=workspace, sufficiency=sufficiency)
    task = build_task_contract(task=_task())
    fit = fit_task_environment(environment=environment, task=task)
    assert environment["status"] == ENVIRONMENT_UNRECONSTRUCTABLE
    assert fit["decision"] == ENVIRONMENT_UNRECONSTRUCTABLE
    assert not fit["variant_eligible"]
    with pytest.raises(Exception):
        build_task_variant(parent_task=task, environment=environment, fit=fit, proposal={})


def test_infrastructure_failure_is_review_not_unreconstructable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print(1)\n", encoding="utf-8")
    environment = build_environment_contract(
        workspace_root=workspace,
        sufficiency={
            "status": "REVIEW",
            "errors": ["READ_ONLY_PROBE_NOT_CONFIRMED"],
            "integrity_report": {"issues": []},
        },
    )
    environment["execution_readiness"] = "PROBED"
    assert environment["status"] == "REVIEW"


def test_directory_binding_and_variant_are_environment_grounded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/main.py").write_text("print(1)\n", encoding="utf-8")
    environment = _probed_environment(workspace)
    task = build_task_contract(task=_task())
    fit = fit_task_environment(environment=environment, task=task)
    assert fit["decision"] == "READY_ORIGINAL"

    incompatible = fit | {
        "decision": "INCOMPATIBLE",
        "status": "INCOMPATIBLE",
        "variant_eligible": True,
        "reason": "原始目标对象不可用",
    }
    incompatible["requirements"][0]["status"] = "UNSATISFIED"
    variant = build_task_variant(
        parent_task=task,
        environment=environment,
        fit=incompatible,
        proposal={
            "task_instruction": "在项目中完成入口适配（使用现有入口）",
            "changed_requirements": [
                {
                    "obligation_id": "obl-1",
                    "transformation": "SCOPE_REDUCTION",
                    "text": "读取现有入口",
                    "reason": "原始目标对象不可用",
                    "environment_binding": {
                        "obligation_id": "obl-1",
                        "required_paths": ["src"],
                        "observable": "入口内容可读",
                        "verifier_kind": "FILE",
                    },
                }
            ],
        },
    )
    assert variant is not None
    assert variant["status"] == "PROPOSED"
    assert variant["parent_task_id"] == "task-1"


def test_variant_rejects_unknown_path_or_core_intent_change(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print(1)\n", encoding="utf-8")
    environment = _probed_environment(workspace)
    task = build_task_contract(task=_task(required="main.py"))
    fit = fit_task_environment(environment=environment, task=task) | {
        "decision": "INCOMPATIBLE",
        "status": "INCOMPATIBLE",
        "variant_eligible": True,
    }
    fit["requirements"][0]["status"] = "UNSATISFIED"
    proposal = {
        "task_instruction": "完全改做数据库迁移",
        "changed_requirements": [
            {
                "obligation_id": "obl-1",
                "transformation": "SCOPE_REDUCTION",
                "text": "完成数据库迁移",
                "reason": "任务不可完成",
                "environment_binding": {
                    "obligation_id": "obl-1",
                    "required_paths": ["unknown.py"],
                    "observable": "完成",
                    "verifier_kind": "FILE",
                },
            }
        ],
    }
    with pytest.raises(Exception):
        build_task_variant(parent_task=task, environment=environment, fit=fit, proposal=proposal)


def test_probe_pass_without_workspace_execution_receipt_cannot_make_environment_ready(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.py").write_text("print(1)\n", encoding="utf-8")
    inventory = workspace_tree_hash(workspace)
    environment = build_environment_contract(
        workspace_root=workspace,
        sufficiency={
            "status": "READY",
            "errors": [],
            "integrity_report": {"issues": []},
            "workspace_hashes": inventory,
            "environment_probes": [
                {
                    "probe_id": "fake-load",
                    "purpose": "load",
                    "status": "PASS",
                    "environment_unchanged": True,
                    "workspace_before": {},
                    "workspace_after": {},
                    "executions": [],
                }
            ],
            "environment_checks": [
                {"kind": "load", "probe_ids": ["fake-load"], "reason": "伪造收据"},
                {"kind": "reset", "probe_ids": ["fake-load"], "reason": "伪造收据"},
                {"kind": "dependency", "probe_ids": ["fake-load"], "reason": "伪造收据"},
            ],
        },
    )
    assert environment["status"] == "READY"
    assert environment["execution_readiness"] == "FAILED"
    assert any(error.startswith("ENVIRONMENT_PROBE_NOT_PASS") for error in environment["execution_errors"])


def test_ready_original_normalizes_unknown_with_bound_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/main.py").write_text("print(1)\n", encoding="utf-8")
    environment = _probed_environment(workspace)
    task = build_task_contract(task=_task(required="src"))
    fit = fit_task_environment(
        environment=environment,
        task=task,
        agent_fit={
            "decision": "ready_original",
            "requirements": [{
                "obligation_id": "obl-1",
                "status": "unknown",
                "reason": "目标行为是待实现能力，入口证据存在。",
                "evidence_paths": ["src"],
                "probe_ids": [],
            }],
        },
    )
    assert fit["decision"] == "READY_ORIGINAL"
    assert fit["requirements"][0]["status"] == "SATISFIED"
    assert fit["requirements"][0]["status_before_normalization"] == "UNKNOWN"


def test_non_file_obligation_does_not_require_fake_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/main.py").write_text("print(1)\n", encoding="utf-8")
    environment = _probed_environment(workspace)
    task = _task(required="src")
    task["acceptance_obligations"].append({"id": "obl-2", "text": "提交分析"})
    task["environment_bindings"].append({
        "obligation_id": "obl-2",
        "required_paths": [],
        "observable": "提交分析",
        "verifier_kind": "NON_FILE",
    })
    fit = fit_task_environment(
        environment=environment,
        task=build_task_contract(task=task),
        agent_fit={
            "decision": "READY_ORIGINAL",
            "requirements": [
                {"obligation_id": "obl-1", "status": "UNKNOWN", "reason": "入口存在。", "evidence_paths": ["src"], "probe_ids": []},
                {"obligation_id": "obl-2", "status": "UNKNOWN", "reason": "文字义务由后续验证。"},
            ],
        },
    )
    assert fit["decision"] == "REVIEW_TASK_FIT"
    assert "TASK_FIT_UNKNOWN:obl-1" not in fit["errors"]
    assert "TASK_FIT_UNKNOWN:obl-2" in fit["errors"]
    assert {item["status"] for item in fit["requirements"]} == {"SATISFIED", "UNKNOWN"}
