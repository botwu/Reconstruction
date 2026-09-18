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
    env_root: Path | None = None,
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
    env_metadata: dict[str, Any] = {}
    hidden_source: Path | None = None
    if env_root is not None:
        env_root = Path(env_root).resolve()
        if not env_root.is_dir():
            raise ValueError("env_root 不存在")
        manifest_path = env_root / "env_manifest.json"
        if manifest_path.is_file():
            try:
                raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("env_manifest.json 无法解析") from exc
            if not isinstance(raw_manifest, dict):
                raise ValueError("env_manifest.json 必须是 object")
            env_metadata = {
                "schema_version": raw_manifest.get("schema_version"),
                "provenance": raw_manifest.get("provenance", {}),
                "withheld_change_count": raw_manifest.get("withheld_change_count", 0),
                "dependencies": raw_manifest.get("dependencies", []),
                "runtime_constraints": raw_manifest.get("runtime_constraints", []),
                "uncertainties": raw_manifest.get("uncertainties", []),
            }
            for key in ("provenance", "dependencies", "runtime_constraints", "uncertainties"):
                if key not in raw_manifest:
                    raise ValueError(f"env_manifest.json 缺少 {key}")
            if not isinstance(env_metadata["provenance"], dict):
                raise ValueError("env_manifest.provenance 必须是 object")
            if any(not isinstance(raw_manifest[key], list) for key in ("dependencies", "runtime_constraints", "uncertainties")):
                raise ValueError("env_manifest dependencies/runtime_constraints/uncertainties 必须为数组")
        hidden = env_root / "hidden_control"
        if hidden.is_dir():
            hidden_source = hidden
        elif manifest_path.is_file():
            raise ValueError("env_manifest 存在但 hidden_control 缺失")
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
                "environment": env_metadata,
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
        if hidden_source is not None:
            # The control copy is verifier-only.  It is never placed under the
            # public workspace or instruction, so solver agents cannot read it.
            shutil.copytree(hidden_source, root / "tests/control/reconstruction", symlinks=False)
        (root / "tests/control/reconstruction_environment.json").write_text(
            json.dumps(env_metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
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
            "使用 harbor_ags 锁定的 AGS 预置环境；python3 来自模板，"
            "pytest 由 tests/vendor 离线提供，verifier 无网。\n",
            encoding="utf-8",
        )
        (root / "solution/solve.sh").write_text(
            "#!/bin/sh\nset -eu\ncd /home/user/workspace\n" + variant.script + "\n",
            encoding="utf-8",
        )
        (root / "solution/solve.sh").chmod(0o755)
        (root / "tests/test_outputs.py").write_text(verifier.test_outputs_py, encoding="utf-8")
        shutil.copyfile(Path(__file__).with_name("grading.py"), root / "tests/grader.py")
        vendor_src, lock_src = Path(__file__).with_name("vendor"), Path(__file__).with_name(
            "vendor_lock.json"
        )
        if not vendor_src.is_dir() or not lock_src.is_file():
            raise ValueError("缺少离线 pytest vendor，无法编译无网 verifier")
        shutil.copytree(vendor_src, root / "tests" / "vendor")
        shutil.copyfile(lock_src, root / "tests" / "vendor_lock.json")
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
                    "environment_metadata": env_metadata,
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
