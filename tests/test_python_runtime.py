"""依赖锁定既约束原声明，也约束离线 wheel 和安装入口。"""

import zipfile

import pytest

from traceforge.reconstruction.python_runtime import freeze_wheels, validate_python_runtime
from traceforge.reconstruction.researcher import reuse_python_runtime


def frozen_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requirements = workspace / "requirements.txt"
    requirements.write_text("example>=1\n")
    root = tmp_path / "python_runtime"
    (root / "wheels").mkdir(parents=True)
    with zipfile.ZipFile(root / "wheels/example-1-py3-none-any.whl", "w") as archive:
        archive.writestr("example-1.dist-info/METADATA", "Name: example\nVersion: 1\n")
        archive.writestr("example/_vendor/nested-2.dist-info/METADATA",
                         "Name: nested\nVersion: 2\n")
    freeze_wheels(root, requirements)
    return root, requirements


def test_frozen_requirements_bind_transitive_wheels_and_installer(tmp_path):
    root, requirements = frozen_runtime(tmp_path)
    manifest = validate_python_runtime(root, requirements)
    assert len(manifest["files"]) == 4
    assert (root / "requirements.lock").read_text().startswith("example==1 --hash=sha256:")


@pytest.mark.parametrize("changed", ["requirements", "wheel", "installer"])
def test_changed_dependency_inputs_cannot_reuse_the_runtime(tmp_path, changed):
    root, requirements = frozen_runtime(tmp_path)
    path = {"requirements": requirements, "wheel": next((root / "wheels").iterdir()),
            "installer": root / "install.sh"}[changed]
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="PYTHON_RUNTIME_"):
        validate_python_runtime(root, requirements)


def test_candidate_reuses_only_matching_intact_dependency_bundle(tmp_path):
    root, requirements = frozen_runtime(tmp_path)
    target = tmp_path / "next_runtime"
    reuse_python_runtime(root, requirements.parent, target)
    assert validate_python_runtime(target, requirements) == validate_python_runtime(root, requirements)
    requirements.write_text("example>=2\n")
    reuse_python_runtime(root, requirements.parent, tmp_path / "changed_runtime")
    assert not (tmp_path / "changed_runtime").exists()
    requirements.write_text("example>=1\n")
    next((root / "wheels").iterdir()).write_bytes(b"changed")
    with pytest.raises(ValueError, match="PYTHON_RUNTIME_CHANGED"):
        reuse_python_runtime(root, requirements.parent, tmp_path / "tampered_runtime")


def test_private_installer_is_locked_without_relaxing_default_contract(tmp_path):
    root, requirements = frozen_runtime(tmp_path)
    private = "#!/bin/sh\nset -eu\n"
    freeze_wheels(root, requirements, install_script=private)
    assert validate_python_runtime(root, requirements, install_script=private)
    with pytest.raises(ValueError, match="INSTALLER_CHANGED"):
        validate_python_runtime(root, requirements)
