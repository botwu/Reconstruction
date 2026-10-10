"""在新目录重试一个既有原生任务的后审；不恢复作者或重新执行 solver。"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from traceforge.harbor_ags.results import read_rollout_results
from traceforge.harbor_ags.rollout import load_verified_rollout_plan
from traceforge.reconstruction.agents import AgentRuntime
from traceforge.reconstruction.agents.session import workspace_tree_hash
from traceforge.reconstruction.environment_bindings import observed_body_paths
from traceforge.reconstruction.python_runtime import validate_python_runtime
from traceforge.reconstruction.researcher import ReconstructionRuntime
from traceforge.reconstruction.session_source import indexed_session
from traceforge.reconstruction.task_fit import build_environment_contract, build_task_contract
from traceforge.reconstruction.terminal_rollout_review import (
    build_terminal_rollout_evidence,
    terminal_review_outcome,
)
from traceforge.reconstruction.workspace_sufficiency import run_workspace_sufficiency
from traceforge.task_instruction import render_task_instruction


def _inside(path: Path | str, root: Path) -> Path:
    path = Path(path).absolute()
    if (
        not path.is_relative_to(root)
        or not path.resolve().is_relative_to(root)
        or any(p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents)
    ):
        raise ValueError(f"后审输入路径越界或包含符号链接：{path}")
    return path


def _read(path: Path | str, root: Path, hashes: dict[str, str]) -> dict[str, Any]:
    path = _inside(path, root)
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"后审输入必须是对象：{path}")
    hashes[str(path)] = hashlib.sha256(raw).hexdigest()
    return value


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare_review_retry(
    *,
    manifest_path: Path,
    task_id: str,
    output_root: Path,
) -> dict[str, Any]:
    """离线认证原任务及完整原生证据；任何不一致均在模型和沙箱创建前失败。"""
    root = manifest_path.absolute().parent
    output = output_root.absolute()
    if (
        output.exists()
        or output.resolve().is_relative_to(root.resolve())
        or root.resolve().is_relative_to(output.resolve())
    ):
        raise ValueError("后审重试必须使用原运行之外、尚不存在的新目录")
    hashes: dict[str, str] = {}
    manifest = _read(manifest_path, root, hashes)
    source = _read(root / "reconstruction_source.json", root, hashes)
    domain = source.get("input_domain")
    expected = "rollout_review" if domain == "terminal" else "researcher_review"
    rows = [t for t in manifest.get("tasks", []) if t.get("task_id") == task_id]
    if (
        domain not in {"terminal", "search"}
        or len(rows) != 1
        or rows[0].get("status") not in {"REVIEW", "BLOCKED"}
        or rows[0].get("stopped_at") != expected
        or rows[0].get("acceptance") != "NOT_ASSESSED"
        or task_id not in source.get("selected_task_ids", [])
        or (source.get("session_parser") or {}).get("status") != "READY"
    ):
        raise ValueError("只支持同一已解析任务的未评分原生后审失败")
    previous = rows[0]
    source_sha = (manifest.get("source") or {}).get("line_sha256", manifest.get("source_sha256"))
    if source_sha != source.get("line_sha256"):
        raise ValueError("后审原始行身份不一致")
    parser = _read(root / "session_parser/receipt.json", root, hashes)
    request = _read(Path(parser["accepted_attempt"]) / "request.json", root, hashes)
    indexed = indexed_session(source["raw_session"])
    if (
        parser.get("status") not in {"READY", "VALID"}
        or parser.get("source_sha256") != source_sha
        or hashlib.sha256(request["prompt"].encode()).hexdigest() != parser.get("input_sha256")
        or json.loads(request["prompt"])["session"]
        != {**indexed["session_fields"], "messages": indexed["messages"]}
    ):
        raise ValueError("后审原始 session 与已捕获的完整解析请求不一致")
    intent = _read(root / "intent/intent.json", root, hashes)
    items = [
        item
        for item in intent.get("tasks", [])
        if (item.get("task") or {}).get("task_id") == task_id
    ]
    if len(items) != 1 or items[0].get("status") != "READY" or items[0].get("errors"):
        raise ValueError("后审任务缺少原 READY intent")
    task = items[0]["task"]
    record = _read(previous["rollout_record"], root, hashes)
    plan_root = _inside(record["plan"], root)
    plan = load_verified_rollout_plan(plan_root)
    plan_raw = _read(plan_root / "rollout_plan.json", root, hashes)
    receipt = _read(plan_root / "run_receipt.json", root, hashes)
    results = record["results"]
    binding = results["input_binding"]
    trials = results.get("trials") or []
    if (
        record.get("errors")
        or record["execution"].get("status") != "COMPLETED"
        or results.get("execution_completed") is not True
        or not trials
        or len(trials) != plan["agent"]["trials"]
        or len(trials) != results.get("expected_trial_count")
        or plan.get("domain") != domain
        or plan["verifier"]["enabled"] is not False
        or receipt.get("status") != "COMPLETED"
        or receipt.get("returncode") != 0
        or binding.get("run_id") != plan_raw["run_id"]
        or binding.get("plan_sha256") != hashes[str(plan_root / "rollout_plan.json")]
        or binding.get("run_receipt_sha256") != hashes[str(plan_root / "run_receipt.json")]
    ):
        raise ValueError("原生执行未完整结束或与冻结计划不一致")
    job = _inside(Path(plan["jobs_root"]) / plan["job_name"], root)
    verified = read_rollout_results(
        job,
        expected_trial_count=len(trials),
        domain=domain,
        verifier_enabled=False,
        expected_task=_inside(trials[0]["native_trial"]["receipt"]["input_task"], root),
    )
    if not verified["execution_completed"]:
        raise ValueError(f"原生 job 或清理认证失败：{verified['quality_gate']['reasons']}")
    current_trials = {row["trial_name"]: row["native_trial"] for row in verified["trials"]}
    native = []
    slots = []
    seen = set()
    for row in trials:
        trial_root = _inside(Path(row["result_path"]).parent, root)
        old = row["native_trial"]
        task_path = _inside(old["receipt"]["input_task"], root)
        slot = _inside(binding["trial_task_paths"][trial_root.name], root)
        if trial_root.parent != job or trial_root in seen or trial_root.name != old["trial"]:
            raise ValueError("原生 trial 身份重复或输入任务不一致")
        config = _read(trial_root / "config.json", root, hashes)
        if Path(config["task"]["path"]) != slot:
            raise ValueError("原生 trial 实际输入槽位与计划不一致")
        seen.add(trial_root)
        current = current_trials[trial_root.name]
        if not current["completed"] or current["errors"]:
            raise ValueError(f"原生完整捕获认证失败：{current['errors']}")
        # 新 reader 可补充派生字段；旧捕获的字节和已记录回答不允许改变。
        if any(
            current["receipt"]["evidence_files"].get(k) != v
            for k, v in old["receipt"]["evidence_files"].items()
        ) or any(
            current.get(k) != old.get(k)
            for k in ("answer", "tool_events", "trajectory")
            if k in old
        ):
            raise ValueError("原生捕获与原回执不一致")
        hashes.update(
            {
                str(_inside(trial_root / name, root)): digest
                for name, digest in current["receipt"]["evidence_files"].items()
            }
        )
        instruction_path = _inside(task_path / "instruction.md", root)
        expected_instruction = (
            render_task_instruction(task) if domain == "terminal" else task["task_instruction"]
        )
        if not instruction_path.read_text().startswith(expected_instruction):
            raise ValueError("后审任务说明与实际 native 输入不一致")
        native.append(current)
        slots.append(slot)
    data = dict(
        root=root,
        manifest_path=manifest_path.absolute(),
        output=output,
        source=source,
        task=task,
        previous=previous,
        domain=domain,
        native_trials=native,
        input_hashes=hashes,
    )
    if domain == "terminal":
        if (
            previous.get("task_contract") != build_task_contract(task=task)
            or previous.get("executed_task") != task
        ):
            raise ValueError("原任务合同与实际执行任务不一致")
        candidate = previous["completion"]["candidates"][previous["selected_index"]]
        workspace = _inside(candidate["workspace"], root)
        old_review = _read(previous["rollout_review"]["sufficiency_path"], root, hashes)
        evidence = build_terminal_rollout_evidence(native)
        if evidence != _read(previous["rollout_evidence_path"], root, hashes):
            raise ValueError("完整终端证据与原后审输入不一致")
        replay = _read(Path(previous["rollout_evidence_path"]).parent / "replay.json", root, hashes)
        inventory = workspace_tree_hash(workspace)
        if (
            not inventory
            or inventory != old_review["workspace_hashes"]
            or any(
                inventory != {f["path"]: f["sha256"] for f in trial["initial_files"]}
                for trial in evidence["trials"]
            )
        ):
            raise ValueError("候选初态与原后审或 native 初态不一致")
        bundle = _inside(workspace.parent / "python_runtime", root)
        has_runtime = bundle.exists()
        runtime_manifest = None
        for runtime_path in (bundle, *(slot / "environment/python_runtime" for slot in slots)):
            runtime_path = _inside(runtime_path, root)
            if runtime_path.exists() != has_runtime:
                raise ValueError("候选依赖包与原生输入槽位的有无不一致")
            if not has_runtime:
                continue
            current_manifest = validate_python_runtime(runtime_path, workspace / "requirements.txt")
            if runtime_manifest is not None and current_manifest != runtime_manifest:
                raise ValueError("候选依赖包与原生输入槽位的冻结内容不一致")
            runtime_manifest = current_manifest
            if _read(runtime_path / "manifest.json", root, hashes) != current_manifest:
                raise ValueError("依赖包认证期间发生变化")
            hashes.update(
                {
                    str(_inside(runtime_path / item["file"], root)): item["sha256"]
                    for item in current_manifest["files"]
                }
            )
        hashes.update({str(workspace / name): digest for name, digest in inventory.items()})
        data.update(
            workspace=workspace,
            workspace_hashes=inventory,
            evidence=evidence,
            context=old_review["reconstruction_context"],
            # 仅恢复原评估所用的证据视图，不重新回放轨迹。
            replay=SimpleNamespace(
                files=[SimpleNamespace(**row) for row in replay["files"]],
                partial_evidence=replay["partial_evidence"],
            ),
            python_runtime=bundle if has_runtime else None,
        )
    else:
        environment = _read(previous["environment_path"], root, hashes)
        checkpoint = _inside(previous["researcher_checkpoint"], root)
        if (
            environment.get("status") != "READY"
            or environment.get("task") != task
            or environment.get("source_sha256") != source_sha
        ):
            raise ValueError("检索环境与原任务不一致")
        for trial in native:
            task_path = Path(trial["receipt"]["input_task"])
            supplied = _read(task_path / "workspace/evidence.json", root, hashes)
            if supplied != {
                key: environment.get(key, []) for key in ("captures", "live_references")
            }:
                raise ValueError("检索材料与实际 native 初态不一致")
            context = {
                "original_user_texts": (task.get("source_task") or {}).get("user_texts", []),
                "context_messages": environment.get("context_messages", []),
                "limitations": environment.get("limitations", []),
            }
            if (
                json.dumps(context, ensure_ascii=False, indent=2)
                not in (task_path / "instruction.md").read_text()
            ):
                raise ValueError("检索历史输入与实际 native 指令不一致")
        snapshot = _read(checkpoint, root, hashes)
        for name, digest in snapshot["files"].items():
            path = _inside(checkpoint.parent / name, root)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("检索检查点原件不一致")
            hashes[str(path)] = digest
        data.update(environment=environment, checkpoint=checkpoint)
        from traceforge.reconstruction.search_environment import validate_search_review_checkpoint

        with tempfile.TemporaryDirectory(prefix="review-check-") as scratch:
            validate_search_review_checkpoint(
                source=source, task=task, checkpoint=checkpoint, output_root=Path(scratch)
            )
    return data


def run_review_retry(
    prepared: dict[str, Any],
    *,
    agent: AgentRuntime,
    runtime_factory: Any = None,
) -> Path:
    """只执行独立后审；发现需返修则交还明确结果，不暗中重跑作者或 solver。"""
    data = prepared
    root = data["output"]
    for path, digest in data["input_hashes"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise ValueError(f"后审启动前输入改变：{path}")
    root.mkdir(parents=True, exist_ok=False)
    previous = data["previous"]
    result = copy.deepcopy(previous)
    result["review_retry"] = {
        "from_manifest": str(data["manifest_path"]),
        "previous_status": previous["status"],
        "previous_errors": previous.get("errors", []),
        "native_reused": True,
    }
    binding = {
        "operation": "retry-review",
        "reviewer_model": agent.model_name,
        "task_id": data["task"]["task_id"],
        "source_sha256": data["source"]["line_sha256"],
        "input_sha256": data["input_hashes"],
    }
    _save(root / "retry-input.json", binding)
    try:
        if data["domain"] == "terminal":
            if runtime_factory is None:
                raise ValueError("terminal 后审必须提供正式隔离运行时")
            workspace = root / "input/workspace"
            shutil.copytree(data["workspace"], workspace)
            if workspace_tree_hash(workspace) != data["workspace_hashes"]:
                raise ValueError("复制后的候选初态不一致")
            bundle = data["python_runtime"]
            if bundle is not None:
                shutil.copytree(bundle, root / "input/python_runtime")
                bundle = root / "input/python_runtime"
                validate_python_runtime(bundle, workspace / "requirements.txt")
            researcher = ReconstructionRuntime(
                agent,
                source=data["source"],
                task=data["task"],
                runtime_factory=runtime_factory,
                python_runtime=bundle,
            )
            diagnosis = root / "downstream_environment_review"
            judge = run_workspace_sufficiency(
                task=data["task"],
                workspace_root=workspace,
                agent=researcher,
                output_root=diagnosis,
                observed_paths=observed_body_paths(data["source"]),
                reconstruction_context=data["context"],
                rollout_evidence=data["evidence"],
                repair_feedback={
                    "downstream_failure": {"stage": "rollout"},
                    "instruction": "复核原候选初态和既有实跑，区分环境缺口与 solver 错误。"
                    "不要修改任务、候选或 solver 终态，不提前实现用户目标。",
                },
            )
            environment = build_environment_contract(
                workspace_root=workspace,
                env_root=workspace.parent,
                sufficiency=judge,
                replay=data["replay"],
            )
            result.update(
                terminal_review_outcome(judge, environment, diagnosis / "sufficiency.json")
            )
            _save(root / "rollout-review.json", result["rollout_review"])
        else:
            from traceforge.reconstruction.search_environment import retry_search_rollout_review

            result.update(
                retry_search_rollout_review(
                    source=data["source"],
                    task=data["task"],
                    environment=data["environment"],
                    checkpoint=data["checkpoint"],
                    native_trials=data["native_trials"],
                    agent=agent,
                    output_root=root,
                )
            )
        _save(root / "result.json", result)
        return root / "result.json"
    finally:
        changed = [
            path
            for path, digest in data["input_hashes"].items()
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
        ]
        _save(root / "retry-input-audit.json", {"unchanged": not changed, "changed": changed})
        if changed:
            raise ValueError("后审重试改变了原绑定输入")
