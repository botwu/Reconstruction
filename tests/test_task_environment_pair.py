from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.task_environment import (
    TASK_ENVIRONMENT_PAIR_SCHEMA,
    TaskEnvironmentPairError,
    build_task_environment_pair,
    validate_task_environment_pair,
    write_task_environment_pair,
)


def _inputs(tmp_path: Path):
    replay = replay_from_timeline(
        [
            {
                "call_id": "read-1",
                "name": "read",
                "arguments": {"path": "src/main.py"},
                "result_text": "print('ok')\n",
            },
            {
                "call_id": "write-1",
                "name": "write",
                "arguments": {"path": "src/main.py", "content": "print('changed')\n"},
                "result_text": "",
            },
        ],
        tmp_path / "tasks/task-1/initial_workspace",
    )
    source_task = {
        "task_id": "task-1",
        "user_texts": ["请读取入口文件"],
        "message_indices": [0],
    }
    intent = {
        "status": "READY",
        "prompt_version": "intent-v1",
        "task": {
            "task_id": "task-1",
            "task_instruction": "请读取入口文件并保留可验证的入口内容",
            "core_objective": "读取入口文件",
            "acceptance_obligations": [
                {"id": "obl-1", "text": "读取入口", "evidence_ref_ids": ["user:0"]},
            ],
            "environment_bindings": [
                {
                    "obligation_id": "obl-1",
                    "required_paths": ["src/main.py"],
                    "observable": "入口内容可读",
                    "verifier_kind": "FILE",
                }
            ],
            "success_criteria": ["入口可读"],
            "mandatory_constraints": [],
            "prohibitions": [],
        },
    }
    source = {
        "schema_version": "traceforge.reconstruction-source.v3",
        "source_ref": "session.jsonl:1",
        "line_sha256": "a" * 64,
    }
    result = {
        "status": "PENDING_EXECUTION",
        "stopped_at": "verification",
        "execution_support_route": {"route": "TERMINAL_FILE", "env_origin": "REPLAYED"},
        "completion": {
            "status": "READY",
            "candidates": [
                {
                    "decision": "READY",
                    "workspace": str(
                        tmp_path / "tasks/task-1/completion/candidates/000/workspace"
                    ),
                }
            ],
        },
        "workspace": str(tmp_path / "tasks/task-1/completion/candidates/000/workspace"),
        "env_root": str(tmp_path / "tasks/task-1/completion/candidates/000"),
        "selected_index": 0,
        "sufficiency": {"status": "READY", "label": "SUFFICIENT"},
        "verification": {"status": "PENDING_EXECUTION", "sft_eligible": False},
    }
    return source_task, intent, source, replay, result


def test_pair_records_q_environment_and_relative_provenance(tmp_path: Path) -> None:
    source_task, intent, source, replay, result = _inputs(tmp_path)
    pair = build_task_environment_pair(
        source_task=source_task,
        intent=intent,
        source=source,
        replay=replay,
        result=result,
        root=tmp_path,
    )
    assert pair["schema_version"] == TASK_ENVIRONMENT_PAIR_SCHEMA
    assert pair["q"]["source_instruction"] == "请读取入口文件"
    assert pair["q"]["execution_instruction"]
    assert pair["environment"]["replayed_file_count"] == 1
    assert pair["environment"]["withheld_change_count"] == 1
    assert pair["environment"]["completed_workspace_ref"].startswith("tasks/task-1/")
    assert not pair["environment"]["completed_workspace_ref"].startswith("/")
    result["environment_contract"] = {"status": "READY", "execution_readiness": "PROBED"}
    pair = build_task_environment_pair(
        source_task=source_task,
        intent=intent,
        source=source,
        replay=replay,
        result=result,
        root=tmp_path,
    )
    assert pair["environment"]["execution_readiness"] == "PROBED"
    assert pair["environment"]["environment_contract_ref"] == "tasks/task-1/environment_contract.json"
    validate_task_environment_pair(pair)
    output = write_task_environment_pair(tmp_path / "tasks/task-1", pair)
    assert json.loads(output.read_text(encoding="utf-8"))["task_id"] == "task-1"


def test_pair_records_review_without_inventing_instruction(tmp_path: Path) -> None:
    source_task, _, source, replay, _ = _inputs(tmp_path)
    review = {
        "status": "REVIEW",
        "errors": ["INTENT_REVIEW"],
        "execution_support_route": {"env_origin": "REPLAYED"},
    }
    pair = build_task_environment_pair(
        source_task=source_task,
        intent={"status": "REVIEW", "tasks": []},
        source=source,
        replay=replay,
        result=review,
        root=tmp_path,
    )
    assert pair["q"]["execution_instruction"] is None
    assert pair["q"]["intent_status"] == "REVIEW"
    assert pair["environment"]["verification_status"] == "NOT_RUN"


def test_pair_rejects_absolute_artifact_reference(tmp_path: Path) -> None:
    source_task, intent, source, replay, result = _inputs(tmp_path)
    result["workspace"] = "/outside/workspace"
    with pytest.raises(TaskEnvironmentPairError):
        build_task_environment_pair(
            source_task=source_task,
            intent=intent,
            source=source,
            replay=replay,
            result=result,
            root=tmp_path,
        )


def test_variant_pair_uses_executed_task_for_delivery_q(tmp_path: Path) -> None:
    source_task, intent, source, replay, result = _inputs(tmp_path)
    variant = json.loads(json.dumps(intent["task"]))
    variant["task_id"] = "variant_abc123"
    variant["task_instruction"] = "请修改入口并通过变体验收"
    variant["core_objective"] = "在补全环境中修改入口"
    variant["acceptance_obligations"][0]["text"] = "入口内容应为 changed"
    variant["success_criteria"] = ["入口内容应为 changed"]
    result["status"] = "READY_VARIANT"
    result["executed_task"] = variant
    pair = build_task_environment_pair(
        source_task=source_task,
        intent=intent,
        source=source,
        replay=replay,
        result=result,
        root=tmp_path,
    )
    assert pair["q"]["source_task_id"] == "task-1"
    assert pair["q"]["executed_task_id"] == "variant_abc123"
    assert pair["q"]["projection_mode"] == "environment_grounded_variant"
    assert pair["q"]["execution_instruction"] == "请修改入口并通过变体验收"
    assert pair["q"]["acceptance_obligations"][0]["text"] == "入口内容应为 changed"
