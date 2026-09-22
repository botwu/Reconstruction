from __future__ import annotations

from pathlib import Path

import pytest

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
    environment = build_environment_contract(
        workspace_root=workspace,
        sufficiency={"status": "READY", "errors": [], "integrity_report": {"issues": []}},
    )
    environment["execution_readiness"] = "PROBED"
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
    environment = build_environment_contract(
        workspace_root=workspace,
        sufficiency={"status": "READY", "errors": [], "integrity_report": {"issues": []}},
    )
    environment["execution_readiness"] = "PROBED"
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
