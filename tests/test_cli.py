"""命令行入口行为测试。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import json_line

from traceforge.cli import main
from traceforge.trajectory.source_adapter import RESTORED_LONG_CAPTURE_SCHEMA


def test_trajectory_compile_command_publishes_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    raw = json_line(capture)
    source = tmp_path / "cli.jsonl"
    output = tmp_path / "artifacts"
    source.write_bytes(raw)

    exit_code = main(
        [
            "trajectory",
            "compile",
            "--input",
            str(source),
            "--dataset-id",
            "cli-fixture-v1",
            "--source-schema",
            RESTORED_LONG_CAPTURE_SCHEMA,
            "--expected-sha256",
            hashlib.sha256(raw).hexdigest(),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    published = Path(capsys.readouterr().out.strip())
    assert published.is_dir()
    assert (published / "artifact_manifest.json").is_file()


def test_trajectory_compile_command_reports_digest_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "wrong-digest.jsonl"
    source.write_bytes(b"{}\n")

    exit_code = main(
        [
            "trajectory",
            "compile",
            "--input",
            str(source),
            "--dataset-id",
            "cli-wrong-digest-v1",
            "--source-schema",
            RESTORED_LONG_CAPTURE_SCHEMA,
            "--expected-sha256",
            "0" * 64,
            "--output",
            str(tmp_path / "artifacts"),
        ]
    )

    assert exit_code == 2
    assert "来源 SHA-256 与冻结值不一致" in capsys.readouterr().err


def test_trajectory_compile_command_rejects_unsupported_schema_before_reading(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "artifacts"
    exit_code = main(
        [
            "trajectory",
            "compile",
            "--input",
            str(tmp_path / "不存在.jsonl"),
            "--dataset-id",
            "cli-unsupported-schema-v1",
            "--source-schema",
            "traceforge.future-capture.v1",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 2
    assert "不支持该 source_schema" in capsys.readouterr().err
    assert not output.exists()
