from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "run_session_batch", _REPO / "scripts" / "run_session_batch.py"
)
assert _SPEC is not None and _SPEC.loader is not None
batch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(batch)


def inventory(tmp_path: Path) -> tuple[Path, Path]:
    sessions = tmp_path / "sessions.jsonl"
    sessions.write_text(
        json.dumps(
            {
                "session_id": "r04-one",
                "input": str(tmp_path / "missing-source.jsonl"),
                "rubric": "R04",
                "line_number": 1,
                "line_sha256": "a" * 64,
                "input_status": "PENDING",
            }
        ) + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "source_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "traceforge.session-source-manifest.v1",
                "status": "READY",
                "coverage_complete": True,
                "sessions": str(sessions),
                "pending_sessions": 1,
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    return manifest, config


def result_row(output: Path) -> dict:
    return json.loads((output / "batch_results.jsonl").read_text(encoding="utf-8"))


def test_session_batch_rejects_unknown_domain(tmp_path: Path) -> None:
    manifest, config = inventory(tmp_path)
    with pytest.raises(batch.BatchInputError, match="domain"):
        batch.execute_batch(
            manifest_path=manifest, output_root=tmp_path / "batch", config=config, domain="unknown",
        )
    assert not (tmp_path / "batch").exists()


@pytest.mark.parametrize("domain", ["search", "terminal"])
def test_session_batch_invokes_real_cli_and_records_missing_input(tmp_path: Path, domain: str) -> None:
    manifest, config = inventory(tmp_path)
    output = tmp_path / "batch"

    report = batch.execute_batch(
        manifest_path=manifest, output_root=output, config=config, repo_root=_REPO, domain=domain
    )

    row = result_row(output)
    assert row["status"] == "PROCESS_ERROR"
    assert row["exit_code"] == 2
    assert row["manifest"] is None
    assert report["status_counts"] == {"PROCESS_ERROR": 1}
    assert report["domain"] == domain
    stderr = (output / "runs" / "r04-one" / "batch_stderr.txt").read_text(encoding="utf-8")
    assert "原始 JSONL 不存在" in stderr


@pytest.mark.parametrize(
    ("receipt_status", "exit_code", "expected"),
    [
        ("READY", 0, "PROCESS_ERROR"),
        ("READY", 2, "PROCESS_ERROR"),
        ("SESSION_TASK_REVIEW", 2, "SESSION_TASK_REVIEW"),
        ("INPUT_INVALID", 2, "INPUT_INVALID"),
    ],
)
def test_session_batch_requires_reconstruction_manifest_for_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    receipt_status: str, exit_code: int, expected: str,
) -> None:
    manifest, config = inventory(tmp_path)
    output = tmp_path / "batch"

    def run(command: list[str], **kwargs: object) -> int | None:
        run_root = Path(command[command.index("--output") + 1])
        receipt = run_root / "session_segmentation" / "session_task_segmentation.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"status": receipt_status}), encoding="utf-8")
        return exit_code

    monkeypatch.setattr(batch, "run_batch_process", run)
    report = batch.execute_batch(
        manifest_path=manifest, output_root=output, config=config, repo_root=_REPO, domain="terminal"
    )

    row = result_row(output)
    assert row["status"] == expected
    assert row["manifest"] is None
    assert row["stage_receipt"].endswith("session_task_segmentation.json")
    assert report["status_counts"] == {expected: 1}

def test_session_batch_records_timeout_and_preserves_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, config = inventory(tmp_path)
    output = tmp_path / "batch"

    def run(command: list[str], **kwargs: object) -> int | None:
        assert kwargs["timeout_seconds"] == 1
        Path(kwargs["stdout_path"]).write_text("partial")
        Path(kwargs["stderr_path"]).write_text("slow")
        return None

    monkeypatch.setattr(batch, "run_batch_process", run)
    report = batch.execute_batch(
        manifest_path=manifest,
        output_root=output,
        config=config,
        domain="terminal",
        repo_root=_REPO,
        session_timeout_seconds=1,
    )

    row = result_row(output)
    assert row["status"] == "PROCESS_TIMEOUT"
    assert row["exit_code"] is None
    assert row["timeout_seconds"] == 1
    assert report["status"] == "COMPLETED_WITH_ERRORS"
    assert (output / "runs" / "r04-one" / "batch_stdout.txt").read_text() == "partial"
    assert (output / "runs" / "r04-one" / "batch_stderr.txt").read_text() == "slow"


def test_session_batch_real_timeout_continues_to_next_session(tmp_path: Path) -> None:
    manifest, config = inventory(tmp_path)
    payload = json.loads(manifest.read_text())
    sessions = Path(payload["sessions"])
    first = json.loads(sessions.read_text())
    sessions.write_text(
        json.dumps(first) + "\n"
        + json.dumps({**first, "session_id": "r04-two", "line_number": 2}) + "\n"
    )
    payload["pending_sessions"] = 2
    manifest.write_text(json.dumps(payload))
    repo = tmp_path / "fake_repo"
    package = repo / "src" / "traceforge"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "def main():\n"
        "    line = sys.argv[sys.argv.index('--line-number') + 1]\n"
        "    print('session ' + line, flush=True)\n"
        "    if line == '1':\n"
        "        time.sleep(30)\n"
        "    root = Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "    (root / 'reconstruction_manifest.json').write_text(json.dumps({'status': 'READY'}))\n"
        "    return 0\n"
    )
    output = tmp_path / "batch"
    report = batch.execute_batch(
        manifest_path=manifest, output_root=output, config=config, domain="terminal",
        repo_root=repo, session_timeout_seconds=2,
    )
    rows = [json.loads(line) for line in (output / "batch_results.jsonl").read_text().splitlines()]
    assert [row["status"] for row in rows] == ["PROCESS_TIMEOUT", "READY"]
    assert [row["exit_code"] for row in rows] == [None, 0]
    assert report["completed_sessions"] == 2
    assert report["status"] == "COMPLETED_WITH_ERRORS"
    assert (output / "runs" / "r04-one" / "batch_stdout.txt").read_text() == "session 1\n"


def test_search_batch_preserves_real_completion_and_unassessed_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, config = inventory(tmp_path)
    output = tmp_path / "batch"
    task = {"task_id": "search-task", "status": "ROLLOUT_COMPLETED",
            "acceptance": "NOT_ASSESSED", "environment_review": "COMPLETE"}

    def run(command: list[str], **kwargs: object) -> int:
        run_root = Path(command[command.index("--output") + 1])
        (run_root / "manifest.json").write_text(json.dumps({
            "schema_version": "traceforge.search-reconstruction.v1",
            "status": "COMPLETED", "domain_route": "retrieval",
            "acceptance": "NOT_ASSESSED", "tasks": [task],
        }))
        return 0

    monkeypatch.setattr(batch, "run_batch_process", run)
    report = batch.execute_batch(manifest_path=manifest, output_root=output, config=config,
                                 domain="search", repo_root=_REPO)
    row = result_row(output)
    assert row["status"] == "COMPLETED"
    assert row["manifest"].endswith("/manifest.json")
    assert row["acceptance"] == "NOT_ASSESSED"
    assert row["tasks"] == [task]
    assert report["status"] == "COMPLETED"


def test_batch_passes_same_runtime_and_review_options_as_single_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, config = inventory(tmp_path)
    output = tmp_path / "batch"

    def run(command: list[str], **kwargs: object) -> int:
        assert command[command.index("--hermes-home") + 1] == str(tmp_path / "hermes")
        assert command[command.index("--harbor-root") + 1] == str(tmp_path / "harbor")
        assert "--manual-response-review" in command
        run_root = Path(command[command.index("--output") + 1])
        (run_root / "reconstruction_manifest.json").write_text(json.dumps({"status": "REVIEW"}))
        return 2

    monkeypatch.setattr(batch, "run_batch_process", run)
    report = batch.execute_batch(
        manifest_path=manifest, output_root=output, config=config, domain="terminal",
        repo_root=_REPO, hermes_home=tmp_path / "hermes", harbor_root=tmp_path / "harbor",
        manual_response_review=True,
    )
    assert result_row(output)["status"] == "REVIEW"
    assert report["status"] == "COMPLETED_WITH_ERRORS"


def test_resume_keeps_finished_session_and_interrupted_attempt(tmp_path, monkeypatch):
    manifest, config = inventory(tmp_path)
    data = json.loads(manifest.read_text())
    sessions = Path(data["sessions"])
    first = json.loads(sessions.read_text())
    sessions.write_text(json.dumps(first) + "\n" + json.dumps({
        **first, "session_id": "r04-two", "line_number": 2,
    }) + "\n")
    data["pending_sessions"] = 2
    manifest.write_text(json.dumps(data))
    output = tmp_path / "batch"
    calls = []

    def run(command, **kwargs):
        number = int(command[command.index("--line-number") + 1])
        root = Path(command[command.index("--output") + 1])
        calls.append(number)
        if len(calls) == 2:
            (root / "batch_stdout.txt").write_text("中断前的真实进度")
            (root / "batch_process.json").write_text(json.dumps({"status": "EXITED"}))
            raise OSError("控制端连接中断")
        (root / "reconstruction_manifest.json").write_text(json.dumps({"status": "READY"}))
        return 0

    monkeypatch.setattr(batch, "run_batch_process", run)
    with pytest.raises(OSError, match="中断"):
        batch.execute_batch(manifest_path=manifest, output_root=output, config=config,
                            domain="terminal", repo_root=_REPO)
    progress = json.loads((output / "batch_manifest.json").read_text())
    assert progress["completed_sessions"] == 1
    assert progress["status"] == "RUNNING"
    report = batch.execute_batch(manifest_path=manifest, output_root=output, config=config,
                                 domain="terminal", repo_root=_REPO, resume=True)
    assert calls == [1, 2, 2]
    assert report["completed_sessions"] == 2
    assert report["status"] == "COMPLETED"
    assert (output / "runs/r04-two/batch_stdout.txt").read_text() == "中断前的真实进度"
    assert "/retries/" in report["session_results"][1]["manifest"]
    batch.execute_batch(manifest_path=manifest, output_root=output, config=config,
                        domain="terminal", repo_root=_REPO, resume=True)
    assert calls == [1, 2, 2]
    with pytest.raises(batch.BatchInputError, match="配置"):
        batch.execute_batch(manifest_path=manifest, output_root=output, config=config,
                            domain="terminal", repo_root=_REPO, resume=True, execute_rollout=True)


def test_batch_domain_mismatch_fails_before_model_start(tmp_path):
    manifest, config = inventory(tmp_path)
    data = json.loads(manifest.read_text())
    data["domain"] = "search"
    manifest.write_text(json.dumps(data))
    with pytest.raises(batch.BatchInputError, match="domain"):
        batch.execute_batch(manifest_path=manifest, output_root=tmp_path / "batch",
                            config=config, domain="terminal")

def test_success_manifest_cannot_hide_process_failure(tmp_path, monkeypatch):
    manifest, config = inventory(tmp_path)
    output = tmp_path / "batch"

    def run(command, **kwargs):
        root = Path(command[command.index("--output") + 1])
        (root / "manifest.json").write_text(json.dumps({"status": "COMPLETED"}))
        return 1

    monkeypatch.setattr(batch, "run_batch_process", run)
    report = batch.execute_batch(manifest_path=manifest, output_root=output, config=config,
                                 domain="search", repo_root=_REPO)
    assert report["status"] == "COMPLETED_WITH_ERRORS"
    assert result_row(output)["status"] == "PROCESS_ERROR"


def test_broken_manifest_is_recorded_and_batch_continues(tmp_path, monkeypatch):
    manifest, config = inventory(tmp_path)
    data = json.loads(manifest.read_text())
    sessions = Path(data["sessions"])
    first = json.loads(sessions.read_text())
    sessions.write_text(json.dumps(first) + "\n" + json.dumps({
        **first, "session_id": "r04-two", "line_number": 2,
    }) + "\n")
    data["pending_sessions"] = 2
    manifest.write_text(json.dumps(data))

    def run(command, **kwargs):
        root = Path(command[command.index("--output") + 1])
        number = int(command[command.index("--line-number") + 1])
        content = '{"status":' if number == 1 else json.dumps({
            "status": "READY", "tasks": [
                {"task_id": "task", "status": "READY", "source_task": {"user_text": "large original"}}
            ],
        })
        (root / "reconstruction_manifest.json").write_text(content)
        return 1 if number == 1 else 0

    monkeypatch.setattr(batch, "run_batch_process", run)
    report = batch.execute_batch(manifest_path=manifest, output_root=tmp_path / "batch",
                                 config=config, domain="terminal", repo_root=_REPO)
    assert report["status_counts"] == {"RECEIPT_ERROR": 1, "READY": 1}
    assert report["session_results"][0]["errors"]
    assert report["session_results"][1]["tasks"] == [{"task_id": "task", "status": "READY"}]


@pytest.mark.parametrize("changed", ["config", "code"])
def test_resume_rejects_changed_content_at_same_path(tmp_path, monkeypatch, changed):
    manifest, config = inventory(tmp_path)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    code = repo / "src/runtime.py"
    code.write_text("version = 1\n")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        root = Path(command[command.index("--output") + 1])
        (root / "manifest.json").write_text(json.dumps({"status": "COMPLETED"}))
        return 0

    monkeypatch.setattr(batch, "run_batch_process", run)
    options = dict(manifest_path=manifest, output_root=tmp_path / "batch", config=config,
                   domain="search", repo_root=repo)
    batch.execute_batch(**options)
    batch.execute_batch(**options, resume=True)
    assert len(calls) == 1
    if changed == "config":
        config.write_text("model: another-model\n")
    else:
        code.write_text("version = 2\n")
    with pytest.raises(batch.BatchInputError, match="配置"):
        batch.execute_batch(**options, resume=True)
    assert len(calls) == 1
