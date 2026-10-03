"""运行时源码回退只使用当前 checkout，保留显式路径和已安装导入。"""

import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceforge.harbor_ags import results


@pytest.mark.parametrize("installed", [False, True])
def test_ledger_import_preserves_installed_module_or_uses_checkout(monkeypatch, installed):
    expected = Path(results.__file__).resolve().parents[3] / "integrations/harbor_ags/src"
    before = [path for path in sys.path if path != str(expected)]
    monkeypatch.setattr(sys, "path", before.copy())
    audit = object()
    real_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == "harbor_ags.sandbox_ledger":
            if installed or str(expected) in sys.path:
                return SimpleNamespace(audit_sandbox_ledger=audit)
            raise ImportError("确定性模拟：没有预安装模块")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    assert results._import_audit_sandbox_ledger() is audit
    assert sys.path == (before if installed else [str(expected), *before])


def test_missing_checkout_does_not_probe_neighbor_project(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "_HARBOR_AGS_SRC", tmp_path / "missing-src")
    before = sys.path.copy()
    monkeypatch.setattr(sys, "path", before.copy())
    real_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == "harbor_ags.sandbox_ledger":
            raise ImportError("确定性模拟：没有预安装模块")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    with pytest.raises(results.HarborResultError, match="无法导入"):
        results._import_audit_sandbox_ledger()
    assert sys.path == before


def test_explicit_certification_runtime_precedes_checkout(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    source = selected / "src"
    (source / "harbor_ags").mkdir(parents=True)
    (source / "harbor_ags/artifacts.py").write_text("# 显式路径选择 fixture\\n")
    job = tmp_path / "empty-job"
    job.mkdir()
    monkeypatch.setattr(sys, "path", sys.path.copy())
    results.certify_hermes_job(job, harbor_root=selected)
    assert sys.path[0] == str(source)
