"""任务导出保留真实初态，供原生 Harbor 独立加载。"""

import json
import tomllib

import pytest

from traceforge.harbor_task import export_search_task, write_container_environment


def search_environment():
    return {
        "status": "READY", "errors": [], "missing_inputs": [],
        "task": {"task_id": "search-1", "task_instruction": "比较两种处理方式。",
                 "source_task": {"user_texts": ["保留出处。"]}},
        "source_sha256": "fixture-source", "context_note": "原始对象的必要说明。",
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
    assert source["context_note"] in instruction
    assert "保留出处。" in instruction
    assert (task / "environment/Dockerfile").is_file()
    assert (task / "environment/docker-compose.yaml").is_file()
    assert (task / "environment/search_tools.py").is_file()
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
    assert "本任务使用已捕获的本地语料" in (task / "instruction.md").read_text()
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
