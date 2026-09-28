"""测试重写后仅当前版本参与验收，完整历史诊断仍可按源码摘要追溯。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from traceforge.reconstruction.agents import VERIFIER_ROLE, AgentSession
from traceforge.reconstruction.agents import sandbox as sandbox_tools
from traceforge.reconstruction.agents.runtime import HermesNativeRuntime, write_agent_trace
from traceforge.reconstruction.verifier_recovery import _pytest_red_ok

SOURCE_A = "# 初版测试\n" + "# 原始上下文\n" * 700 + "from unavailable import target\n"
SOURCE_B = (
    "# 修订测试\n" + "# 修订上下文\n" * 700
    + "def test_missing(): assert False\ndef test_protective(): assert True\n"
)
DIAGNOSTIC = "采集前置日志\n" * 900 + "ImportError: cannot import name target from unavailable"


def _digest(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _run_versions(tmp_path, monkeypatch, *, run_b=True, valid_a=False):
    session = AgentSession(sandbox=object(), allow_tests=True)
    written = []
    returned = []

    def write_test(binding, content):
        assert binding is session.sandbox
        written.append(content)
        return "wrote tests/test_outputs.py"

    def run_pytest(binding, names, *, test_sha256):
        assert binding is session.sandbox
        assert test_sha256 == _digest(written[-1])
        first = written[-1] == SOURCE_A
        return [
            {"name": name, "status": (
                "ERROR" if first and not valid_a
                else "FAIL" if name == "test_missing" else "PASS"
            ), "stdout": DIAGNOSTIC if first else "修订测试完成",
             "stderr": "", "test_sha256": test_sha256}
            for name in names
        ]

    monkeypatch.setattr(sandbox_tools, "sandbox_write_test", write_test)
    monkeypatch.setattr(sandbox_tools, "sandbox_run_pytest", run_pytest)

    class VersionAgent:
        def run_conversation(self, instruction, **kwargs):
            for version, source in [("A", SOURCE_A), ("B", SOURCE_B)]:
                result = self._invoke_tool(
                    "write_test", {"content": source}, "verifier",
                    tool_call_id=f"write-{version}",
                )
                assert not result.startswith("error:")
                if version == "B":
                    assert session.pytest_runs == []
                    if not run_b:
                        continue
                returned.append(self._invoke_tool(
                    "run_pytest", {"names": ["test_missing", "test_protective"]},
                    "verifier", tool_call_id=f"pytest-{version}",
                ))
            return {"final_response": '{"status":"READY"}', "completed": True,
                    "messages": [], "api_calls": 1}

    runtime = HermesNativeRuntime(
        factory=lambda **kwargs: VersionAgent(), base_url="https://example.test",
        api_key="unit", model_name="fixture", provider="gpt",
    )
    result = runtime.run(
        role=VERIFIER_ROLE, instruction="构造并修订测试", session=session, output_root=tmp_path,
    )
    assert result.completed and not result.errors
    assert result.payload == {"status": "READY"}
    trace = json.loads((tmp_path / "private/agent_trace.json").read_text(encoding="utf-8"))
    history = [event for event in trace["tool_events"] if event["name"] == "run_pytest"]
    candidate = SimpleNamespace(
        test_outputs_py=SOURCE_B, missing_capability_tests=["test_missing"],
        protective_tests=["test_protective"],
    )
    return session, history, candidate, returned


def test_failed_version_full_diagnostic_survives_rewrite_and_final_gate_uses_b(
    tmp_path, monkeypatch,
):
    session, history, candidate, returned = _run_versions(tmp_path, monkeypatch)
    assert _pytest_red_ok(session.pytest_runs, candidate)
    assert {run["test_sha256"] for run in session.pytest_runs} == {_digest(SOURCE_B)}
    assert [event["tool_call_id"] for event in history] == ["pytest-A", "pytest-B"]
    for event, source, actual_result in zip(history, [SOURCE_A, SOURCE_B], returned, strict=True):
        assert event["test_sha256"] == _digest(source)
        assert event["test_outputs_py"] == source
        assert event["result"] == actual_result
        assert event["result_sha256"] == _digest(actual_result)
        runs = json.loads(event["result"])
        assert {run["test_sha256"] for run in runs} == {_digest(source)}
    assert json.loads(history[0]["result"])[0]["stdout"] == DIAGNOSTIC
    assert "ImportError" not in history[0]["result_preview"]
    assert len(history[0]["result"]) > 4096
    assert len(history[0]["test_outputs_py"]) > 4096


def test_old_valid_red_history_cannot_satisfy_unexecuted_new_version(tmp_path, monkeypatch):
    session, history, candidate, _ = _run_versions(
        tmp_path, monkeypatch, run_b=False, valid_a=True,
    )
    assert session.pytest_runs == []
    assert not _pytest_red_ok(session.pytest_runs, candidate)
    assert len(history) == 1
    assert history[0]["test_sha256"] == _digest(SOURCE_A)
    assert not _pytest_red_ok(json.loads(history[0]["result"]), candidate)


@pytest.mark.parametrize("field", ["result", "test_outputs_py"])
def test_complete_pytest_trace_retains_tail_and_redacts_credentials(tmp_path: Path, field: str):
    text = "审计前文\n" * 900 + "api_key=fixture-secret-value 诊断尾部"
    trace_path = write_agent_trace(
        tmp_path, role=VERIFIER_ROLE, backend="fixture", instruction="审计测试",
        turns=[], final_text='{"status":"READY"}', model_name="fixture",
        tool_events=[{"name": "run_pytest", field: text}],
    )
    saved = json.loads(trace_path.read_text(encoding="utf-8"))["tool_events"][0][field]
    assert saved.endswith("api_key=<redacted> 诊断尾部")
    assert "fixture-secret-value" not in saved
    assert "[truncated]" not in saved
