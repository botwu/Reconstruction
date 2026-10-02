"""生成可交给原生 Harbor 的任务目录，不改变原任务和验收结论。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from traceforge.reconstruction.python_runtime import freeze_wheels, validate_python_runtime
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


_SEARCH_INSTALL_SCRIPT = '''#!/bin/sh
set -eu
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -m pip install --disable-pip-version-check --no-index --no-deps --require-hashes \\
    --no-compile --target "$bundle/../python" \\
    --find-links "$bundle/wheels" -r "$bundle/requirements.lock"
'''


def _copy_search_runtime(environment: Path, lock: dict[str, Any]) -> None:
    """复用现有 wheel 哈希冻结，离线安装到检索工具私有目录。"""
    runtime = environment / "python_runtime"
    wheels = runtime / "wheels"
    wheels.mkdir(parents=True)
    vendor = Path(__file__).parent / "reconstruction/search_vendor"
    for item in lock["wheels"]:
        name = item["filename"]
        if Path(name).name != name or not name.endswith("-py3-none-any.whl"):
            raise ValueError("search 依赖必须为跨平台纯 Python wheel")
        raw = (vendor / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError(f"search 依赖 wheel 哈希不匹配：{name}")
        (wheels / name).write_bytes(raw)
    requirements = environment / "requirements.txt"
    freeze_wheels(runtime, requirements, install_script=_SEARCH_INSTALL_SCRIPT)
    validate_python_runtime(runtime, requirements, install_script=_SEARCH_INSTALL_SCRIPT)
    (environment / "dependency-sources.json").write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_search_sources(public: Path, environment: dict[str, Any]) -> None:
    """按来源分开原文与其余字段，保留规范证据文件用于完整对账。"""
    (public / "sources").mkdir()
    sources = []
    for collection, prefix, field in (
        ("captures", "captured", "result_text"), ("live_references", "live", "text"),
    ):
        for index, original in enumerate(environment.get(collection, [])):
            record = dict(original)
            stem = f"sources/{prefix}-{index:04d}"
            entry = {
                "collection": collection, "index": index, "record_path": f"{stem}.json",
                "body_field": None, "body_path": None, "body_sha256": None,
                **{key: original[key] for key in
                   ("evidence_ref_id", "url", "query", "title", "content_kind") if key in original},
            }
            if isinstance(record.get(field), str):
                body = record.pop(field)
                raw = body.encode("utf-8")
                (public / f"{stem}.txt").write_bytes(raw)
                entry.update(
                    body_field=field, body_path=f"{stem}.txt",
                    body_sha256=hashlib.sha256(raw).hexdigest(),
                    body_bytes=len(raw), body_lines=len(body.splitlines()),
                )
            record_path = public / f"{stem}.json"
            record_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            entry["record_sha256"] = hashlib.sha256(record_path.read_bytes()).hexdigest()
            sources.append(entry)
    (public / "evidence-index.json").write_text(
        json.dumps({"sources": sources}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def export_search_task(environment: dict[str, Any], output_root: Path) -> Path:
    """封装已补全的检索初态；未提供内容验证器时使用 Harbor 的跳过验证模式。"""
    from traceforge.reconstruction.search_handoff import SEARCH_ENVIRONMENT_SCHEMA

    if environment.get("schema_version") != SEARCH_ENVIRONMENT_SCHEMA:
        raise ValueError("旧检索交付缺少来源边界与材料覆盖记录，请重新补全")
    task = environment.get("task") or {}
    instruction = task.get("task_instruction")
    if (environment.get("status") != "READY" or environment.get("errors")
            or environment.get("missing_inputs")
            or not isinstance(instruction, str) or not instruction.strip()):
        raise ValueError("只有任务和必要上下文完整的检索初态才能导出 Harbor")
    tool_source = Path(__file__).parent / "reconstruction/search_tools.py"
    requires_web = environment.get("requires_live_web", True)
    dependency_lock = json.loads(
        (Path(__file__).parent / "reconstruction/search_vendor_lock.json").read_text()
    ) if requires_web else {}
    pdf_dependencies = [f"{item['name']}=={item['version']}"
                        for item in dependency_lock.get("wheels", [])]
    digest = hashlib.sha256(json.dumps({
        "search_delivery_version": 12, "pdf_dependencies": pdf_dependencies,
        "dependency_sources": dependency_lock,
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
        _write_search_sources(public, environment)
        context = {
            "original_user_texts": (task.get("source_task") or {}).get("user_texts", []),
            "context_messages": environment.get("context_messages", []),
            "limitations": environment.get("limitations", []),
        }
        instruction += "\n\n任务所需历史上下文：\n" + json.dumps(context, ensure_ascii=False, indent=2)
        instruction += (
            "\n\n工作目录为 /home/user/workspace。先从 evidence-index.json 定位每个来源的文件路径；"
            "sources 中的 JSON 保存该来源除正文外的全部字段，同名 txt 保留未改写的正文。"
            "使用 read_file/search_files 定位全文关键词并分页核对相关内容；"
            "前缀截取不能等同于已读摘要或相关章节。"
            "evidence.json 仍完整保留原始资料及捕获来源；也可使用 Python 读取 JSON："
            "captures 中的 result_blocks 和 session_parse 保存原始观察；"
            "live_references 中的 results/text 保存补全时取得的检索返回与源码或网页正文，"
            "按 url、title、source_ref 定位并核对所需内容。"
            "用户确实依赖的原会话历史回答或方案是任务输入，用于恢复已有文献、比较或执行对象；"
            "当前待解任务的答案或草稿，以及重建生成的 solver/rollout 答卷与复核意见不属于初态，"
            "不能与必要历史输入混淆。"
            "历史 AI 陈述和搜索题录或摘要应按各自实际范围使用，出版事实须对照同一文献实际取得的"
            "出版元数据、JSON-LD 作者关系或正文署名（live_references 的 metadata/jsonld/text）；"
            "不能把正文参考文献作者归给当前论文、仅凭单值 citation_author 断言署名完整，"
            "或把访问成功当成未读内容已核实。"
            "文件路径和行号来自原始观察；公开上游版本与原仓库分开引用，未捕获不等于不存在。"
            "按原任务要求给出最终回答，Harbor 会保存执行轨迹。\n"
        )
        if requires_web:
            instruction += (
                '\n公开网页工具：traceforge-search search "查询内容"；'
                'traceforge-search open "https://来源地址" --offset 0 --limit 8000。'
                "返回保留抓取时间与原始内容哈希；历史片段不能当作已读取当前全文。\n"
            )
        else:
            instruction += "\n本任务使用已交付的原始语料及补充来源，不需要继续公网搜索；缺少的证据应明确说明。\n"
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
            (root / "environment/requirements.txt").write_text("\n".join(pdf_dependencies) + "\n")
            _copy_search_runtime(root / "environment", dependency_lock)
            (root / "environment/traceforge-search").write_text(
                '#!/bin/sh\nset -eu\n'
                'script_dir=$(dirname -- "$(readlink -f -- "$0")")\n'
                'export PYTHONPATH="$script_dir/python${PYTHONPATH:+:$PYTHONPATH}"\n'
                'exec python3 "$script_dir/search_tools.py" "$@"\n',
                encoding="utf-8",
            )
            (root / "environment/setup.sh").write_text(
                '#!/bin/sh\nset -eu\n'
                'script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                'sh "$script_dir/python_runtime/install.sh"\n'
                'chmod 755 "$script_dir/traceforge-search"\n'
                'ln -sf "$script_dir/traceforge-search" /usr/local/bin/traceforge-search\n',
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
