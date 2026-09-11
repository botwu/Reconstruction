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
            f"cli-provenance-{str(head_changed).lower()}-v1",
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


def test_query_turns_build_command_publishes_run(
    stable_git_provenance: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    """query-turns build 在已发布 M1B run 之上派生并发布 M1D run，回打印内容寻址目录。"""

    capture = capture_factory(
        messages=[
            {"role": "user", "content": "虚构提问。"},
            {"role": "assistant", "content": "虚构回答。"},
        ],
        terminal_prefix_depths=[2],
    )
    m1b_run = compile_dataset([capture], label="cli-m1d")
    output = tmp_path / "m1d-artifacts"

    exit_code = main(["query-turns", "build", "--m1b-run", str(m1b_run), "--output", str(output)])

    assert exit_code == 0
    published = Path(capsys.readouterr().out.strip())
    assert published.is_dir()
    assert (published / "query_turn_manifest.json").is_file()
    assert (published / "reports/m1d_report.json").is_file()


def test_query_turns_build_command_reports_invalid_m1b_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """指向非 M1B 目录 → fail-closed 为 exit 2 且不外泄 traceback。"""

    not_a_run = tmp_path / "空目录"
    not_a_run.mkdir()

    exit_code = main(
        ["query-turns", "build", "--m1b-run", str(not_a_run), "--output", str(tmp_path / "out")]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "回合图构建失败" in captured.err
    assert "Traceback" not in captured.err


def test_source_projection_build_command_publishes_run_with_optional_m1d(
    stable_git_provenance: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    compile_dataset: Callable[..., Path],
    capture_factory: Callable[..., dict[str, Any]],
) -> None:
    """source-projection build 在 M1B run（+ 可选 M1D run）之上发布投影，回打印内容寻址目录。"""

    capture = capture_factory(
        messages=[
            {"role": "user", "content": "虚构提问。"},
            {"role": "assistant", "content": "虚构回答。"},
            {"role": "user", "content": "<environment_context>虚构环境</environment_context>"},
            {"role": "assistant", "content": "虚构回答二。"},
        ],
        terminal_prefix_depths=[2, 4],
    )
    m1b_run = compile_dataset([capture], label="cli-utp")
    exit_code = main(
        ["query-turns", "build", "--m1b-run", str(m1b_run), "--output", str(tmp_path / "m1d")]
    )
    assert exit_code == 0
    m1d_run = Path(capsys.readouterr().out.strip())

    exit_code = main(
        [
            "source-projection",
            "build",
            "--m1b-run",
            str(m1b_run),
            "--m1d-run",
            str(m1d_run),
            "--output",
            str(tmp_path / "utp"),
        ]
    )
    assert exit_code == 0
    published = Path(capsys.readouterr().out.strip())
    assert published.is_dir()
    assert (published / "projection_manifest.json").is_file()
    assert (published / "reports/projection_report.json").is_file()
    assert (published / "private/user_text_annotations.jsonl").is_file()

    exit_code = main(
        [
            "source-projection",
            "build",
            "--m1b-run",
            str(m1b_run),
            "--output",
            str(tmp_path / "utp-unbound"),
        ]
    )
    assert exit_code == 0
    unbound = Path(capsys.readouterr().out.strip())
    assert unbound.name != published.name


def test_source_projection_build_command_reports_invalid_input_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    not_a_run = tmp_path / "空目录"
    not_a_run.mkdir()

    exit_code = main(
        [
            "source-projection",
            "build",
            "--m1b-run",
            str(not_a_run),
            "--output",
            str(tmp_path / "out"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "来源投影构建失败" in captured.err
    assert "Traceback" not in captured.err


def test_failure_analysis_build_command_dispatches(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = tmp_path / "m4-run"
    published.mkdir()
    monkeypatch.setattr(
        "traceforge.cli.build_failure_analysis",
        lambda *, m1b_run_dir, output_root: published,
    )
    exit_code = main(
        [
            "failure-analysis",
            "build",
            "--m1b-run",
            str(tmp_path / "m1b"),
            "--output",
            str(tmp_path / "artifacts"),
        ]
    )
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == str(published)
