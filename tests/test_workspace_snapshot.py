"""实际虚拟环境和业务文件检验同一个收集边界。"""
import json
import venv

import pytest

from traceforge.harbor_task import workspace_snapshot_hook
from traceforge.workspace_snapshot import collect_workspace


def test_collects_source_and_records_real_venv_and_cache_exclusions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('原样源码')\n")
    (source / "empty").mkdir()
    (source / "__pycache__").mkdir()
    (source / "__pycache__/main.pyc").write_bytes(b"cache")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(source / ".venv")
    assert (source / ".venv/bin/python").is_symlink()
    destination = tmp_path / "collected/workspace"
    receipt = collect_workspace(source, destination, initial_paths=["main.py", "empty"], output_paths=[])
    assert receipt["status"] == "COLLECTED"
    assert (destination / "main.py").read_bytes() == (source / "main.py").read_bytes()
    assert (destination / "empty").is_dir()
    assert not (destination / ".venv").exists()
    assert {row["path"] for row in receipt["excluded"]} == {".venv", "__pycache__"}
    assert json.loads((destination.parent / "workspace-collection.json").read_text()) == receipt


@pytest.mark.parametrize("protection", ["initial", "output"])
def test_declared_material_inside_runtime_directory_is_preserved(tmp_path, protection):
    source = tmp_path / "source"
    (source / ".venv").mkdir(parents=True)
    (source / ".venv/pyvenv.cfg").write_text("home = /usr/bin\n")
    (source / ".venv/task.txt").write_text("任务资料")
    relative = ".venv/task.txt"
    receipt = collect_workspace(
        source, tmp_path / "snapshot/workspace",
        initial_paths=[relative] if protection == "initial" else [],
        output_paths=[relative] if protection == "output" else [],
    )
    assert not receipt["excluded"]
    assert receipt["protected"] == [{"path": ".venv", "reason": "PYTHON_VENV"}]
    assert (tmp_path / "snapshot/workspace/.venv/task.txt").read_text() == "任务资料"


@pytest.mark.parametrize("path", ["link.py", ".venv/bin/python"])
def test_links_outside_excluded_runtime_fail_with_receipt(tmp_path, path):
    source = tmp_path / "source"
    source.mkdir()
    link = source / path
    link.parent.mkdir(parents=True, exist_ok=True)
    if path.startswith(".venv"):
        (source / ".venv/pyvenv.cfg").write_text("home = /usr/bin\n")
    link.symlink_to("/usr/bin/python3")
    destination = tmp_path / "snapshot/workspace"
    with pytest.raises(ValueError, match="链接"):
        collect_workspace(source, destination, initial_paths=[path], output_paths=[])
    receipt = json.loads((destination.parent / "workspace-collection.json").read_text())
    assert receipt["status"] == "ERROR"
    assert not destination.exists()


def test_directory_name_alone_cannot_hide_business_files(tmp_path):
    source = tmp_path / "source"
    (source / ".venv").mkdir(parents=True)
    (source / ".venv/task.py").write_text("business")
    (source / "__pycache__").mkdir()
    (source / "__pycache__/fixture.pyc").write_bytes(b"original fixture")
    destination = tmp_path / "snapshot/workspace"
    receipt = collect_workspace(source, destination,
                                initial_paths=["__pycache__/fixture.pyc"], output_paths=[])
    assert not receipt["excluded"]
    assert (destination / ".venv/task.py").read_text() == "business"
    assert (destination / "__pycache__/fixture.pyc").read_bytes() == b"original fixture"


def test_generated_hook_executes_same_collector_with_frozen_path_protection(tmp_path):
    import subprocess
    import tomllib

    task = tmp_path / "task"
    (task / "workspace").mkdir(parents=True)
    (task / "workspace/main.py").write_text("initial")
    (task / "tests/control").mkdir(parents=True)
    (task / "tests/control/input-manifest.json").write_text(json.dumps({
        "task_acceptance": {"environment_bindings": [{"output_paths": [".venv/report.txt"]}]},
    }))
    hook = tomllib.loads(workspace_snapshot_hook(task))["verifier"]["collect"][0]
    (task / "workspace/.venv").mkdir()
    (task / "workspace/.venv/pyvenv.cfg").write_text("home = /usr/bin\n")
    (task / "workspace/.venv/report.txt").write_text("requested report")
    destination = tmp_path / "artifact/workspace"
    command = hook["command"].replace("/home/user/workspace", str(task / "workspace"))
    command = command.replace("/logs/artifacts/traceforge/workspace", str(destination))
    subprocess.run(["sh", "-c", command], check=True)
    assert (destination / ".venv/report.txt").read_text() == "requested report"
    assert (destination / "main.py").read_text() == "initial"
