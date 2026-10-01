"""将重建 Task Bundle 映射为 Harbor/AGS 可执行边界。

本模块只做协议适配和边界审计，不生成任务内容、不调用模型、不启动 AGS。
Task Bundle 的业务正确性仍由独立 verifier 负责。
"""

from __future__ import annotations

import hashlib
import importlib
import json
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


def validate_search_delivery(task_dir: Path | str) -> dict[str, Any]:
    """核对显式 search 任务及其完整交付哈希，不附加文件评分器。"""
    root = Path(task_dir).resolve()
    try:
        config = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
        delivery_path = root.parent / "delivery.json"
        delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise HarborAgsAdapterError("search 交付配置或 delivery.json 无法读取") from exc
    metadata = config.get("metadata")
    task = config.get("task")
    if not isinstance(delivery, dict) or not isinstance(metadata, dict) or not isinstance(task, dict):
        raise HarborAgsAdapterError("search 交付配置必须为对象")
    if (metadata.get("domain") != "search"
            or config.get("schema_version") != "1.4"
            or delivery.get("schema_version") != "traceforge.harbor-delivery.v1"
            or delivery.get("domain") != "search"
            or delivery.get("response_acceptance") != "NOT_ASSESSED"
            or delivery.get("rollout_args") != ["--disable-verification"]):
        raise HarborAgsAdapterError("search 交付必须显式声明领域与未自动验收状态")
    for name in ("instruction.md", "workspace/evidence.json"):
        if not (root / name).is_file():
            raise HarborAgsAdapterError(f"search 交付缺少 {name}")
    for name in ("environment", "tests"):
        if not (root / name).is_dir():
            raise HarborAgsAdapterError(f"search 交付缺少 {name}/")
    instruction = (root / "instruction.md").read_text(encoding="utf-8")
    name = task.get("name")
    if not instruction.strip() or not isinstance(name, str) or not name.strip():
        raise HarborAgsAdapterError("search 任务名称或说明为空")
    files = _tree(root)["files"]
    actual = {entry["path"]: entry["sha256"] for entry in files}
    if actual != delivery.get("task_file_sha256"):
        raise HarborAgsAdapterError("search 交付文件与 delivery.json 哈希不一致")
    return {
        "domain": "search", "task_bundle_root": str(root), "task_name": name,
        "schema_version": "traceforge.search-harbor-input.v1",
        "task_toml_sha256": actual["task.toml"],
        "instruction_sha256": actual["instruction.md"],
        "delivery_sha256": _sha256(delivery_path),
        "workspace": _tree(root / "workspace"),
        "environment": _tree(root / "environment"),
        "tests": _tree(root / "tests"),
        "response_acceptance": "NOT_ASSESSED",
    }


def validate_bundle_layout(task_dir: Path | str) -> dict[str, Any]:
    """执行不依赖 Harbor 安装的 Task Bundle 结构检查。"""
    root = Path(task_dir).resolve()
    if not root.is_dir():
        raise HarborAgsAdapterError(f"Task Bundle 目录不存在: {root}")
    try:
        config = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise HarborAgsAdapterError("task.toml 无法解析") from exc
    metadata = config.get("metadata")
    if isinstance(metadata, dict) and metadata.get("domain") == "search":
        return validate_search_delivery(root)
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


def _local_bundle_contract(root: Path) -> dict[str, Any]:
    """为没有安装 Harbor 源码的确定性单元测试生成最小绑定摘要。"""
    try:
        task_config = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise HarborAgsAdapterError("task.toml 无法解析") from exc
    task = task_config.get("task")
    name = task.get("name") if isinstance(task, Mapping) else None
    if not isinstance(name, str) or not name:
        raise HarborAgsAdapterError("task.toml 缺少 task.name")
    layout = validate_bundle_layout(root)
    return {
        "schema_version": "traceforge-task-bundle-local-summary/v1",
        "task_name": name,
        "task_toml_sha256": layout["task_toml_sha256"],
        "instruction_sha256": layout["instruction_sha256"],
        "workspace": layout["workspace"],
        "environment": layout["environment"],
        "solution": layout["solution"],
        "tests": layout["tests"],
        "harbor_task_checksum": hashlib.sha256(
            json.dumps(layout, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def validate_harbor_bundle(
    task_dir: Path | str, *, harbor_root: Path | str | None = None
) -> dict[str, Any]:
    """调用 Harbor 校验；测试替身缺少 Harbor 源码时只返回本地摘要。"""
    root = Path(task_dir).resolve()
    layout = validate_bundle_layout(root)
    if layout.get("domain") == "search":
        return layout
    external_root = Path(harbor_root).resolve() if harbor_root is not None else None
    if external_root is None or not (external_root / "src").is_dir():
        return _local_bundle_contract(root)
    try:
        with _harbor_import_path(external_root):
            module = importlib.import_module("harbor_ags.task_bundle")
            result = module.validate_task_bundle(root, require_control=True)
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
    if layout.get("domain") == "search":
        raise HarborAgsAdapterError("search 请使用 prepare-rollout，边界计划仅适用于文件评分任务")
    harbor_validation = None
    validation_status = "LOCAL_LAYOUT_ONLY"
    if harbor_root is not None:
        harbor_validation = validate_harbor_bundle(root, harbor_root=Path(harbor_root))
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
    "validate_search_delivery",
]
