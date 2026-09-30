"""批处理的进程总时限；日志直接落盘，超时终止整个本地进程组。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path


def run_batch_process(
    command: Sequence[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path | None = None,
    timeout_seconds: float,
    env: Mapping[str, str] | None = None,
) -> int | None:
    """返回退出码；超时返回 None。stderr_path 为空时合并到 stdout。"""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")
    with ExitStack() as stack:
        stdout = stack.enter_context(stdout_path.open("wb"))
        stderr = (
            stack.enter_context(stderr_path.open("wb"))
            if stderr_path is not None
            else subprocess.STDOUT
        )
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        state_path = stdout_path.parent / "batch_process.json"

        def save_state(status: str) -> None:
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"status": status, "pid": process.pid,
                                             "exit_code": process.poll()}) + "\n")
            temporary.replace(state_path)

        try:
            save_state("RUNNING")
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return None
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGINT)
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                except ProcessLookupError:
                    process.wait()
            save_state("EXITED")
