"""依赖锁定既约束原声明，也约束离线 wheel 和安装入口。"""

import hashlib
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


@pytest.mark.parametrize("changed", ["requirements", "wheel", "installer", "source", "lock"])
def test_changed_dependency_inputs_cannot_reuse_the_runtime(tmp_path, changed):
    root, requirements = frozen_runtime(tmp_path)
    path = {"requirements": requirements, "wheel": next((root / "wheels").iterdir()),
            "installer": root / "install.sh", "source": root / "requirements.source.txt",
            "lock": root / "requirements.lock"}[changed]
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


@pytest.mark.parametrize("original,current", [
    (b"example>=1\r\n", b"example>=1\n"),
    (b"example>=1\n", b"example>=1\r\n"),
])
def test_newline_only_reuse_preserves_frozen_source_and_hashes(tmp_path, original, current):
    root, requirements = frozen_runtime(tmp_path)
    requirements.write_bytes(original)
    manifest = freeze_wheels(root, requirements)
    frozen = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    requirements.write_bytes(current)
    target = tmp_path / "next_runtime"

    reuse_python_runtime(root, requirements.parent, target)

    assert validate_python_runtime(target, requirements) == manifest
    assert manifest["requirements_sha256"] == hashlib.sha256(original).hexdigest()
    assert manifest["requirements_sha256"] != hashlib.sha256(current).hexdigest()
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == frozen
    copied = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    assert copied == frozen
    assert requirements.read_bytes() == current


@pytest.mark.parametrize("current", [
    b"example>=2\r\n", b"example>=1 --pre\r\n", b"example>=1 # new comment\r\n",
    b"example>=1", b"example>=1\r",
])
def test_newline_compatibility_does_not_normalize_other_input_changes(tmp_path, current):
    root, requirements = frozen_runtime(tmp_path)
    requirements.write_bytes(current)
    with pytest.raises(ValueError, match="PYTHON_RUNTIME_REQUIREMENTS_CHANGED"):
        validate_python_runtime(root, requirements)


@pytest.mark.parametrize("changed", ["requirements.source.txt", "requirements.lock"])
def test_newline_compatible_input_cannot_hide_frozen_input_tampering(tmp_path, changed):
    root, requirements = frozen_runtime(tmp_path)
    requirements.write_bytes(b"example>=1\r\n")
    path = root / changed
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError, match="PYTHON_RUNTIME_CHANGED"):
        validate_python_runtime(root, requirements)
