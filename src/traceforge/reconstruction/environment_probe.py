"""在已授权的只读沙盒中采集环境探针事实，不执行宿主机代码或判定可解性。"""

from __future__ import annotations

import hashlib
import json
import shlex
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from traceforge.reconstruction.agents.session import AgentSession

ENVIRONMENT_PROBE_SCHEMA = "traceforge.environment-probe.v1"
PROBE_PURPOSES = frozenset({"load", "reset", "dependency", "task_conflict"})

# 包装器只在 binding.runtime.exec 的远程进程中执行。子进程的 stdout 与
# 包装器收据分离，防止探针打印的 JSON 被误当作执行元数据。
_REMOTE_RUNNER = r'''
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

def snapshot(root):
    if not root.is_dir():
        raise FileNotFoundError("探针工作区不存在: " + str(root))
    result = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[rel] = "symlink:" + str(path.readlink())
        elif path.is_file():
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result

workspace = Path(workspace_path)
before = snapshot(workspace)
executions = []
for index in range(repetitions):
    with tempfile.TemporaryDirectory(prefix="traceforge-env-probe-") as scratch:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["TRACEFORGE_WORKSPACE"] = str(workspace)
        env["TRACEFORGE_PROBE_SCRATCH"] = scratch
        env["TMPDIR"] = scratch
        child = (
            "import os, sys\n"
            "sys.dont_write_bytecode = True\n"
            "workspace = os.environ['TRACEFORGE_WORKSPACE']\n"
            "os.chdir(workspace)\n"
            "sys.path.insert(0, workspace)\n"
            "exec(compile(" + repr(python_code) + ", '<environment-probe>', 'exec'), "
            "{'__name__': '__main__'})\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", child], cwd=workspace, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        executions.append({
            "index": index,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "scratch_root": scratch,
            "scratch_hashes": snapshot(Path(scratch)),
            "workspace_after": snapshot(workspace),
        })
        # 检测到输入污染后不再进行第二次探针，避免把污染环境当成 reset。
        if executions[-1]["workspace_after"] != before or timed_out:
            break
after = snapshot(workspace)
print(json.dumps({
    "workspace_before": before, "workspace_after": after,
    "executions": executions,
}, ensure_ascii=False))
'''


def _store(session: AgentSession, result: dict[str, Any]) -> dict[str, Any]:
    session.environment_probes.append(result)
    return result


def _comparison_output(execution: dict[str, Any], field: str) -> Any:
    """比较时忽略执行器分配的目录名，原始输出与文件哈希保持不变。"""
    value = execution[field]
    scratch = execution.get("scratch_root")
    if field in {"stdout", "stderr"} and isinstance(scratch, str) and scratch:
        return value.replace(scratch, "<TRACEFORGE_PROBE_SCRATCH>")
    return value


def run_environment_probe(
    session: AgentSession,
    *,
    python_code: str,
    purpose: str,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """记录真实远程结果；超时与执行设施异常不能证明任务不可完成。"""

    result: dict[str, Any] = {
        "schema_version": ENVIRONMENT_PROBE_SCHEMA,
        "probe_id": "probe-" + uuid.uuid4().hex,
        "purpose": purpose if isinstance(purpose, str) else None,
        "python_code": python_code if isinstance(python_code, str) else None,
        "code_sha256": hashlib.sha256(python_code.encode()).hexdigest()
        if isinstance(python_code, str) else None,
        "timeout_seconds": timeout_seconds,
        "status": "REJECTED",
        "execution_plane": "sandbox",
        "executions": [],
        "workspace_before": None,
        "workspace_after": None,
        "environment_unchanged": None,
        "reproducible": None,
        "reset_reproducible": None,
        "reset_scope": "independent_scratch_and_workspace_snapshot"
        if purpose == "reset" else None,
        "limitations": [
            "PASS 仅表示探针进程成功且快照未变；检查条件必须用 assert 或非零退出表达失败。",
            "探针从任务工作区执行；临时写操作必须使用 TRACEFORGE_PROBE_SCRATCH。",
            "仅验证探针实际执行的能力，不证明任务数学可解。",
            "reset 仅比较独立临时目录的输出和文件快照，不重置外部服务。",
        ],
    }
    if not session.allow_environment_probe or session.sandbox is None:
        result["error_code"] = "ENVIRONMENT_PROBE_NOT_AUTHORIZED"
        return _store(session, result)
    if session.read_only_probe_blocked is not True:
        result.update(status="INFRA_ERROR", error_code="ENVIRONMENT_PROBE_INPUT_NOT_PROTECTED")
        return _store(session, result)
    if (
        not isinstance(python_code, str)
        or not python_code.strip()
        or not isinstance(purpose, str)
        or purpose not in PROBE_PURPOSES
        or type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= 60
    ):
        result["error_code"] = "ENVIRONMENT_PROBE_INVALID_ARGUMENTS"
        return _store(session, result)

    from traceforge.reconstruction.agents.sandbox import run_coro

    binding = session.sandbox
    repetitions = 2 if purpose in {"reset", "task_conflict"} else 1
    script = (
        f"workspace_path = {binding.remote_root!r}\n"
        f"python_code = {python_code!r}\n"
        f"timeout_seconds = {timeout_seconds!r}\n"
        f"repetitions = {repetitions!r}\n"
        + _REMOTE_RUNNER
    )
    command = "PYTHONDONTWRITEBYTECODE=1 python3 -B -c " + shlex.quote(script)
    try:
        remote = run_coro(
            binding.runtime.exec(
                command, cwd="/", timeout_sec=timeout_seconds * repetitions + 15, user="user"
            )
        )
    except Exception as exc:
        result.update(
            status="INFRA_ERROR",
            error_code="ENVIRONMENT_PROBE_RUNTIME_ERROR",
            runtime_error=f"{type(exc).__name__}: {exc}",
        )
        return _store(session, result)
    result["runtime_stdout"] = str(getattr(remote, "stdout", "") or "")
    result["runtime_stderr"] = str(getattr(remote, "stderr", "") or "")
    result["runtime_exit_code"] = getattr(remote, "return_code", None)
    if type(result["runtime_exit_code"]) is not int or result["runtime_exit_code"] != 0:
        result.update(status="INFRA_ERROR", error_code="ENVIRONMENT_PROBE_RUNNER_FAILED")
        return _store(session, result)
    try:
        payload = json.loads(result["runtime_stdout"])
        before, after, executions = (
            payload["workspace_before"], payload["workspace_after"], payload["executions"]
        )
        valid = (
            isinstance(before, dict) and isinstance(after, dict)
            and isinstance(executions, list) and 1 <= len(executions) <= repetitions
            and all(
                isinstance(item, dict)
                and type(item.get("exit_code")) is int
                and isinstance(item.get("stdout"), str)
                and isinstance(item.get("stderr"), str)
                and type(item.get("timed_out")) is bool
                and isinstance(item.get("workspace_after"), dict)
                and isinstance(item.get("scratch_hashes"), dict)
                for item in executions
            )
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        valid = False
    if not valid:
        result.update(status="INFRA_ERROR", error_code="ENVIRONMENT_PROBE_RECEIPT_INVALID")
        return _store(session, result)

    result.update(workspace_before=before, workspace_after=after, executions=executions)
    unchanged = before == after and all(item["workspace_after"] == before for item in executions)
    result["environment_unchanged"] = unchanged
    fields = ("stdout", "stderr", "exit_code", "scratch_hashes")
    repeatable = (
        len(executions) == 2
        and all(not item["timed_out"] for item in executions)
        and all(_comparison_output(executions[0], field) == _comparison_output(executions[1], field)
                for field in fields)
    )
    if repetitions == 2:
        result["reproducible"] = repeatable
    if purpose == "reset":
        result["reset_reproducible"] = repeatable
    if not unchanged:
        result.update(status="PIPELINE_ERROR", error_code="ENVIRONMENT_PROBE_WORKSPACE_CHANGED")
        session.policy_errors.append("ENVIRONMENT_PROBE_WORKSPACE_CHANGED")
    elif any(item["timed_out"] for item in executions):
        result.update(status="INFRA_ERROR", error_code="ENVIRONMENT_PROBE_TIMEOUT")
    elif any(item["exit_code"] != 0 for item in executions):
        result.update(status="FAIL", error_code="ENVIRONMENT_PROBE_NONZERO_EXIT")
    elif purpose == "reset":
        result.update(
            status="PASS" if repeatable else "FAIL",
            error_code=None if repeatable else "ENVIRONMENT_PROBE_RESET_NOT_REPRODUCIBLE",
        )
    else:
        result.update(status="PASS", error_code=None)
    return _store(session, result)


def environment_probe_summary(result: dict[str, Any], *, max_chars: int = 8000) -> str:
    """给模型返回有界收据，完整输出和快照保留在 session 中。"""

    text = json.dumps(result, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    summary = {
        key: result.get(key)
        for key in (
            "schema_version", "probe_id", "purpose", "code_sha256", "status", "error_code",
            "environment_unchanged", "reproducible", "reset_reproducible", "reset_scope",
            "runtime_exit_code",
        )
    }
    summary["truncated"] = True
    summary["full_result_location"] = "session.environment_probes"
    summary["executions"] = [
        {key: item.get(key) for key in ("index", "exit_code", "timed_out", "stdout", "stderr")}
        for item in result.get("executions") or []
    ]
    limit = max_chars // 8
    while True:
        for index, item in enumerate(summary["executions"]):
            for key in ("stdout", "stderr"):
                output = str(result["executions"][index].get(key) or "")
                # 错误类型和 pytest 结论通常在末尾，不能只留下调用栈开头。
                head = limit // 3
                item[key] = output if len(output) <= limit else output[:head] + "\n…\n" + output[-(limit - head):]
        text = json.dumps(summary, ensure_ascii=False)
        if len(text) <= max_chars:
            return text
        limit //= 2
        if not limit:
            return json.dumps(
                {"probe_id": result["probe_id"], "status": result["status"], "truncated": True}
            )[:max_chars]
