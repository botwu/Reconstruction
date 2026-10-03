#!/usr/bin/env python3
"""冻结的 AGS Hermes smoke harness。

该文件由旧 runner 与 Harbor ``LosslessHermesAgent`` 共用。它只依赖预置
Hermes 源码里的 ``run_agent.AIAgent``，模型密钥只从进程环境读取。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import time
import traceback
from pathlib import Path
from typing import Any

TOKEN_COUNT_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "last_prompt_tokens",
}


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def _safe_meta(result: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "final_response",
        "last_reasoning",
        "completed",
        "failed",
        "partial",
        "interrupted",
        "turn_exit_reason",
        "api_calls",
        "model",
        "provider",
        "base_url",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "last_prompt_tokens",
        "estimated_cost_usd",
        "session_id",
    )
    meta = {name: result.get(name) for name in fields}
    for key in list(meta):
        lower = key.lower()
        if "api_key" in lower or "secret" in lower:
            meta.pop(key, None)
        elif "key" in lower and key not in TOKEN_COUNT_FIELDS:
            meta.pop(key, None)
    return meta


def _file_digest(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": path.as_posix(),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _workspace_manifest(workspace: Path) -> dict[str, Any]:
    files = []
    for path in sorted(workspace.rglob("*")):
        if path.is_file() and not path.is_symlink():
            item = _file_digest(path)
            item["path"] = path.relative_to(workspace).as_posix()
            item["mode"] = stat.S_IMODE(path.stat().st_mode)
            files.append(item)
    canonical = json.dumps(
        files, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "root": workspace.as_posix(),
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(int(item["size"]) for item in files),
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _component(value: str) -> dict[str, Any]:
    raw = value.encode("utf-8")
    return {
        "content": value,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "characters": len(value),
    }


def _load_input_components(path: Path | None, instruction: str) -> dict[str, Any]:
    if path is None or not path.is_file():
        rendered = _component(instruction)
        return {
            "schema_version": "traceforge-rendered-agent-input/v1",
            "task_instruction": rendered,
            "runtime_appendix": None,
            "rendered_user_prompt": rendered,
            "composition": "task_instruction",
            "hashes": {
                "task_instruction_sha256": rendered["sha256"],
                "runtime_appendix_sha256": None,
                "rendered_user_prompt_sha256": rendered["sha256"],
            },
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("HERMES_INPUT_COMPONENTS_FILE 必须包含 JSON 对象")
    rendered = value.get("rendered_user_prompt")
    if not isinstance(rendered, dict) or rendered.get("content") != instruction:
        raise RuntimeError("input components 与最终 instruction 内容不一致")
    observed = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    if rendered.get("sha256") != observed:
        raise RuntimeError("input components 与最终 instruction 哈希不一致")
    return value


def main() -> int:
    out_dir = Path(os.environ.get("HERMES_HARNESS_OUT", "/logs/agent"))
    workspace = Path(os.environ.get("HERMES_WORKSPACE", "/home/user/workspace"))
    instruction_file = Path(_required("HERMES_INSTRUCTION_FILE"))
    input_components_raw = os.environ.get("HERMES_INPUT_COMPONENTS_FILE", "").strip()
    input_components_file = Path(input_components_raw) if input_components_raw else None
    model = _required("HERMES_LLM_MODEL")
    provider = os.environ.get("HERMES_LLM_PROVIDER", "anthropic").strip()
    base_url = _required("HERMES_LLM_BASE_URL")
    api_key = _required("HERMES_LLM_API_KEY")
    task_id = os.environ.get("HERMES_TASK_ID", "harbor-smoke")
    max_iterations = int(os.environ.get("HERMES_MAX_ITER", "30"))
    toolsets = [
        item.strip()
        for item in os.environ.get("HERMES_TOOLSETS", "file,terminal").split(",")
        if item.strip()
    ]
    system_message = os.environ.get("HERMES_SYSTEM_MESSAGE", "").strip() or None
    reasoning_effort = os.environ.get("HERMES_REASONING_EFFORT", "").strip()

    out_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    artifact_dir = Path("/logs/artifacts/traceforge")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    input_path = workspace / "input.json"
    if input_path.is_file():
        shutil.copyfile(input_path, artifact_dir / "input.json")

    instruction = instruction_file.read_text(encoding="utf-8")
    input_components = _load_input_components(input_components_file, instruction)
    workspace_initial = _workspace_manifest(workspace)
    (out_dir / "workspace-initial-manifest.json").write_text(
        json.dumps(workspace_initial, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    task_input = {
        "schema_version": "traceforge-agent-visible-input/v1",
        "instruction": input_components,
        "workspace": workspace_initial,
        "runtime": {
            "workspace_root": workspace.as_posix(),
            "artifact_root": artifact_dir.as_posix(),
            "provider": provider,
            "model": model,
            "toolsets": toolsets,
            "task_id": task_id,
        },
    }
    task_input["sha256"] = hashlib.sha256(
        json.dumps(
            task_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    (out_dir / "task-input.json").write_text(
        json.dumps(task_input, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chdir(workspace)
    started = time.time()

    try:
        from run_agent import AIAgent

        kwargs: dict[str, Any] = {
            "base_url": base_url,
            "api_key": api_key,
            "provider": provider,
            "model": model,
            "enabled_toolsets": toolsets,
            "max_iterations": max_iterations,
            "quiet_mode": True,
            "save_trajectories": False,
            "skip_context_files": True,
            "skip_memory": True,
        }
        if reasoning_effort:
            kwargs["reasoning_config"] = {"effort": reasoning_effort}

        agent = AIAgent(**kwargs)
        result = agent.run_conversation(
            instruction,
            system_message=system_message,
            task_id=task_id,
        )
    except Exception as exc:
        error = {
            "status": "RUNNER_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-8000:],
            "elapsed_s": round(time.time() - started, 3),
        }
        (out_dir / "runner_error.json").write_text(
            json.dumps(error, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr, flush=True)
        return 2

    messages = result.get("messages") or []
    meta = _safe_meta(result)
    meta.update(
        {
            "task_id": task_id,
            "backend": "harbor_ags_hermes",
            "provider_requested": provider,
            "model_requested": model,
            "toolsets": toolsets,
            "n_messages": len(messages),
            "elapsed_s": round(time.time() - started, 3),
            "harness_version": "1",
        }
    )
    native = {"meta": meta, "messages": messages}
    (out_dir / "hermes-result.json").write_text(
        json.dumps(native, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out_dir / "hermes-session.jsonl").write_text(
        json.dumps(native, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / "workspace-manifest.json").write_text(
        json.dumps(_workspace_manifest(workspace), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "status": "OK" if messages else "EMPTY_TRAJECTORY",
                "messages": len(messages),
                "api_calls": meta.get("api_calls"),
                "completed": meta.get("completed"),
                "elapsed_s": meta["elapsed_s"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if messages else 2


if __name__ == "__main__":
    raise SystemExit(main())
