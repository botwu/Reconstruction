"""将重建 Task Bundle 映射为 Harbor/AGS 可执行边界。

本模块只做协议适配和边界审计，不生成任务内容、不调用模型、不启动 AGS。
Task Bundle 的业务正确性仍由独立 verifier 负责。
"""

from __future__ import annotations

import hashlib
import importlib
import sys
import tomllib
from collections.abc import Iterable, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)
from traceforge.trajectory.json_codec import canonical_json_bytes, stable_id

HARBOR_AGS_PLAN_SCHEMA = "traceforge.harbor-ags-boundary-plan.v1"
_PLAN_NAMESPACE = "harbor-ags-boundary-plan-v1"


class HarborAgsAdapterError(ValueError):
    """Task Bundle 无法映射到 Harbor/AGS 执行边界。"""


_REQUIRED_FILES = ("task.toml", "instruction.md")
_REQUIRED_DIRS = ("workspace", "environment", "solution", "tests")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise HarborAgsAdapterError(f"边界目录不允许符号链接: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        files.append(
            {"path": relative, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        )
    return {"files": files, "tree_sha256": hashlib.sha256(canonical_json_bytes(files)).hexdigest()}


def validate_bundle_layout(task_dir: Path | str) -> dict[str, Any]:
    """执行不依赖 Harbor 安装的 Task Bundle 结构检查。"""
    root = Path(task_dir).resolve()
    if not root.is_dir():
        raise HarborAgsAdapterError(f"Task Bundle 目录不存在: {root}")
    for name in _REQUIRED_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise HarborAgsAdapterError(f"缺少 Task Bundle 文件: {name}")
    for name in _REQUIRED_DIRS:
        path = root / name
        if not path.is_dir() or path.is_symlink():
            raise HarborAgsAdapterError(f"缺少 Task Bundle 目录: {name}/")
    if not (root / "instruction.md").read_text(encoding="utf-8").strip():
        raise HarborAgsAdapterError("instruction.md 不能为空")
    control = root / "tests" / "control"
    if not control.is_dir() or control.is_symlink():
        raise HarborAgsAdapterError("正式 Bundle 必须包含 tests/control/")
    test_sh = root / "tests" / "test.sh"
    grader = root / "tests" / "grader.py"
    rubric = root / "tests" / "rubric.json"
    for path in (test_sh, grader, rubric):
        if not path.is_file() or path.is_symlink():
            raise HarborAgsAdapterError(f"缺少 verifier 文件: {path.relative_to(root)}")
    if test_sh.read_text(encoding="utf-8") != "#!/bin/sh\nset -eu\n\npython3 /tests/grader.py\n":
        raise HarborAgsAdapterError("tests/test.sh 不符合 Harbor verifier 契约")
    try:
        task_config = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise HarborAgsAdapterError("task.toml 无法解析") from exc
    if task_config.get("schema_version") != "1.4":
        raise HarborAgsAdapterError("task.toml 必须声明 Harbor schema 1.4")
    verifier = task_config.get("verifier")
    verifier = verifier if isinstance(verifier, Mapping) else {}
    if verifier.get("environment_mode") != "separate":
        raise HarborAgsAdapterError("verifier 必须使用 separate environment")
    environment = verifier.get("environment")
    environment = environment if isinstance(environment, Mapping) else {}
    if environment.get("network_mode") != "no-network":
        raise HarborAgsAdapterError("verifier 必须使用 no-network")
    return {
        "task_bundle_root": str(root),
        "task_toml_sha256": _sha256(root / "task.toml"),
        "instruction_sha256": _sha256(root / "instruction.md"),
        "workspace": _tree(root / "workspace"),
        "environment": _tree(root / "environment"),
        "solution": _tree(root / "solution"),
        "tests": _tree(root / "tests"),
        "public_workspace_source": "workspace/",
        "runtime_definition_source": "environment/",
        "agent_visible_sources": ["instruction.md", "workspace/"],
        "runner_only_sources": ["task.toml", "environment/"],
        "reference_solution_source": "solution/",
        "verifier_source": "tests/",
        "hidden_control_source": "tests/control/",
    }


@contextmanager
def _harbor_import_path(harbor_root: Path | None):
    if harbor_root is None:
        yield
        return
    source = harbor_root.resolve() / "src"
    if not source.is_dir():
        raise HarborAgsAdapterError(f"harbor_ags 源码目录不存在: {source}")
    sys.path.insert(0, str(source))
    try:
        yield
    finally:
        with suppress(ValueError):
            sys.path.remove(str(source))


def _run_harbor_validator(task_dir: Path, *, harbor_root: Path | None) -> dict[str, Any]:
    """调用现有 harbor_ags.task_bundle.validate_task_bundle。"""
    try:
        with _harbor_import_path(harbor_root):
            module = importlib.import_module("harbor_ags.task_bundle")
            result = module.validate_task_bundle(task_dir, require_control=True)
    except Exception as exc:
        raise HarborAgsAdapterError(
            f"现有 harbor_ags 校验失败: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(result, Mapping):
        raise HarborAgsAdapterError("Harbor validator 返回值不是对象")
    return dict(result)


def _plan_id(layout: Mapping[str, Any], *, source_refs: tuple[str, ...]) -> str:
    identity = {
        "schema_version": HARBOR_AGS_PLAN_SCHEMA,
        "task_toml_sha256": layout["task_toml_sha256"],
        "instruction_sha256": layout["instruction_sha256"],
        "workspace_tree_sha256": layout["workspace"]["tree_sha256"],
        "environment_tree_sha256": layout["environment"]["tree_sha256"],
        "solution_tree_sha256": layout["solution"]["tree_sha256"],
        "tests_tree_sha256": layout["tests"]["tree_sha256"],
        "source_refs": source_refs,
    }
    return stable_id(_PLAN_NAMESPACE, identity)


def build_boundary_plan(
    task_dir: Path | str,
    *,
    output_root: Path | str,
    harbor_root: Path | str | None = None,
    source_refs: Iterable[str] = (),
) -> Path:
    """生成可审计的 Harbor/AGS 执行计划 artifact（dry-run）。"""
    root = Path(task_dir).resolve()
    refs = tuple(sorted(set(str(item) for item in source_refs if str(item))))
    layout = validate_bundle_layout(root)
    harbor_validation = None
    validation_status = "LOCAL_LAYOUT_ONLY"
    if harbor_root is not None:
        harbor_validation = _run_harbor_validator(root, harbor_root=Path(harbor_root))
        validation_status = "HARBOR_VALIDATED"
    plan_id = _plan_id(layout, source_refs=refs)
    plan = {
        "schema_version": HARBOR_AGS_PLAN_SCHEMA,
        "plan_id": plan_id,
        "status": "READY_FOR_ROLLOUT"
        if validation_status == "HARBOR_VALIDATED"
        else "LOCAL_LAYOUT_ONLY",
        "model_status": "NOT_RUN",
        "task_name": harbor_validation.get("task_name") if harbor_validation else None,
        "source_refs": list(refs),
        "validation": {
            "status": validation_status,
            "layout": layout,
            "harbor_task_bundle": harbor_validation,
        },
        "agent_surface": {
            "instruction_source": "instruction.md + runtime appendix",
            "public_workspace_source": "workspace/",
            "remote_workspace": "/home/user/workspace",
            "visible_roots": ["/home/user/workspace"],
            "excluded_sources": ["task.toml", "environment/", "solution/", "tests/"],
            "toolsets": ["file", "terminal"],
        },
        "verifier_surface": {
            "environment_mode": "separate",
            "network_mode": "no-network",
            "remote_root": "/tests",
            "verifier_source": "tests/grader.py",
            "control_source": "tests/control/",
            "agent_artifacts_source": "/logs/artifacts/traceforge/",
            "visible_to_agent": False,
        },
        "execution": {
            "environment_import": "harbor_ags.environment:AGSPrebuiltEnvironment",
            "agent_import": "harbor_ags.agent:LosslessHermesAgent",
            "agent_model_call": "DEFERRED",
            "max_retries": 0,
            "sandbox_delete_required": True,
            "cleanup_ledger": "_control/ags-sandbox-ledger.jsonl",
        },
        "sft_admission": {
            "requires_task_reward": True,
            "requires_independent_verifier": True,
            "requires_no_infra_error": True,
            "requires_trace_capture": True,
            "requires_reconstruction_provenance": True,
        },
    }
    workspace = ArtifactWorkspace(Path(output_root), plan_id)
    entries = [write_json_artifact(workspace.staging_path, "harbor_boundary_plan.json", plan)]
    entries.append(
        write_json_artifact(
            workspace.staging_path,
            "artifact_manifest.json",
            {
                "schema_version": "traceforge.harbor-ags-artifact-manifest.v1",
                "plan_id": plan_id,
                "files": artifact_entry_dicts(entries),
            },
        )
    )
    entries.append(
        write_json_artifact(
            workspace.staging_path,
            "run_receipt.json",
            {
                "schema_version": "traceforge.harbor-ags-plan-receipt.v1",
                "plan_id": plan_id,
                "artifact_manifest_sha256": entries[-1].sha256,
                "model_status": "NOT_RUN",
            },
        )
    )
    return workspace.publish()


__all__ = [
    "HARBOR_AGS_PLAN_SCHEMA",
    "HarborAgsAdapterError",
    "build_boundary_plan",
    "validate_bundle_layout",
]
