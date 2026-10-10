"""生成可交给原生 Harbor 的任务目录，不改变原任务和验收结论。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from traceforge.reconstruction.python_runtime import (
    RUNTIME_NAME,
    freeze_wheels,
    validate_python_runtime,
)
from traceforge.task_instruction import render_task_instruction
from traceforge.trajectory.artifacts import ArtifactWorkspace, write_json_artifact

CONTAINER_VERSION = "traceforge.harbor-container.v4"


def workspace_snapshot_hook(
    task: Path, *, environment_bindings: list[dict[str, Any]] | None = None,
) -> str:
    """在同一收集命令中绑定初态及已声明输出，排除范围不依赖模型猜测。"""
    initial = sorted(path.relative_to(task / "workspace").as_posix()
                     for path in (task / "workspace").rglob("*"))
    if environment_bindings is None:
        manifest = task / "tests/control/input-manifest.json"
        acceptance = (
            json.loads(manifest.read_text()).get("task_acceptance", {})
            if manifest.is_file() else {}
        )
        environment_bindings = acceptance.get("environment_bindings", [])
    outputs = sorted({str(Path(path).as_posix()).rstrip("/")
                      for binding in environment_bindings
                      for path in binding.get("output_paths", [])})
    code = Path(__file__).with_name("workspace_snapshot.py").read_text()
    code += (
        "\ncollect_workspace(Path('/home/user/workspace'), "
        "Path('/logs/artifacts/traceforge/workspace'), "
        f"initial_paths={initial!r}, output_paths={outputs!r})\n"
    )
    command = "python3 - <<'TRACEFORGE_SNAPSHOT'\n" + code + "TRACEFORGE_SNAPSHOT"
    return (
        "# TraceForge workspace snapshot hook v2\n[[verifier.collect]]\n"
        f"command = {json.dumps(command, ensure_ascii=False)}\n"
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


def terminal_task_inputs(
    *, task: dict[str, Any], workspace_root: Path, env_root: Path | None = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """验证并绑定公开任务、初态与环境来源；隐藏评分文件不属于交付前提。"""
    instruction = render_task_instruction(task)
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("缺少自足的任务指令")
    if (workspace_root / ".traceforge/source-excerpts.json").is_file():
        instruction += (
            "\n\n.traceforge/source-excerpts.json 记录原轨迹直接捕获的部分源码及捕获覆盖范围，"
            "不代表当前文件仍未恢复的区间。请结合当前工作区实际文件核查；"
            "不要仅由片段范围推断当前代码缺失；对于原始片段，不能将其视为完整源码，"
            "也不能以报告自述替代验证。"
        )
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
    python_runtime = workspace_root.parent / RUNTIME_NAME
    if python_runtime.is_dir():
        env_metadata["python_runtime"] = validate_python_runtime(
            python_runtime, workspace_root / "requirements.txt",
        )
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
            env_metadata.update({
                "schema_version": raw_manifest.get("schema_version"),
                "provenance": raw_manifest.get("provenance", {}),
                "withheld_change_count": raw_manifest.get("withheld_change_count", 0),
                "dependencies": raw_manifest.get("dependencies", []),
                "runtime_constraints": raw_manifest.get("runtime_constraints", []),
                "uncertainties": raw_manifest.get("uncertainties", []),
            })
            for key in ("provenance", "dependencies", "runtime_constraints", "uncertainties"):
                if key not in raw_manifest:
                    raise ValueError(f"env_manifest.json 缺少 {key}")
            if not isinstance(env_metadata["provenance"], dict):
                raise ValueError("env_manifest.provenance 必须是 object")
            required_lists = ("dependencies", "runtime_constraints", "uncertainties")
            if any(not isinstance(raw_manifest[key], list) for key in required_lists):
                raise ValueError(
                    "env_manifest dependencies/runtime_constraints/uncertainties 必须为数组")
    return instruction, tree, env_metadata


def _make_workspace_solver_writable(workspace: Path) -> None:
    """让 AGS 中以普通 user 运行的 oracle/Hermes 能修改公开 workspace。"""

    paths = [workspace, *workspace.rglob("*")]
    for path in sorted(paths, key=lambda item: (not item.is_dir(), item.as_posix())):
        try:
            mode = path.stat().st_mode
            path.chmod(mode | (0o777 if path.is_dir() else 0o666))
        except OSError as exc:
            raise ValueError(f"无法设置 workspace 写权限: {path}") from exc


def write_terminal_task(
    root: Path, *, task: dict[str, Any], workspace_root: Path, instruction: str,
    name: str, separate_verifier: bool,
) -> None:
    """共同生成终端任务初态、运行环境和采集入口，不写参考解或评分内容。"""
    shutil.copytree(workspace_root, root / "workspace")
    _make_workspace_solver_writable(root / "workspace")
    (root / "environment").mkdir()
    python_runtime = workspace_root.parent / RUNTIME_NAME
    if python_runtime.is_dir():
        shutil.copytree(python_runtime, root / "environment" / RUNTIME_NAME)
        (root / "environment/setup.sh").write_text(
            '#!/bin/sh\nset -eu\nsh "$(dirname "$0")/python_runtime/install.sh"\n',
        )
    (root / "instruction.md").write_text(instruction + "\n", encoding="utf-8")
    assessment = "" if separate_verifier else 'response_acceptance = "NOT_ASSESSED"\n'
    verifier_config = (
        'timeout_sec = 120.0\nenvironment_mode = "separate"\nuser = "user"\n'
        '[verifier.environment]\nnetwork_mode = "no-network"\n'
        if separate_verifier else ""
    )
    (root / "task.toml").write_text(
        'schema_version = "1.4"\n[task]\n'
        f'name = "{name}"\nversion = "1.0.0"\n'
        '[metadata]\nworkspace_snapshot = true\ndomain = "terminal"\n' + assessment +
        '[agent]\ntimeout_sec = 900.0\nuser = "user"\n'
        '[verifier]\n' + verifier_config +
        '[environment]\nos = "linux"\nnetwork_mode = "public"\n' +
        workspace_snapshot_hook(root, environment_bindings=task.get("environment_bindings", [])),
        encoding="utf-8",
    )
    (root / "environment/README.md").write_text(
        "原生 Harbor 通过 Dockerfile/Compose 构建与冻结依赖 ABI 匹配的 Python 环境；"
        "现有 AGS 后端继续使用其锁定模板。" + (
            "pytest 由 tests/vendor 离线提供；"
            "项目依赖从冻结的 python_runtime 离线安装，verifier 无网。\n"
            if separate_verifier else
            "项目依赖从冻结的 python_runtime 离线安装。\n"
            "此交付未包含自动评分器；使用 Harbor --disable-verification，验收保持 NOT_ASSESSED。\n"
        ), encoding="utf-8",
    )
    write_container_environment(root, separate_verifier=separate_verifier)


def export_terminal_task(
    *, task: dict[str, Any], workspace_root: Path, output_root: Path,
    env_root: Path | None = None,
) -> Path:
    """独立交付已恢复的任务与环境，不因尚无公平评分器而构造通过结论。"""
    instruction, tree, env_metadata = terminal_task_inputs(
        task=task, workspace_root=workspace_root, env_root=env_root,
    )
    digest = hashlib.sha256(json.dumps({
        "terminal_delivery_version": 1, "container_version": CONTAINER_VERSION,
        "task": task, "instruction": instruction, "tree": tree, "environment": env_metadata,
    }, sort_keys=True).encode()).hexdigest()
    if (output_root / digest).exists():
        raise ValueError("导出目录已存在，不能覆盖已发布任务")
    artifact = ArtifactWorkspace(output_root, digest)
    try:
        root = artifact.staging_path / "task"
        write_terminal_task(
            root, task=task, workspace_root=workspace_root, instruction=instruction,
            name=f"traceforge/reconstructed-{digest[:16]}", separate_verifier=False,
        )
        hashes = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()
        }
        write_json_artifact(artifact.staging_path, "delivery.json", {
            "schema_version": "traceforge.harbor-delivery.v1", "domain": "terminal",
            "task_path": "task", "task_id": task.get("task_id"),
            "source_task_hash": task.get("source_task_hash"),
            "response_acceptance": "NOT_ASSESSED",
            "rollout_args": ["--disable-verification"], "execution_status": "NOT_RUN",
            "workspace_sha256": tree,
            "task_file_sha256": hashes,
        })
        return artifact.publish() / "task"
    except BaseException:
        artifact.abort()
        raise


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


def _write_search_sources(
    public: Path, environment: dict[str, Any], pdf_paths: dict[str, str],
) -> None:
    """按来源拆分可读正文视图；规范证据文件保留原字节用于完整对账。"""
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
            digest = record.get("raw_sha256")
            if collection == "live_references" and isinstance(digest, str) and digest in pdf_paths:
                entry.update(pdf_path=pdf_paths[digest], pdf_sha256=digest)
            if record.get("ocr_pages"):
                entry["ocr_pages"] = [
                    {"page": item["page_number"], "record_key": f"ocr_pages.{number}",
                     "raw_path": f"source-assets/{item['ocr_raw_sha256']}.ocr.raw",
                     "image_path": f"source-assets/{item['image_sha256']}.png"}
                    for number, item in record["ocr_pages"].items()
                ]
            if isinstance(record.get(field), str):
                body = record.pop(field)
                if "\x00" in body:
                    entry["body_projection"] = {
                        "kind": "nul_to_control_picture_v1",
                        "nul_codepoint_offsets": [
                            offset for offset, character in enumerate(body) if character == "\x00"
                        ],
                        "original_body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    }
                    body = body.replace("\x00", "␀")
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


def export_search_task(
    environment: dict[str, Any], output_root: Path, *, evidence_root: Path | None = None,
) -> Path:
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
    from traceforge.reconstruction.search_tools import pdf_assets

    assets: set[Path] = set()
    for page in environment.get("live_references", []):
        assets.update(pdf_assets(page, evidence_root))
    pdf_paths = {asset.stem: f"source-assets/{asset.name}" for asset in assets
                 if asset.suffix == ".pdf"}
    tool_source = Path(__file__).parent / "reconstruction/search_tools.py"
    requires_web = environment.get("requires_live_web", True)
    dependency_lock = json.loads(
        (Path(__file__).parent / "reconstruction/search_vendor_lock.json").read_text()
    ) if requires_web else {}
    pdf_dependencies = [f"{item['name']}=={item['version']}"
                        for item in dependency_lock.get("wheels", [])]
    digest = hashlib.sha256(json.dumps({
        "search_delivery_version": 16, "pdf_dependencies": pdf_dependencies,
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
        _write_search_sources(public, environment, pdf_paths)
        if assets:
            (public / "source-assets").mkdir()
            for asset in sorted(assets):
                shutil.copyfile(asset, public / "source-assets" / asset.name)
        context = {
            "original_user_texts": (task.get("source_task") or {}).get("user_texts", []),
            "context_messages": environment.get("context_messages", []),
            "limitations": environment.get("limitations", []),
        }
        instruction += "\n\n任务所需历史上下文：\n" + json.dumps(context, ensure_ascii=False, indent=2)
        instruction += (
            "\n\n工作目录为 /home/user/workspace。先从 evidence-index.json 定位每个来源的文件路径；"
            "sources 中的 JSON 保存该来源除正文外的全部字段，同名 txt 是可读检索视图。"
            "若索引含 body_projection，只有记录位置的 NUL 显示为 ␀，行号不变；"
            "nul_codepoint_offsets 是从 0 开始的 Unicode 码点位置，可据此恢复 NUL。"
            "原有字面 ␀ 不变，原始字符和完整正文仍保存在 evidence.json。"
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
        if pdf_paths:
            instruction += (
                "\n索引的 pdf_path 指向已抓取的原 PDF，pdf_sha256 绑定原件字节；"
                "文本层和 OCR 是辅助视图，图表、公式和排版应核对原件。"
                "交付原文件不代表已经完成视觉核对。\n"
            )
        if any(page.get("ocr_pages") for page in environment.get("live_references", [])):
            instruction += (
                "\nPDF 的 ocr_pages 为按页保存的 OCR 原检测块，含 bbox 像素坐标和置信度。"
                "source-assets 中 source_pdf_sha256.pdf 是原文件，image_sha256.png 是原页图，"
                "ocr_raw_sha256.ocr.raw 是带版本、模型哈希的识别原始返回；字段值替换对应文件名前缀。"
                "原 text 文本层保留不变；OCR 检测顺序不是双栏阅读顺序，公式/上下标/表格可能误识别，"
                "未经核对不能声称精确恢复。solver 无需安装或运行 OCR。\n"
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
                'chmod -R a+rX "$script_dir"\n'
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
