"""Harbor/AGS 边界适配器测试。"""

import json
from pathlib import Path

import pytest

from traceforge.harbor_ags.adapter import (
    HarborAgsAdapterError,
    build_boundary_plan,
    validate_bundle_layout,
)


def _bundle(root: Path) -> Path:
    for p in [root / "workspace", root / "environment", root / "solution", root / "tests/control"]:
        p.mkdir(parents=True)
    (root / "workspace/input.txt").write_text("public\n")
    (root / "environment/README").write_text("prebuilt\n")
    (root / "solution/solve.sh").write_text("#!/bin/sh\n")
    (root / "tests/grader.py").write_text("print('ok')\n")
    (root / "tests/rubric.json").write_text("{}\n")
    (root / "tests/control/truth.txt").write_text("hidden\n")
    (root / "tests/test.sh").write_text("#!/bin/sh\nset -eu\n\npython3 /tests/grader.py\n")
    (root / "task.toml").write_text(
        'schema_version = "1.4"\n[task]\nname = "traceforge/test"\n[verifier]\nenvironment_mode = "separate"\n[verifier.environment]\nnetwork_mode = "no-network"\n'
    )
    (root / "instruction.md").write_text("Do the task.\n")
    return root


def test_layout_rejects_missing_control(tmp_path: Path) -> None:
    root = _bundle(tmp_path / "task")
    (root / "tests/control").rmdir()
    with pytest.raises(HarborAgsAdapterError):
        validate_bundle_layout(root)


def test_plan_is_dry_run_and_declares_surfaces(tmp_path: Path) -> None:
    root = _bundle(tmp_path / "task")
    output = build_boundary_plan(root, output_root=tmp_path / "out", source_refs=["m4:report"])
    plan = json.loads((output / "harbor_boundary_plan.json").read_text())
    assert plan["model_status"] == "NOT_RUN"
    assert plan["agent_surface"]["visible_roots"] == ["/home/user/workspace"]
    assert plan["verifier_surface"]["visible_to_agent"] is False
    assert plan["status"] == "LOCAL_LAYOUT_ONLY"
