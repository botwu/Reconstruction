"""生成可交给原生 Harbor 的任务目录，不改变原任务和验收结论。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from traceforge.trajectory.artifacts import ArtifactWorkspace, write_json_artifact

CONTAINER_VERSION = "traceforge.harbor-container.v3"
WORKSPACE_SNAPSHOT_HOOK = (
    "# TraceForge workspace snapshot hook\n"
    "[[verifier.collect]]\n"
    'command = "set -eu; rm -rf /logs/artifacts/traceforge/workspace; '
    "mkdir -p /logs/artifacts/traceforge/workspace; "
    'cp -a /home/user/workspace/. /logs/artifacts/traceforge/workspace/"\n'
    'service = "main"\nuser = "root"\ntimeout_sec = 120.0\n'
)
_BASE = """RUN apt-get update && apt-get install -y --no-install-recommends \\
    bash ca-certificates curl git ripgrep && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 user \\
    && mkdir -p /home/user/workspace /logs/artifacts /logs/verifier \\
    && chown user:user /home/user/workspace \\
    && chmod 777 /logs/artifacts /logs/verifier
WORKDIR /home/user/workspace
"""


def _python_version(task: Path) -> str:
    # 目标沙箱的 CPython wheel 绑定解释器 ABI，不能使用控制端的 Python 版本。
    versions = set()
    for wheel in (task / "environment/python_runtime/wheels").glob("*.whl"):
        match = re.search(r"-cp(3)(\d+)-cp\1\2-", wheel.name)
        if match:
            versions.add(f"{match[1]}.{match[2]}")
    if len(versions) > 1:
        raise ValueError("冻结依赖包含不兼容的 CPython ABI，无法生成统一容器")
    return next(iter(versions), "3.12")


def write_container_environment(task: Path, *, separate_verifier: bool) -> None:
    """Compose 的构建上下文为任务根目录，公开 workspace 无需复制两份。"""
    roles = ["environment", "tests"] if separate_verifier else ["environment"]
    base = f"FROM python:{_python_version(task)}-slim-bookworm\n" + _BASE
    for role in roles:
        folder = task / role
        folder.mkdir(parents=True, exist_ok=True)
        install_root = f"/opt/traceforge-{role}"
        dockerfile = base + f"COPY {role}/ {install_root}/\n"
        dockerfile += (
            f"RUN if [ -f {install_root}/setup.sh ]; then sh {install_root}/setup.sh; fi\n"
        )
        if role == "environment":
            dockerfile += "COPY --chown=user:user workspace/ /home/user/workspace/\n"
        (folder / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        # 已冻结的 AGS Python wheel 面向 Linux x86_64；本地 ARM 主机也使用相同目标。
        (folder / "docker-compose.yaml").write_text(
            "services:\n  main:\n    platform: linux/amd64\n"
            f"    build:\n      context: ..\n      dockerfile: {role}/Dockerfile\n"
            "    command: [sleep, infinity]\n", encoding="utf-8",
        )


def export_search_task(environment: dict[str, Any], output_root: Path) -> Path:
    """封装已补全的检索初态；未提供内容验证器时使用 Harbor 的跳过验证模式。"""
    task = environment.get("task") or {}
    instruction = task.get("task_instruction")
    if (environment.get("status") != "READY" or environment.get("errors")
            or environment.get("missing_inputs")
            or not isinstance(instruction, str) or not instruction.strip()):
        raise ValueError("只有任务和必要上下文完整的检索初态才能导出 Harbor")
    tool_source = Path(__file__).parent / "reconstruction/search_tools.py"
    requires_web = environment.get("requires_live_web", True)
    digest = hashlib.sha256(json.dumps({
        "search_delivery_version": 2,
        "container_version": CONTAINER_VERSION, "environment": environment,
        "search_tool_sha256": hashlib.sha256(tool_source.read_bytes()).hexdigest(),
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if (output_root / digest).exists():
        raise ValueError("Harbor 交付已存在，拒绝覆盖")
    artifact = ArtifactWorkspace(output_root, digest)
    try:
        root = artifact.staging_path / "task"
        public = root / "workspace"
        public.mkdir(parents=True)
        write_json_artifact(public, "evidence.json", {
            "captures": environment.get("captures", []),
            "live_references": environment.get("live_references", []),
        })
        context = {
            "original_user_texts": (task.get("source_task") or {}).get("user_texts", []),
            "context_note": environment.get("context_note"),
            "limitations": environment.get("limitations", []),
        }
        instruction += "\n\n任务所需历史上下文：\n" + json.dumps(context, ensure_ascii=False, indent=2)
        instruction += (
            "\n\n工作目录为 /home/user/workspace。evidence.json 保留原始资料及捕获来源；"
            "可使用 Python 读取 JSON，再按关键词检索 captures 中的 result_blocks 和 session_parse。"
            "文件路径和行号来自原始观察；历史版本与当前文件分开引用，未捕获不等于不存在。"
            "按原任务要求给出最终回答，Harbor 会保存执行轨迹。\n"
        )
        if requires_web:
            instruction += (
                '\n公开网页工具：traceforge-search search "查询内容"；'
                'traceforge-search open "https://来源地址" --offset 0 --limit 8000。'
                "返回保留抓取时间与原始内容哈希；历史片段不能当作已读取当前全文。\n"
            )
        else:
            instruction += "\n本任务使用已捕获的本地语料，不需要公网搜索；缺少的证据应明确说明。\n"
        (root / "instruction.md").write_text(instruction, encoding="utf-8")
        (root / "task.toml").write_text(
            'schema_version = "1.4"\nartifacts = ["/home/user/workspace"]\n'
            f'[task]\nname = "traceforge/search-{digest[:16]}"\n'
            '[metadata]\ndomain = "search"\nresponse_acceptance = "NOT_ASSESSED"\n'
            '[agent]\ntimeout_sec = 1800.0\nuser = "user"\n'
            '[environment]\nos = "linux"\nnetwork_mode = "public"\n'
            'workdir = "/home/user/workspace"\ncpus = 2\nmemory_mb = 4096\n' + (
            '[environment.env]\nSERPER_API_KEY = "${SERPER_API_KEY:-}"\n'
            'JINA_API_KEY = "${JINA_API_KEY:-}"\n'
            'TRACEFORGE_FETCH_PROVIDER = "${TRACEFORGE_FETCH_PROVIDER:-jina}"\n' if requires_web else ''),
            encoding="utf-8",
        )
        write_container_environment(root, separate_verifier=False)
        if requires_web:
            shutil.copyfile(tool_source, root / "environment/search_tools.py")
            (root / "environment/traceforge-search").write_text(
                '#!/bin/sh\nexec python3 /opt/traceforge-environment/search_tools.py "$@"\n',
                encoding="utf-8",
            )
            (root / "environment/setup.sh").write_text(
                '#!/bin/sh\nset -eu\nchmod 755 /opt/traceforge-environment/traceforge-search\n'
                'ln -s /opt/traceforge-environment/traceforge-search /usr/local/bin/traceforge-search\n',
                encoding="utf-8",
            )
        (root / "tests").mkdir()
        (root / "tests/README.md").write_text(
            "此任务的回答内容由人工核查，尚无自动 verifier。\n"
            "使用 Harbor --disable-verification 执行 rollout；不生成虚假的通过奖励。\n",
            encoding="utf-8",
        )
        hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(root.rglob("*")) if p.is_file()}
        write_json_artifact(artifact.staging_path, "delivery.json", {
            "schema_version": "traceforge.harbor-delivery.v1", "domain": "search",
            "task_path": "task", "source_sha256": environment.get("source_sha256"),
            "task_id": task.get("task_id"), "response_acceptance": "NOT_ASSESSED",
            "rollout_args": ["--disable-verification"], "execution_status": "NOT_RUN",
            "task_file_sha256": hashes,
        })
        return artifact.publish() / "task"
    except BaseException:
        artifact.abort()
        raise
