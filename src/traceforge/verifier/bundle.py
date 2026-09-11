"""把重建环境和隐藏验证器编译为 Harbor Task Bundle。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from traceforge.harbor_ags.adapter import validate_bundle_layout
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)

from .synthesis import VerifierCandidate


def compile_bundle(
    *,
    task: dict[str, Any],
    workspace_root: Path,
    verifier: VerifierCandidate,
    output_root: Path,
    solution_index: int = 0,
    mutation_index: int | None = None,
) -> Path:
    """生成带 hash 的 bundle；参考解只写入 solution，测试只写入 tests。"""
    instruction = task.get("task_instruction") or task.get("core_objective")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("缺少自足的任务指令")
    if not workspace_root.is_dir():
        raise ValueError("初始 workspace 不存在")
    tree: dict[str, str] = {}
    for path in sorted(workspace_root.rglob("*")):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("初始 workspace 不得包含符号链接或特殊文件")
        if path.is_file():
            tree[path.relative_to(workspace_root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    variants = verifier.oracle_solutions if mutation_index is None else verifier.mutation_solutions
    index = solution_index if mutation_index is None else mutation_index
    if index < 0 or index >= len(variants):
        raise ValueError("参考解索引超出范围")
    variant = variants[index]
    digest = hashlib.sha256(
        json.dumps(
            {
                "instruction": instruction,
                "tree": tree,
                "verifier": verifier.to_dict(),
                "variant": variant.name,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    artifact = ArtifactWorkspace(output_root, digest)
    entries = []
    try:
        root = artifact.staging_path / "task"
        shutil.copytree(workspace_root, root / "workspace")
        for name in ("environment", "solution", "tests/control"):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "instruction.md").write_text(instruction + "\n", encoding="utf-8")
        (root / "task.toml").write_text(
            'schema_version = "1.4"\n[task]\n'
            f'name = "traceforge/reconstructed-{digest[:16]}"\nversion = "1.0.0"\n'
            "[metadata]\nworkspace_snapshot = true\n"
            '[agent]\ntimeout_sec = 900.0\nuser = "user"\n'
            '[verifier]\ntimeout_sec = 120.0\nenvironment_mode = "separate"\nuser = "user"\n'
            '[verifier.environment]\nnetwork_mode = "no-network"\n'
            '[environment]\nos = "linux"\nnetwork_mode = "public"\n',
            encoding="utf-8",
        )
        (root / "environment/README.md").write_text(
            "使用 harbor_ags 锁定的 AGS 预置环境；需要 python3 和 pytest。\n", encoding="utf-8"
        )
        (root / "solution/solve.sh").write_text(
            "#!/bin/sh\nset -eu\ncd /home/user/workspace\n" + variant.script + "\n",
            encoding="utf-8",
        )
        (root / "solution/solve.sh").chmod(0o755)
        (root / "tests/test_outputs.py").write_text(verifier.test_outputs_py, encoding="utf-8")
        shutil.copyfile(Path(__file__).with_name("grading.py"), root / "tests/grader.py")
        (root / "tests/test.sh").write_text(
            "#!/bin/sh\nset -eu\n\npython3 /tests/grader.py\n", encoding="utf-8"
        )
        (root / "tests/test.sh").chmod(0o755)
        (root / "tests/rubric.json").write_text(
            json.dumps(
                {
                    "schema_version": "traceforge-task-rubric/v1",
                    "aggregation": "weighted_sum",
                    "criteria": [
                        {
                            "id": "task",
                            "weight": 1.0,
                            "description": "用户验收义务全部通过",
                            "verifier": "grader.py",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        entries.append(
            write_json_artifact(
                artifact.staging_path,
                "task/tests/control/verification_spec.json",
                verifier.to_dict(),
            )
        )
        layout = validate_bundle_layout(root)
        entries.append(
            write_json_artifact(
                artifact.staging_path,
                "compile_manifest.json",
                {
                    "schema_version": "traceforge.compiled-bundle.v1",
                    "bundle_id": digest,
                    "task_path": "task",
                    "layout": layout,
                    "verifier_status": "UNVALIDATED",
                    "variant": variant.name,
                    "workspace_sha256": tree,
                },
            )
        )
        hashes = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                hashes[path.relative_to(artifact.staging_path).as_posix()] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        write_json_artifact(
            artifact.staging_path,
            "artifact_manifest.json",
            {
                "schema_version": "traceforge.bundle-artifacts.v1",
                "bundle_id": digest,
                "files": artifact_entry_dicts(entries),
                "bundle_file_sha256": hashes,
            },
        )
        return artifact.publish()
    except BaseException:
        artifact.abort()
        raise


__all__ = ["compile_bundle"]
