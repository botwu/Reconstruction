"""命令行入口行为测试。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import json_line

from traceforge.cli import main
from traceforge.trajectory.provenance import GitProvenance
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


def test_trajectory_compile_command_rejects_file_as_output_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"{}\n")
    output = tmp_path / "不是目录"
    output.write_text("不得覆盖", encoding="utf-8")

    exit_code = main(
        [
            "trajectory",
            "compile",
            "--input",
            str(source),
            "--dataset-id",
            "cli-output-file-v1",
            "--source-schema",
            RESTORED_LONG_CAPTURE_SCHEMA,
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "artifact 根路径必须是目录" in captured.err
    assert "Traceback" not in captured.err
    assert output.read_text(encoding="utf-8") == "不得覆盖"


def test_trajectory_compile_command_wraps_artifact_io_error_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"{}\n")

    def fail_temporary_file(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise OSError("不应暴露的底层错误")

    monkeypatch.setattr(
        "traceforge.trajectory.artifacts.tempfile.mkstemp",
        fail_temporary_file,
    )

    exit_code = main(
        [
            "trajectory",
            "compile",
            "--input",
            str(source),
            "--dataset-id",
            "cli-artifact-error-v1",
            "--source-schema",
            RESTORED_LONG_CAPTURE_SCHEMA,
            "--output",
            str(tmp_path / "artifacts"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "无法创建 JSON artifact 临时文件" in captured.err
    assert "Traceback" not in captured.err
    assert "不应暴露的底层错误" not in captured.err


@pytest.mark.parametrize("head_changed", [False, True])
def test_run_receipt_verifies_git_snapshot_at_completion_without_paths(
    head_changed: bool,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    capture = capture_factory(
        messages=[{"role": "assistant", "content": "虚构完成。"}],
        terminal_prefix_depths=[1],
    )
    source = tmp_path / "不得进入回执.jsonl"
    output = tmp_path / "不得进入回执-artifacts"
    raw = json_line(capture)
    source.write_bytes(raw)
    initial = GitProvenance(True, "a" * 40, "b" * 40, True)
    completion = GitProvenance(True, "c" * 40, "d" * 40, True) if head_changed else initial
    snapshots = iter((initial, completion))
    monkeypatch.setattr(
        "traceforge.trajectory.pipeline.collect_git_provenance",
        lambda: next(snapshots),
    )

    exit_code = main(
        [
            "trajectory",
            "compile",
            "--input",
            str(source),
            "--dataset-id",
            f"cli-provenance-{head_changed!s}-v1",
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
    receipt = json.loads((published / "run_receipt.json").read_bytes())
    assert receipt["git_provenance"] == initial.to_dict()
    assert receipt["git_provenance_verified_at_completion"] is not head_changed
    serialized_receipt = json.dumps(receipt, ensure_ascii=False)
    assert str(source) not in serialized_receipt
    assert str(output) not in serialized_receipt
    assert source.name not in serialized_receipt
    assert output.name not in serialized_receipt
