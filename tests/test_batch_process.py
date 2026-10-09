from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from traceforge.reconstruction.batch_process import run_batch_process


def test_process_writes_logs_and_preserves_exit_code(tmp_path: Path) -> None:
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    code = run_batch_process(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"],
        cwd=tmp_path,
        stdout_path=stdout,
        stderr_path=stderr,
        timeout_seconds=2,
    )
    assert code == 7
    assert json.loads((tmp_path / "batch_process.json").read_text())["forced_kill"] is False
    assert stdout.read_text() == "out\n"
    assert stderr.read_text() == "err\n"


@pytest.mark.skipif(os.name != "posix", reason="批处理进程组用于 POSIX 环境")
def test_timeout_kills_parent_and_child_and_keeps_logs(tmp_path: Path) -> None:
    marker = tmp_path / "child-heartbeat"
    child_code = f"touch {marker}; sleep 30"
    script = (
        "import subprocess, sys, time; "
        f"child=subprocess.Popen([{chr(34)}/bin/dash{chr(34)}, {chr(34)}-c{chr(34)}, {child_code!r}]); "
        "print(1, flush=True); "
        "time.sleep(30)"
    )
    log = tmp_path / "run.log"
    started = time.monotonic()
    code = run_batch_process(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        stdout_path=log,
        timeout_seconds=2.0,
    )
    assert code is None
    assert time.monotonic() - started < 4
    assert int(log.read_text().splitlines()[0]) > 0
    assert marker.is_file()
    mtime = marker.stat().st_mtime_ns
    time.sleep(0.2)
    assert marker.stat().st_mtime_ns == mtime


@pytest.mark.skipif(os.name != "posix", reason="批处理进程组用于 POSIX 环境")
def test_timeout_allows_process_cleanup_and_records_exit(tmp_path: Path) -> None:
    marker = tmp_path / "cleanup"
    script = (
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "def cleanup(*args):\n"
        f"    Path({str(marker)!r}).write_text('cleaned')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGINT, cleanup)\n"
        "print('started', flush=True)\n"
        "time.sleep(30)\n"
    )
    code = run_batch_process([sys.executable, "-c", script], cwd=tmp_path,
                             stdout_path=tmp_path / "log", timeout_seconds=1)
    assert code is None
    assert marker.read_text() == "cleaned"
    state = json.loads((tmp_path / "batch_process.json").read_text())
    assert state["status"] == "EXITED"


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, subprocess.TimeoutExpired])
@pytest.mark.parametrize("cleanup", ["graceful", "forced", "exit_race"])
def test_cleanup_grace_preserves_interrupt_and_records_forced_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException], cleanup: str,
) -> None:
    from unittest.mock import Mock, call

    from traceforge.reconstruction import batch_process

    initial = (KeyboardInterrupt() if interruption is KeyboardInterrupt
               else subprocess.TimeoutExpired("run", 10))
    cleanup_timeout = cleanup != "graceful"
    force_kill = cleanup == "forced"
    outcomes = iter([
        initial,
        subprocess.TimeoutExpired("cleanup", 180) if cleanup_timeout else -signal.SIGINT,
        -signal.SIGKILL if force_kill else -signal.SIGINT,
    ])
    process = Mock(pid=54321, returncode=None)

    def wait(*args: object, **kwargs: object) -> int:
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        process.returncode = outcome
        return outcome

    process.wait.side_effect = wait
    process.poll.side_effect = lambda: process.returncode
    monkeypatch.setattr(batch_process.subprocess, "Popen", Mock(return_value=process))
    killpg = Mock(side_effect=[None, ProcessLookupError()] if cleanup == "exit_race" else None)
    monkeypatch.setattr(batch_process.os, "killpg", killpg)
    kwargs = {"cwd": tmp_path, "stdout_path": tmp_path / "log", "timeout_seconds": 10}
    if interruption is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            run_batch_process(["unused"], **kwargs)
    else:
        assert run_batch_process(["unused"], **kwargs) is None

    assert process.wait.call_args_list == (
        [call(timeout=10), call(timeout=180)] + ([call()] if cleanup_timeout else [])
    )
    assert killpg.call_args_list == (
        [call(process.pid, signal.SIGINT)]
        + ([call(process.pid, signal.SIGKILL)] if cleanup_timeout else [])
    )
    state = json.loads((tmp_path / "batch_process.json").read_text())
    assert state["status"] == "EXITED"
    assert state["cleanup_grace_seconds"] == 180
    assert state["forced_kill"] is force_kill
    assert state["exit_code"] == (-signal.SIGKILL if force_kill else -signal.SIGINT)
