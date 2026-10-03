"""TraceForge 正式 Harbor Task Bundle 与 rubric 契约。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from dirhash import dirhash

TASK_BUNDLE_SCHEMA = "traceforge-harbor-task-bundle/v1"
RUBRIC_SCHEMA = "traceforge-task-rubric/v1"
_CRITERION_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class TaskBundleError(ValueError):
    """Task Bundle 或隐藏评分契约不合法。"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_contract(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise TaskBundleError(f"Task Bundle 不允许符号链接: {path}")
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise TaskBundleError(f"Task Bundle 不允许特殊文件: {path}")
        if (
            "__pycache__" in path.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name == ".DS_Store"
        ):
            raise TaskBundleError(f"Task Bundle 含不可复现的生成文件: {path}")
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        files.append(
            {
                "path": relative,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    encoded = json.dumps(
        files,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "files": files,
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def load_rubric(path: Path) -> dict[str, Any]:
    """读取并严格校验隐藏 rubric。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskBundleError(f"无法读取 rubric: {path}") from exc
    if not isinstance(value, Mapping):
        raise TaskBundleError("rubric 根节点必须是对象")
    if set(value) != {"schema_version", "aggregation", "criteria"}:
        raise TaskBundleError("rubric 根节点字段不符合正式 schema")
    if value.get("schema_version") != RUBRIC_SCHEMA:
        raise TaskBundleError("rubric schema_version 不匹配")
    if value.get("aggregation") != "weighted_sum":
        raise TaskBundleError("当前 rubric 只支持 weighted_sum")
    criteria = value.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise TaskBundleError("rubric.criteria 必须是非空数组")
    seen: set[str] = set()
    total_weight = 0.0
    normalized: list[dict[str, Any]] = []
    for index, criterion in enumerate(criteria):
        if not isinstance(criterion, Mapping) or set(criterion) != {
            "id",
            "weight",
            "description",
            "verifier",
        }:
            raise TaskBundleError(f"rubric.criteria[{index}] 字段不合法")
        criterion_id = criterion.get("id")
        if (
            not isinstance(criterion_id, str)
            or _CRITERION_ID_RE.fullmatch(criterion_id) is None
            or criterion_id in seen
        ):
            raise TaskBundleError(f"rubric.criteria[{index}].id 不合法或重复")
        weight = criterion.get("weight")
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or not 0.0 < float(weight) <= 1.0
        ):
            raise TaskBundleError(f"rubric.criteria[{index}].weight 不合法")
        description = criterion.get("description")
        if not isinstance(description, str) or not description.strip():
            raise TaskBundleError(f"rubric.criteria[{index}].description 不能为空")
        verifier = criterion.get("verifier")
        if not isinstance(verifier, str) or not verifier:
            raise TaskBundleError(f"rubric.criteria[{index}].verifier 不能为空")
        verifier_path = PurePosixPath(verifier)
        if verifier_path.is_absolute() or ".." in verifier_path.parts:
            raise TaskBundleError(f"rubric.criteria[{index}].verifier 路径不安全")
        seen.add(criterion_id)
        total_weight += float(weight)
        normalized.append(dict(criterion))
    if not math.isclose(total_weight, 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise TaskBundleError("rubric criteria 权重之和必须为 1")
    if (
        len(normalized) != 1
        or normalized[0]["id"] != "task"
        or float(normalized[0]["weight"]) != 1.0
        or normalized[0]["verifier"] != "grader.py"
    ):
        raise TaskBundleError(
            "Task Bundle v1 固定使用 task/1.0/grader.py 单 criterion 契约"
        )
    return {
        "schema_version": RUBRIC_SCHEMA,
        "aggregation": "weighted_sum",
        "criteria": normalized,
    }


def validate_task_bundle(
    task_dir: Path,
    *,
    require_control: bool = True,
) -> dict[str, Any]:
    """校验正式目录边界，并返回可写入 compile/certification 的摘要。"""

    task_dir = task_dir.resolve()
    required_files = ("task.toml", "instruction.md")
    required_dirs = ("workspace", "environment", "solution", "tests")
    allowed_root = {*required_files, *required_dirs}
    for path in task_dir.iterdir():
        if path.name not in allowed_root:
            raise TaskBundleError(f"Task Bundle 含未知根级条目: {path.name}")
    for name in required_files:
        path = task_dir / name
        if not path.is_file() or path.is_symlink():
            raise TaskBundleError(f"Task Bundle 缺少文件: {name}")
    for name in required_dirs:
        path = task_dir / name
        if not path.is_dir() or path.is_symlink():
            raise TaskBundleError(f"Task Bundle 缺少目录: {name}/")
    if not (task_dir / "instruction.md").read_text(encoding="utf-8"):
        raise TaskBundleError("instruction.md 不能为空")
    try:
        task_toml = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise TaskBundleError("task.toml 无法解析") from exc
    if task_toml.get("schema_version") != "1.4":
        raise TaskBundleError("task.toml 必须使用 Harbor schema 1.4")
    task_config = task_toml.get("task")
    task_config = task_config if isinstance(task_config, Mapping) else {}
    task_name = task_config.get("name")
    if not isinstance(task_name, str) or not task_name:
        raise TaskBundleError("task.toml 缺少 task.name")
    verifier_config = task_toml.get("verifier")
    verifier_config = verifier_config if isinstance(verifier_config, Mapping) else {}
    if verifier_config.get("environment_mode") != "separate":
        raise TaskBundleError("verifier.environment_mode 必须是 separate")
    verifier_environment = verifier_config.get("environment")
    verifier_environment = (
        verifier_environment if isinstance(verifier_environment, Mapping) else {}
    )
    if verifier_environment.get("network_mode") != "no-network":
        raise TaskBundleError("独立 verifier 必须使用 no-network")
    solve_sh = task_dir / "solution" / "solve.sh"
    if not solve_sh.is_file() or solve_sh.is_symlink():
        raise TaskBundleError("solution/solve.sh 缺失")

    tests_dir = task_dir / "tests"
    rubric_path = tests_dir / "rubric.json"
    test_sh = tests_dir / "test.sh"
    if not test_sh.is_file() or test_sh.is_symlink():
        raise TaskBundleError("tests/test.sh 缺失")
    if test_sh.read_text(encoding="utf-8") != (
        "#!/bin/sh\nset -eu\n\npython3 /tests/grader.py\n"
    ):
        raise TaskBundleError("Task Bundle v1 的 test.sh 必须固定执行 /tests/grader.py")
    rubric = load_rubric(rubric_path)
    verifier_hashes: dict[str, str] = {}
    for criterion in rubric["criteria"]:
        relative = str(criterion["verifier"])
        verifier_path = tests_dir / relative
        if not verifier_path.is_file() or verifier_path.is_symlink():
            raise TaskBundleError(f"rubric verifier 不存在: tests/{relative}")
        verifier_hashes[relative] = _sha256(verifier_path)

    control_dir = tests_dir / "control"
    if require_control and (not control_dir.is_dir() or control_dir.is_symlink()):
        raise TaskBundleError("正式编译 Task 缺少 tests/control/")
    control = _tree_contract(control_dir) if control_dir.is_dir() else None
    contract: dict[str, Any] = {
        "schema_version": TASK_BUNDLE_SCHEMA,
        "task_name": task_name,
        "task_toml_sha256": _sha256(task_dir / "task.toml"),
        "instruction_sha256": _sha256(task_dir / "instruction.md"),
        "workspace": _tree_contract(task_dir / "workspace"),
        "environment": _tree_contract(task_dir / "environment"),
        "solution": _tree_contract(task_dir / "solution"),
        "rubric": {
            "sha256": _sha256(rubric_path),
            "schema_version": rubric["schema_version"],
            "aggregation": rubric["aggregation"],
            "criteria": [
                {"id": item["id"], "weight": float(item["weight"])}
                for item in rubric["criteria"]
            ],
        },
        "verifier_sha256": verifier_hashes,
        "test_sh_sha256": _sha256(test_sh),
        "tests": _tree_contract(tests_dir),
        "control": control,
        "harbor_task_checksum": dirhash(str(task_dir), "sha256"),
    }
    unsigned = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    contract["sha256"] = hashlib.sha256(unsigned).hexdigest()
    return contract


__all__ = [
    "RUBRIC_SCHEMA",
    "TASK_BUNDLE_SCHEMA",
    "TaskBundleError",
    "load_rubric",
    "validate_task_bundle",
]
