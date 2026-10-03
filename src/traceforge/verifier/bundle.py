"""把重建环境和隐藏验证器编译为 Harbor Task Bundle。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from traceforge.harbor_ags.adapter import validate_bundle_layout
from traceforge.harbor_task import (
    CONTAINER_VERSION,
    terminal_task_inputs,
    write_terminal_task,
)
from traceforge.reconstruction.python_runtime import RUNTIME_NAME
from traceforge.trajectory.artifacts import (
    ArtifactWorkspace,
    artifact_entry_dicts,
    write_json_artifact,
)

from .synthesis import VerifierCandidate, is_python_solution, validate_solution_scripts

_BUNDLE_COMPILER_VERSION = "traceforge.bundle-compiler.v11-skip-diagnostics"


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
    instruction, tree, env_metadata = terminal_task_inputs(
        task=task, workspace_root=workspace_root, env_root=env_root,
    )
    python_runtime = workspace_root.parent / RUNTIME_NAME
    hidden_source: Path | None = None
    if env_root is not None:
        env_root = Path(env_root).resolve()
        hidden = env_root / "hidden_control"
        if hidden.is_dir():
            hidden_source = hidden
        elif (env_root / "env_manifest.json").is_file():
            raise ValueError("env_manifest 存在但 hidden_control 缺失")
    variants = verifier.oracle_solutions if mutation_index is None else verifier.mutation_solutions
    index = solution_index if mutation_index is None else mutation_index
    if index < 0 or index >= len(variants):
        raise ValueError("参考解索引超出范围")
    variant = variants[index]
    validate_solution_scripts(verifier.oracle_solutions, verifier.mutation_solutions)
    # 验收输入随隐藏 control 导出，独立 rollout 无需回读重建目录。
    task_acceptance = {
        "task_id": task.get("task_id"),
        "acceptance_obligations": task.get("acceptance_obligations", []),
        "environment_bindings": task.get("environment_bindings", []),
        "response_contract": task.get("response_contract"),
        "file_semantic_checks": verifier.file_semantic_checks,
    }
    digest = hashlib.sha256(
        json.dumps(
            {
                "compiler_version": _BUNDLE_COMPILER_VERSION,
                "container_version": CONTAINER_VERSION,
                "instruction": instruction,
                "task_acceptance": task_acceptance,
                "tree": tree,
                "verifier": verifier.to_dict(),
                "variant_set": "oracle" if mutation_index is None else "mutation",
                "variant_index": index,
                "variant": variant.name,
                "variant_script": variant.script,
                "variant_justification": variant.justification,
                "entrypoint_contract": {
                    "workspace_mount": "/home/user/workspace",
                    "solution_mount": "/solution",
                    "shell_entrypoint": "solve.sh",
                    "python_entrypoint": "solve.py",
                },
                "environment": env_metadata,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    artifact = ArtifactWorkspace(output_root, digest)
    entries = []
    try:
        root = artifact.staging_path / "task"
        write_terminal_task(
            root, task=task, workspace_root=workspace_root, instruction=instruction,
            name=f"traceforge/reconstructed-{digest[:16]}", separate_verifier=True,
        )
        for name in ("solution", "tests/control"):
            (root / name).mkdir(parents=True, exist_ok=True)
        if python_runtime.is_dir():
            shutil.copytree(python_runtime, root / "tests" / RUNTIME_NAME)
            shutil.copyfile(root / "environment/setup.sh", root / "tests/setup.sh")
        if hidden_source is not None:
            # The control copy is verifier-only.  It is never placed under the
            # public workspace or instruction, so solver agents cannot read it.
            shutil.copytree(hidden_source, root / "tests/control/reconstruction", symlinks=False)
        (root / "tests/control/reconstruction_environment.json").write_text(
            json.dumps(env_metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # Bind the hidden control inputs used by grading to this immutable
        # task snapshot. The grader uses this file to publish an
        # evaluation_contract instead of silently grading an unbound bundle.
        (root / "tests/control/input-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "traceforge.control-input-manifest.v1",
                    "task_id": task.get("task_id"),
                    "source_task_hash": task.get("source_task_hash"),
                    "task_acceptance": task_acceptance,
                    "workspace_sha256": tree,
                    "environment_metadata": env_metadata,
                    "hidden_control_files": (
                        sorted(
                            path.relative_to(hidden_source).as_posix()
                            for path in hidden_source.rglob("*")
                            if path.is_file()
                        )
                        if hidden_source is not None
                        else []
                    ),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        if is_python_solution(variant.script):
            (root / "solution/solve.py").write_text(
                variant.script.rstrip() + "\n",
                encoding="utf-8",
            )
            (root / "solution/solve.py").chmod(0o755)
            solve_script = (
                "#!/bin/sh\nset -eu\ncd /home/user/workspace\nexport TRACEFORGE_WORKSPACE=/home/user/workspace\n"
                "exec python3 /solution/solve.py\n"
            )
        else:
            solve_script = "#!/bin/sh\nset -eu\ncd /home/user/workspace\nexport TRACEFORGE_WORKSPACE=/home/user/workspace\n" + variant.script + "\n"
        (root / "solution/solve.sh").write_text(solve_script, encoding="utf-8")
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
                    "compiler_version": _BUNDLE_COMPILER_VERSION,
                    "bundle_id": digest,
                    "task_path": "task",
                    "layout": layout,
                    "verifier_status": "UNVALIDATED",
                    "variant_set": "oracle" if mutation_index is None else "mutation",
                    "variant_index": index,
                    "variant": variant.name,
                    "entrypoint_contract": {
                        "workspace_mount": "/home/user/workspace",
                        "solution_mount": "/solution",
                        "shell_entrypoint": "solve.sh",
                        "python_entrypoint": "solve.py",
                    },
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
