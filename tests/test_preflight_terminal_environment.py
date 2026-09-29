from __future__ import annotations

import importlib.util
from pathlib import Path

from traceforge.reconstruction.agents.sandbox import LocalExecRuntime

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/preflight_terminal_environment.py"


def _module():
    spec = importlib.util.spec_from_file_location("traceforge_preflight", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_sandbox_smoke_checks_pass_and_fail_without_model(tmp_path: Path) -> None:
    module = _module()
    report = module.run_sandbox_smoke(
        lambda: LocalExecRuntime(tmp_path / "runtime"),
        tmp_path / "smoke",
    )
    assert report["status"] == "PASS"
    assert report["model_calls"] is False
    assert [(row["name"], row["status"]) for row in report["runs"]] == [
        ("test_preflight_pass", "PASS"),
        ("test_preflight_fail", "FAIL"),
    ]
    assert all(row["input_unchanged"] is True for row in report["runs"])


def test_build_report_marks_sandbox_unchecked_and_never_e2e_ready(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    config = tmp_path / "config.yaml"
    config.write_text("roles: {}\\n", encoding="utf-8")
    monkeypatch.setattr(module, "resolve_role_matrix", lambda path: {})
    monkeypatch.setattr(module, "load_channel_connection", lambda path, channel: ("url", "key"))
    monkeypatch.setattr(module, "load_e2b_api_key", lambda path: "key")
    monkeypatch.setattr(module, "resolve_sandbox_api_key", lambda: "key")
    monkeypatch.setattr(module, "resolve_hermes_home", lambda path: tmp_path)
    harbor = tmp_path / "harbor"
    (harbor / "configs").mkdir(parents=True)
    for name in ("hermes-batch.yaml", "oracle.yaml", "nop.yaml"):
        (harbor / "configs" / name).write_text("", encoding="utf-8")
    harbor_bin = harbor / ".venv/bin/harbor"
    harbor_bin.parent.mkdir(parents=True)
    harbor_bin.write_text("#!/bin/sh\\n", encoding="utf-8")
    harbor_bin.chmod(0o755)
    report = module.build_report(config, harbor, tmp_path)
    assert report["status"] == "STATIC_PASS"
    assert report["scope"] == "LOCAL_STATIC_ONLY"
    assert report["sandbox_smoke"]["status"] == "UNCHECKED"
    assert report["end_to_end_verified"] is False
    assert "harbor_red_calibration" in report["unchecked_probes"]
