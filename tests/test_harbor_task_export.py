"""任务导出保留真实初态，供原生 Harbor 独立加载。"""

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from traceforge.harbor_task import export_search_task, write_container_environment
from traceforge.trajectory.json_codec import canonical_json_bytes


def search_environment():
    return {"schema_version": "traceforge.search-environment.v5",
        "status": "READY", "errors": [], "missing_inputs": [],
        "task": {"task_id": "search-1", "task_instruction": "比较两种处理方式。",
                 "source_task": {"user_texts": ["保留出处。"]}},
        "source_sha256": "fixture-source", "context_messages": [{"message_index": 0, "content": "原始完整方案。"}],
        "limitations": ["原始页面是历史快照。"],
        "captures": [{"evidence_ref_id": "captured:0", "result_text": "完整正文\r\n尾行\n"}],
        "live_references": [{"url": "https://example.org", "text": "网页正文"}],
    }


def test_search_delivery_preserves_evidence_without_inventing_a_verifier(tmp_path):
    source = search_environment()
    task = export_search_task(source, tmp_path)
    config = tomllib.loads((task / "task.toml").read_text())
    assert config["metadata"]["domain"] == "search"
    assert config["metadata"]["response_acceptance"] == "NOT_ASSESSED"
    assert config["artifacts"] == ["/home/user/workspace"]
    assert config["environment"]["env"]["SERPER_API_KEY"] == "${SERPER_API_KEY:-}"
    evidence = json.loads((task / "workspace/evidence.json").read_text())
    assert evidence["captures"] == source["captures"]
    assert evidence["live_references"] == source["live_references"]
    instruction = (task / "instruction.md").read_text()
    assert source["task"]["task_instruction"] in instruction
    assert source["context_messages"][0]["content"] in instruction
    assert "保留出处。" in instruction
    assert (task / "environment/Dockerfile").is_file()
    assert (task / "environment/docker-compose.yaml").is_file()
    assert (task / "environment/search_tools.py").is_file()
    requirements = (task / "environment/requirements.txt").read_text()
    assert "pypdf==" in requirements and "fonttools==" in requirements
    assert 'sh "$script_dir/python_runtime/install.sh"' in (task / "environment/setup.sh").read_text()
    installer = (task / "environment/python_runtime/install.sh").read_text()
    assert "--no-index --no-deps --require-hashes" in installer
    assert len(json.loads((task / "environment/dependency-sources.json").read_text())["wheels"]) == 2
    assert "live_references" in (task / "instruction.md").read_text()
    assert not (task / "solution").exists()
    assert not (task / "tests/test.sh").exists()
    assert not (task / "workspace/raw-session.json").exists()
    receipt = json.loads((task.parent / "delivery.json").read_text())
    assert receipt["rollout_args"] == ["--disable-verification"]
    assert receipt["execution_status"] == "NOT_RUN"


@pytest.mark.parametrize("change", [
    {"status": "BLOCKED"}, {"missing_inputs": ["缺少用户文件"]},
    {"errors": ["原文引用不存在"]}, {"task": {"task_instruction": ""}},
])
def test_incomplete_search_context_is_not_published(tmp_path, change):
    with pytest.raises(ValueError):
        export_search_task({**search_environment(), **change}, tmp_path)
    assert not list(tmp_path.iterdir())


def test_container_build_keeps_verifier_and_solution_out_of_agent_image(tmp_path):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "tests").mkdir()
    write_container_environment(tmp_path, separate_verifier=True)
    dockerfile = (tmp_path / "environment/Dockerfile").read_text()
    assert "workspace/ /home/user/workspace/" in dockerfile
    assert "COPY tests/" not in dockerfile
    assert "COPY solution/" not in dockerfile
    assert "COPY . " not in dockerfile
    assert "COPY tests/" in (tmp_path / "tests/Dockerfile").read_text()


def test_export_rejects_overwriting_a_published_task(tmp_path):
    source = search_environment()
    export_search_task(source, tmp_path)
    with pytest.raises(ValueError, match="已存在"):
        export_search_task(source, tmp_path)


def test_local_code_search_remains_search_and_does_not_require_web_credentials(tmp_path):
    source = {**search_environment(), "requires_live_web": False, "live_references": []}
    task = export_search_task(source, tmp_path)
    config = tomllib.loads((task / "task.toml").read_text())
    assert config["metadata"]["domain"] == "search"
    assert "env" not in config["environment"]
    assert config["artifacts"] == ["/home/user/workspace"]
    assert not (task / "environment/search_tools.py").exists()
    assert "不需要继续公网搜索" in (task / "instruction.md").read_text()
    assert json.loads((task / "workspace/evidence.json").read_text())["captures"] == source["captures"]


def test_container_python_matches_frozen_binary_wheels(tmp_path):
    wheels = tmp_path / "environment/python_runtime/wheels"
    wheels.mkdir(parents=True)
    (wheels / "orjson-3.12.0-cp311-cp311-manylinux_2_17_x86_64.whl").touch()
    (wheels / "aiofiles-25.1.0-py3-none-any.whl").touch()
    write_container_environment(tmp_path, separate_verifier=True)
    for role in ("environment", "tests"):
        assert (tmp_path / role / "Dockerfile").read_text().startswith(
            "FROM python:3.11-slim-bookworm\n")
    (wheels / "uvloop-0.22.1-cp312-cp312-manylinux_2_17_x86_64.whl").touch()
    with pytest.raises(ValueError, match="ABI"):
        write_container_environment(tmp_path, separate_verifier=True)



@pytest.mark.parametrize("install_exit", [0, 7])
def test_search_setup_uses_its_uploaded_directory(tmp_path, install_exit):
    task = export_search_task(search_environment(), tmp_path / "export")
    relocated = tmp_path / "ags uploaded environment"
    (task / "environment").rename(relocated)
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "setup-arguments.jsonl"
    recorder = (
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "with open(os.environ['SETUP_TEST_LOG'], 'a') as out:\n"
        "    out.write(json.dumps(sys.argv) + '\\n')\n"
        "raise SystemExit(int(os.environ['SETUP_TEST_EXIT']) "
        "if pathlib.Path(sys.argv[0]).name == 'python3' else 0)\n"
    )
    # 使用固定解释器，避免录制 python3 调用时递归进入自身。
    recorder = recorder.replace("#!/usr/bin/env python3", "#!" + sys.executable)
    for name in ("python3", "chmod", "ln"):
        command = commands / name
        command.write_text(recorder)
        command.chmod(0o755)
    result = subprocess.run(
        ["sh", str(relocated / "setup.sh")], cwd=tmp_path,
        env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
             "SETUP_TEST_LOG": str(log), "SETUP_TEST_EXIT": str(install_exit)},
        capture_output=True, text=True,
    )
    assert result.returncode == install_exit, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0][-4:] == ["--find-links", str(relocated / "python_runtime/wheels"),
                              "-r", str(relocated / "python_runtime/requirements.lock")]
    assert Path(calls[0][calls[0].index("--target") + 1]).resolve() == relocated / "python"
    assert "--no-index" in calls[0] and "--require-hashes" in calls[0]
    assert len(calls) == (3 if install_exit == 0 else 1)
    if install_exit == 0:
        assert calls[-1][-2:] == [
            str(relocated / "traceforge-search"), "/usr/local/bin/traceforge-search"]


def test_search_command_follows_its_installed_symlink(tmp_path):
    task = export_search_task(search_environment(), tmp_path / "export")
    command = task / "environment/traceforge-search"
    command.chmod(0o755)
    installed = tmp_path / "traceforge-search"
    installed.symlink_to(command)
    result = subprocess.run(
        [str(installed), "--help"], capture_output=True, text=True,
        env={**os.environ, "PATH": str(Path(sys.executable).parent)
             + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode == 0, result.stderr
    assert "search" in result.stdout and "open" in result.stdout


def test_search_command_imports_private_dependencies_after_relocation(tmp_path):
    task = export_search_task(search_environment(), tmp_path / "export")
    environment = task / "environment"
    private = environment / "python"
    private.mkdir()
    (private / "traceforge_dependency_probe.py").write_text("VALUE = 'private-dependency'\n")
    (environment / "search_tools.py").write_text(
        "from traceforge_dependency_probe import VALUE\nprint(VALUE)\n")
    relocated = tmp_path / "ags relocated environment"
    environment.rename(relocated)
    command = relocated / "traceforge-search"
    command.chmod(0o755)
    installed = tmp_path / "traceforge-search"
    installed.symlink_to(command)
    result = subprocess.run(
        [str(installed)], capture_output=True, text=True,
        env={**os.environ, "PATH": str(Path(sys.executable).parent)
             + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "private-dependency"


def test_search_export_rejects_corrupt_vendored_dependency(tmp_path, monkeypatch):
    read = Path.read_bytes

    def corrupt(path):
        if path.parent.name == "search_vendor":
            return b"corrupt"
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", corrupt)
    with pytest.raises(ValueError, match="wheel 哈希"):
        export_search_task(search_environment(), tmp_path)
    assert not any(tmp_path.iterdir())


def test_native_harbor_collects_web_cache_from_convention_directory(tmp_path):
    import asyncio
    import logging
    import shutil
    from pathlib import PurePosixPath
    from types import SimpleNamespace

    artifacts = pytest.importorskip("harbor.trial.artifact_handler")
    task = export_search_task(search_environment(), tmp_path / "export")
    source = tmp_path / "sandbox"
    cache = source / "logs/artifacts/search"
    cache.mkdir(parents=True)
    (cache / "calls.jsonl").write_text('{"tool":"web_open"}\n')
    (cache / "source.raw").write_bytes(b"captured-source")
    (source / "home/user/workspace").mkdir(parents=True)
    downloads = []

    class LocalTransfer:
        capabilities = SimpleNamespace(mounted=False)

        async def service_is_dir(self, path, **kwargs):
            return (source / path.lstrip("/")).is_dir()

        async def service_download_dir(self, source_dir, target_dir, **kwargs):
            downloads.append(source_dir)
            shutil.copytree(source / source_dir.lstrip("/"), target_dir, dirs_exist_ok=True)

    handler = artifacts.ArtifactHandler(
        artifacts=tomllib.loads((task / "task.toml").read_text())["artifacts"],
        logger=logging.getLogger(__name__),
    )
    target = tmp_path / "collected"
    asyncio.run(handler.download_artifacts(
        LocalTransfer(), target, source_artifacts_dir=PurePosixPath("/logs/artifacts"),
    ))
    assert downloads == ["/logs/artifacts", "/home/user/workspace"]
    assert (target / "logs/artifacts/search/source.raw").read_bytes() == b"captured-source"
    assert (target / "logs/artifacts/search/calls.jsonl").read_text() == '{"tool":"web_open"}\n'


def test_search_guidance_preserves_history_and_publication_evidence(tmp_path):
    source = search_environment()
    source["requires_live_web"] = False
    source["captures"][0]["result_text"] = "【AI】已有清单：作者甲的论文及既有分析。"
    source["live_references"][0].update(
        metadata={"citation_author": "单值作者"},
        jsonld={"author": [{"@id": "作者关系"}]},
    )
    original = json.loads(json.dumps(source))
    task = export_search_task(source, tmp_path)
    instruction = (task / "instruction.md").read_text()

    assert "用户确实依赖的原会话历史回答或方案是任务输入" in instruction
    assert "当前待解任务的答案或草稿" in instruction
    assert "重建生成的 solver/rollout 答卷与复核意见" in instruction
    assert "历史 AI 陈述和搜索题录或摘要" in instruction
    assert "出版元数据、JSON-LD 作者关系或正文署名" in instruction
    assert "参考文献作者" in instruction
    evidence = json.loads((task / "workspace/evidence.json").read_text())
    assert evidence == {key: original[key] for key in ("captures", "live_references")}
    assert source == original
    assert "必须取得全文" not in instruction
    assert "作者甲" not in instruction
    assert "单值作者" not in instruction


def test_search_source_files_round_trip_original_records(tmp_path):
    source = search_environment()
    source["requires_live_web"] = False
    source["captures"][0]["unknown_field"] = {"原值": ["保留", None]}
    source["live_references"] = [
        {
            "url": "https://example.org/paper",
            "title": "真实来源",
            "text": "第1页\r\n\t正文\n第2页\r\n",
            "metadata": {"citation_author": ["作者一", "作者二"]},
            "jsonld": {"@graph": [{"@id": "作者节点", "name": "作者一"}]},
            "unknown_field": {"keep": [1, None, "中文"]},
        },
        {"url": "https://example.org/empty", "text": "", "metadata": {}},
        {"query": "原检索", "results": [{"snippet": "原片段"}], "text": None},
    ]
    original = json.loads(json.dumps(source))
    task = export_search_task(source, tmp_path)
    public = task / "workspace"
    expected = {key: original[key] for key in ("captures", "live_references")}
    assert (public / "evidence.json").read_bytes() == canonical_json_bytes(expected) + b"\n"
    index = json.loads((public / "evidence-index.json").read_text())
    assert len(index["sources"]) == 4
    for entry in index["sources"]:
        record_file = public / entry["record_path"]
        record_raw = record_file.read_bytes()
        assert hashlib.sha256(record_raw).hexdigest() == entry["record_sha256"]
        restored = json.loads(record_raw)
        original_record = expected[entry["collection"]][entry["index"]]
        if entry["body_path"] is not None:
            raw = (public / entry["body_path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == entry["body_sha256"]
            assert raw == original_record[entry["body_field"]].encode("utf-8")
            restored[entry["body_field"]] = raw.decode("utf-8")
        assert restored == original_record
    empty = index["sources"][2]
    assert (public / empty["body_path"]).read_bytes() == b""
    assert index["sources"][3]["body_path"] is None
    assert source == original


def test_search_source_index_guides_native_file_reading_and_is_hashed(tmp_path):
    source = search_environment()
    source["requires_live_web"] = False
    task = export_search_task(source, tmp_path)
    instruction = (task / "instruction.md").read_text()
    assert "evidence-index.json" in instruction
    assert "read_file/search_files" in instruction
    assert "前缀截取不能等同于已读摘要或相关章节" in instruction
    index = json.loads((task / "workspace/evidence-index.json").read_text())
    assert index["sources"][1]["url"] == source["live_references"][0]["url"]
    receipt = json.loads((task.parent / "delivery.json").read_text())
    for path in (task / "workspace").rglob("*"):
        if path.is_file():
            assert receipt["task_file_sha256"][str(path.relative_to(task))] == (
                hashlib.sha256(path.read_bytes()).hexdigest()
            )


def test_search_json_metadata_is_visible_with_native_line_preview(tmp_path):
    source = search_environment()
    source["requires_live_web"] = False
    source["live_references"][0].update(
        abstract="原长字段" * 1000,
        metadata={"citation_authors": ["首位署名", "末位署名"]},
        jsonld={"author": [{"@id": "作者一"}, {"@id": "作者二"}]},
    )
    task = export_search_task(source, tmp_path)
    public = task / "workspace"
    index_text = (public / "evidence-index.json").read_text()
    record_text = (public / "sources/live-0000.json").read_text()
    # 真实 Hermes 对超长单行裁剪；作者关系不能藏在同一长行末端。
    visible = "\n".join(line[:2000] for line in record_text.splitlines())
    assert "首位署名" in visible
    assert "末位署名" in visible
    assert '"@id": "作者一"' in visible
    assert '"@id": "作者二"' in visible
    assert '\n  "sources": [\n' in index_text
