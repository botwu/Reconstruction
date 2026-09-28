"""验证环境探针的沙盒执行边界、真实结果记录及可复现性。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceforge.reconstruction.agents.sandbox import LocalExecRuntime, SandboxBinding, run_coro
from traceforge.reconstruction.agents.session import AgentSession, execute_tool, tool_schemas
from traceforge.reconstruction.environment_probe import run_environment_probe


def _session(tmp_path: Path) -> tuple[AgentSession, LocalExecRuntime]:
    runtime = LocalExecRuntime(tmp_path / "sandbox")
    run_coro(runtime.start(read_only=True))
    (runtime.remote / "entry.py").write_text("VALUE = 42\n", encoding="utf-8")
    session = AgentSession(
        sandbox=SandboxBinding(runtime),
        allow_environment_probe=True,
        read_only_probe_blocked=True,
    )
    return session, runtime


def test_probe_imports_remote_workspace_without_bytecode_or_host_access(tmp_path: Path) -> None:
    session, runtime = _session(tmp_path)
    host = tmp_path / "host"
    host.mkdir()
    (host / "entry.py").write_text("raise AssertionError('禁止宿主机执行')\n", encoding="utf-8")
    session.workspace = host
    result = run_environment_probe(
        session, python_code="import entry; print(entry.VALUE)", purpose="load"
    )
    assert result["status"] == "PASS"
    assert result["executions"][0]["stdout"] == "42\n"
    assert result["executions"][0]["exit_code"] == 0
    assert result["environment_unchanged"] is True
    assert result["workspace_before"] == result["workspace_after"]
    assert not (runtime.remote / "__pycache__").exists()
    assert session.environment_probes == [result]
    assert len(result["code_sha256"]) == 64


def test_reset_uses_independent_scratch_and_compares_files(tmp_path: Path) -> None:
    session, runtime = _session(tmp_path)
    result = run_environment_probe(
        session,
        python_code=(
            "from pathlib import Path\n"
            "import os\n"
            "state = Path(os.environ['TRACEFORGE_PROBE_SCRATCH']) / 'state.txt'\n"
            "assert not state.exists()\n"
            "state.write_text('reset')\n"
            "print('ready')\n"
        ),
        purpose="reset",
    )
    assert result["status"] == "PASS"
    assert len(result["executions"]) == 2
    assert result["reset_reproducible"] is True
    assert result["reproducible"] is True
    assert "state.txt" in result["executions"][0]["scratch_hashes"]
    assert not (runtime.remote / "state.txt").exists()


def test_reset_same_workspace_is_reproducible_without_declaring_unreconstructable(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)
    result = run_environment_probe(
        session, python_code="import os; print(os.getcwd())", purpose="reset"
    )
    assert result["status"] == "PASS"
    assert result["reset_reproducible"] is True
    assert result["environment_unchanged"] is True


def test_task_conflict_records_repeatable_nonzero_failure(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)
    result = run_environment_probe(
        session,
        python_code="import sys; print('unsupported', file=sys.stderr); sys.exit(7)",
        purpose="task_conflict",
    )
    assert result["status"] == "FAIL"
    assert result["reproducible"] is True
    assert [item["exit_code"] for item in result["executions"]] == [7, 7]
    assert result["executions"][0]["stderr"] == "unsupported\n"


def test_timeout_is_infrastructure_error_and_preserves_snapshot(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)
    result = run_environment_probe(
        session,
        python_code="import time; print('started', flush=True); time.sleep(10)",
        purpose="task_conflict",
        timeout_seconds=1,
    )
    assert result["status"] == "INFRA_ERROR"
    assert result["error_code"] == "ENVIRONMENT_PROBE_TIMEOUT"
    assert result["executions"][0]["stdout"] == "started\n"
    assert result["executions"][0]["timed_out"] is True
    assert result["reproducible"] is False
    assert result["environment_unchanged"] is True


def test_workspace_mutation_invalidates_probe_even_when_exit_is_zero(tmp_path: Path) -> None:
    # 本地替身不执行 AGS 文件权限，借此稳定复现权限失效后的二次哈希防线。
    session, _ = _session(tmp_path)
    result = run_environment_probe(
        session,
        python_code=(
            "import os\nfrom pathlib import Path\n"
            "(Path(os.environ['TRACEFORGE_WORKSPACE']) / 'entry.py').write_text('changed')"
        ),
        purpose="load",
    )
    assert result["executions"][0]["exit_code"] == 0
    assert result["status"] == "PIPELINE_ERROR"
    assert result["environment_unchanged"] is False
    assert "ENVIRONMENT_PROBE_WORKSPACE_CHANGED" in session.policy_errors


@pytest.mark.parametrize(
    "arguments",
    [
        {"python_code": ""},
        {"python_code": 4},
        {"purpose": "solve"},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 61},
        {"timeout_seconds": "1"},
    ],
)
def test_invalid_arguments_never_execute(tmp_path: Path, arguments: dict) -> None:
    session, runtime = _session(tmp_path)
    args = {"python_code": "print('ok')", "purpose": "load", "timeout_seconds": 2}
    result = run_environment_probe(session, **(args | arguments))
    assert result["status"] == "REJECTED"
    assert result["error_code"] == "ENVIRONMENT_PROBE_INVALID_ARGUMENTS"
    assert runtime.execs == []


@pytest.mark.parametrize("missing", ["sandbox", "authorization", "readonly"])
def test_probe_requires_sandbox_role_authorization_and_protection(
    tmp_path: Path, missing: str
) -> None:
    session, runtime = _session(tmp_path)
    if missing == "sandbox":
        session.sandbox = None
    elif missing == "authorization":
        session.allow_environment_probe = False
    else:
        session.read_only_probe_blocked = None
    result = run_environment_probe(session, python_code="print('never')", purpose="load")
    assert result["status"] in {"REJECTED", "INFRA_ERROR"}
    assert result["environment_unchanged"] is None
    assert runtime.execs == []


def test_remote_exception_remains_infrastructure_error(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)

    class BrokenRuntime:
        async def exec(self, command, *, cwd, timeout_sec, user):
            assert user == "user"
            raise TimeoutError("远端执行连接超时")

    session.sandbox.runtime = BrokenRuntime()
    result = run_environment_probe(session, python_code="print('ok')", purpose="dependency")
    assert result["status"] == "INFRA_ERROR"
    assert result["error_code"] == "ENVIRONMENT_PROBE_RUNTIME_ERROR"
    assert "TimeoutError" in result["runtime_error"]
    assert result["executions"] == []


def test_malformed_remote_receipt_cannot_pass(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)

    class InvalidRuntime:
        async def exec(self, command, *, cwd, timeout_sec, user):
            return SimpleNamespace(return_code=0, stdout='{"executions": []}', stderr="")

    session.sandbox.runtime = InvalidRuntime()
    result = run_environment_probe(session, python_code="print('ok')", purpose="load")
    assert result["status"] == "INFRA_ERROR"
    assert result["error_code"] == "ENVIRONMENT_PROBE_RECEIPT_INVALID"


def test_tool_response_is_bounded_while_session_preserves_complete_output(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)
    text = execute_tool(
        "run_environment_probe",
        {"python_code": "print('x' * 20000)", "purpose": "load", "timeout_seconds": 5},
        session,
    )
    assert len(text) <= 8000
    assert json.loads(text)["truncated"] is True
    result = session.environment_probes[0]
    assert len(result["executions"][0]["stdout"]) == 20001
    assert result["status"] == "PASS"
    schema = tool_schemas(("run_environment_probe",))[0]["function"]["parameters"]
    assert schema["properties"]["timeout_seconds"]["maximum"] == 60


def test_probe_child_cwd_is_workspace_even_when_runtime_starts_at_root(tmp_path: Path) -> None:
    session, _runtime = _session(tmp_path)
    result = run_environment_probe(
        session,
        python_code=(
            "import os\nfrom pathlib import Path\n"
            "assert Path.cwd().resolve() == Path(os.environ['TRACEFORGE_WORKSPACE']).resolve()\n"
            "print(Path('entry.py').read_text(), end='')"
        ),
        purpose="load",
    )
    assert result["status"] == "PASS"
    assert result["executions"][0]["stdout"] == "VALUE = 42\n"


def test_probe_tool_exposes_execution_contract_before_first_call() -> None:
    description = tool_schemas(("run_environment_probe",))[0]["function"]["description"]
    # 首次调用前就给出沙盒坐标、临时写入位置和失败信号，不能等回执再提示。
    assert "TRACEFORGE_WORKSPACE" in description
    assert "TRACEFORGE_PROBE_SCRATCH" in description
    assert "相对路径" in description
    assert "宿主" in description
    assert "assert" in description
