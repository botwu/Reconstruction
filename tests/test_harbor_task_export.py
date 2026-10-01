"""任务导出保留真实初态，供原生 Harbor 独立加载。"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from traceforge.harbor_task import export_search_task, write_container_environment


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
    assert '-r "$script_dir/requirements.txt"' in (task / "environment/setup.sh").read_text()
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
    assert calls[0][-2:] == ["-r", str(relocated / "requirements.txt")]
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
