from __future__ import annotations

import json
import os
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
