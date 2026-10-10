"""Hard-rule gates for q↔Ê bindings. These tests do not stand in for Hermes."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from traceforge.reconstruction.agents.runtime import is_fatal_tool_result
from traceforge.reconstruction.agents.session import AgentSession, execute_tool
from traceforge.reconstruction.completion_holes import index_completion_holes, listing_stub_error
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.environment_bindings import (
    collect_allowed_paths,
    collect_file_binding_paths,
    file_obligation_ids,
    file_required_paths,
    missing_binding_paths,
    non_file_obligation_ids,
    normalize_binding_path,
    normalize_environment_bindings,
    path_is_allowed,
    workspace_is_stub_ensemble,
    workspace_task_context,
)
from traceforge.reconstruction.intent_recovery import _gate
from traceforge.reconstruction.terminal_universe_environment import validate_completion_candidate
from traceforge.verifier.synthesis import VerifierSynthesisError, candidate_from_payload


def _timeline() -> list[dict]:
    return [
        {
            "call_id": "c1",
            "name": "read_file",
            "arguments": {"path": "Loader.cpp"},
            "result_text": "int load() { return 1; }\n",
        },
        {
            "call_id": "c2",
            "name": "read_file",
            "arguments": {"path": "Injector.cpp", "offset": 1, "limit": 2},
            "result_text": "int inject() {\n",
        },
        {
            "call_id": "c2b",
            "name": "read_file",
            "arguments": {"path": "modules/API.h", "offset": 1, "limit": 2},
            "result_text": "#pragma once\n",
        },
        {
            "call_id": "c3",
            "name": "exec",
            "arguments": {"command": "cat build.bat"},
            "result_text": "@echo off\n",
        },
        {
            "call_id": "c4",
            "name": "exec",
            "arguments": {"command": "rg RobloxDLL.cpp imgui/"},
            "result_text": "RobloxDLL.cpp\nimgui/\nmodules/API.h\n",
        },
    ]


def _l22_style_task() -> dict:
    return {
        "task_id": "t-l22",
        "task_instruction": "读注入器并评估 2026 是否仍可用",
        "core_objective": "读代码并调研",
        "acceptance_obligations": [
            {
                "id": "obl-001",
                "text": "上网查 2026 反作弊是否仍可用",
                "evidence_ref_ids": ["user:0"],
            },
            {
                "id": "obl-002",
                "text": "完全读取并了解注入器代码",
                "evidence_ref_ids": ["user:0"],
            },
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-001",
                "required_paths": [],
                "observable": "",
                "verifier_kind": "NON_FILE",
            },
            {
                "obligation_id": "obl-002",
                "required_paths": ["Loader.cpp", "Injector.cpp", "modules/", "build.bat"],
                "observable": "代码解释覆盖用户指定的入口逻辑",
                "verifier_kind": "FILE",
            },
        ],
    }


def test_invented_binding_path_is_rejected() -> None:
    allowed = collect_allowed_paths(
        {"tool_timeline": _timeline()},
        [{"id": "user:0", "text": "读注入器并评估 2026"}],
    )
    assert "Injector.cpp" in allowed
    assert "modules/" in allowed
    payload = {
        "task_id": "t-l22",
        "task_instruction": "读注入器",
        "core_objective": "读代码",
        "acceptance_obligations": [
            {
                "id": "obl-002",
                "text": "完全读取代码",
                "evidence_ref_ids": ["user:0"],
            }
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-002",
                "required_paths": ["secret_oracle.cpp"],
                "verifier_kind": "FILE",
            }
        ],
    }
    status, errors, gated = _gate(
        payload,
        {"task_id": "t-l22"},
        {"user:0"},
        allowed_paths=allowed,
        user_blob="读注入器并评估 2026",
    )
    assert status == "REVIEW"
    assert any(item.startswith("BINDING_PATH_NOT_ALLOWED") for item in errors)
    assert "secret_oracle.cpp" not in file_required_paths(gated)


def test_file_binding_does_not_fall_back_to_matching_basename() -> None:
    assert path_is_allowed("src/foo.py", ["src/foo.py"])
    assert not path_is_allowed("foo.py", ["src/foo.py"])


def test_l22_style_bindings_split_file_and_research() -> None:
    task = _l22_style_task()
    allowed = collect_allowed_paths(
        {"tool_timeline": _timeline()},
        [{"id": "user:0", "text": task["task_instruction"]}],
    )
    bindings, errors = normalize_environment_bindings(
        task, task["acceptance_obligations"], allowed
    )
    assert errors == []
    by_id = {item["obligation_id"]: item for item in bindings}
    assert by_id["obl-001"]["verifier_kind"] == "NON_FILE"
    assert by_id["obl-001"]["required_paths"] == []
    assert by_id["obl-002"]["verifier_kind"] == "FILE"
    assert set(by_id["obl-002"]["required_paths"]) >= {
        "Loader.cpp",
        "Injector.cpp",
        "modules/",
        "build.bat",
    }
    assert file_obligation_ids(task) == ["obl-002"]
    assert non_file_obligation_ids(task) == ["obl-001"]


def test_explicit_file_bindings_preserve_observed_names_without_bodies() -> None:
    source = {"tool_timeline": _timeline()}
    allowed = collect_allowed_paths(source, [{"id": "user:0", "text": "读注入器"}])
    bindable = collect_file_binding_paths(source)
    assert "Injector.cpp" in bindable
    assert "RobloxDLL.cpp" in allowed
    assert "RobloxDLL.cpp" not in bindable
    payload = {
        "task_id": "t-l22",
        "task_instruction": "读注入器",
        "core_objective": "读代码",
        "acceptance_obligations": [
            {
                "id": "obl-002",
                "text": "完全读取并了解注入器代码",
                "evidence_ref_ids": ["user:0"],
            }
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-002",
                "required_paths": ["RobloxDLL.cpp", "Injector.cpp"],
                "observable": "解释用户指定代码的执行逻辑",
                "verifier_kind": "FILE",
            }
        ],
    }
    status, errors, gated = _gate(
        payload,
        {"task_id": "t-l22"},
        {"user:0"},
        allowed_paths=allowed,
        user_blob="读注入器",

    )
    assert status == "READY"
    assert errors == []
    assert file_required_paths(gated) == ["RobloxDLL.cpp", "Injector.cpp"]
    valid, missing = validate_completion_candidate(
        {"files": [], "decision": "READY"}, replay_from_timeline(_timeline()), set(),
        required_paths=file_required_paths(gated),
    )
    assert not valid
    assert "BINDING_PATH_MISSING:RobloxDLL.cpp" in missing


def test_listing_real_body_allowed_stub_forbidden() -> None:
    replay = replay_from_timeline(_timeline())
    index = index_completion_holes(replay, _timeline(), _l22_style_task())
    assert "RobloxDLL.cpp" in index.listing_names
    assert (
        listing_stub_error(
            "RobloxDLL.cpp",
            "int main() { return 0; }\n",
            listing_names=index.listing_names,
            body_paths=index.body_paths,
            replay_paths={item.path for item in replay.files},
        )
        is None
    )
    assert listing_stub_error(
        "RobloxDLL.cpp",
        "// observed name, body unobserved\n",
        listing_names=index.listing_names,
        body_paths=index.body_paths,
        replay_paths={item.path for item in replay.files},
    ) == "BINDING_PATH_STUB_ONLY:RobloxDLL.cpp"
    impl = {
        "files": [
            {
                "path": "RobloxDLL.cpp",
                "content": "void Inject() { CreateRemoteThread(0,0,0,0,0,0,0); }\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": ["c4"],
            }
        ],
        "decision": "READY",
    }
    ok, errors = validate_completion_candidate(
        impl,
        replay,
        {"c1", "c2", "c2b", "c3", "c4"},
        listing_names=index.listing_names,
        body_paths=index.body_paths,
        required_paths=file_required_paths(_l22_style_task()),
    )
    assert ok, errors
    stub = {
        "files": [
            {
                "path": "RobloxDLL.cpp",
                "content": "// observed name, body unobserved\n",
                "provenance": "SYNTHETIC_STUB",
                "evidence_ref_ids": ["c4"],
            }
        ],
        "decision": "READY",
    }
    ok_stub, stub_errors = validate_completion_candidate(
        stub,
        replay,
        {"c1", "c2", "c2b", "c3", "c4"},
        listing_names=index.listing_names,
        body_paths=index.body_paths,
        required_paths=file_required_paths(_l22_style_task()),
    )
    assert not ok_stub
    assert any("BINDING_PATH_STUB_ONLY" in item for item in stub_errors)


def test_write_file_rejects_listing_stub_allows_real_body() -> None:
    session = AgentSession(
        allow_write=True,
        listing_names={"RobloxDLL.cpp"},
        evidence=[{"evidence_ref_id": "c4", "name": "exec"}],
    )
    rejected = execute_tool(
        "write_file",
        {
            "path": "RobloxDLL.cpp",
            "content": "// observed name, body unobserved\n",
            "evidence_ref_ids": ["c4"],
        },
        session,
    )
    assert rejected.startswith("error:")
    assert "BINDING_PATH_STUB_ONLY:RobloxDLL.cpp" in rejected
    assert session.writes == []
    accepted = execute_tool(
        "write_file",
        {
            "path": "RobloxDLL.cpp",
            "content": "int inject(void);\n",
            "evidence_ref_ids": ["c4"],
        },
        session,
    )
    assert not accepted.startswith("error:")
    assert session.writes[0]["path"] == "RobloxDLL.cpp"


def test_rejected_partial_write_does_not_wipe_payload() -> None:
    assert (
        is_fatal_tool_result("write_file", "error: PARTIAL_OBSERVED_CONTENT_LOST:README.md")
        is False
    )
    assert (
        is_fatal_tool_result("write_file", "error: BINDING_PATH_STUB_ONLY:RobloxDLL.cpp")
        is False
    )
    assert is_fatal_tool_result("write_file", "error: PROTECTED_FILE_OVERWRITE:build.bat") is False
    assert is_fatal_tool_result("write_file", "error: unsafe path") is False


def test_partial_substring_gate_stays() -> None:
    replay = replay_from_timeline(_timeline())
    candidate = {
        "files": [
            {
                "path": "Injector.cpp",
                "content": "int other() { return 0; }\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": ["c2"],
            }
        ],
        "decision": "READY",
    }
    ok, errors = validate_completion_candidate(candidate, replay, {"c2"})
    assert not ok
    assert "PARTIAL_OBSERVED_CONTENT_LOST:Injector.cpp" in errors


def test_missing_injector_is_insufficient_binding(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "Loader.cpp").write_text("int load() { return 1; }\n", encoding="utf-8")
    (workspace / "modules").mkdir()
    (workspace / "modules" / "API.h").write_text("#pragma once\n", encoding="utf-8")
    (workspace / "build.bat").write_text("@echo off\n", encoding="utf-8")
    missing = missing_binding_paths(workspace, _l22_style_task())
    assert "Injector.cpp" in missing
    (workspace / "Injector.cpp").write_text("int inject() {\n", encoding="utf-8")
    assert missing_binding_paths(workspace, _l22_style_task()) == []


def test_stub_ensemble_is_not_project_specific(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "RobloxDLL.cpp").write_text(
        "// observed name, body unobserved\n", encoding="utf-8"
    )
    assert workspace_is_stub_ensemble(workspace) is True
    (workspace / "Loader.cpp").write_text("int load() { return 1; }\n", encoding="utf-8")
    assert workspace_is_stub_ensemble(workspace) is False


def test_non_file_paths_preserve_required_inputs(tmp_path: Path) -> None:
    allowed = collect_allowed_paths(
        {"tool_timeline": _timeline()},
        [{"id": "user:0", "text": "读注入器并评估 2026；我修了一点你看看"}],
    )
    payload = {
        "task_id": "t-l22",
        "task_instruction": "读注入器并审阅修改",
        "core_objective": "读代码并反馈",
        "acceptance_obligations": [
            {
                "id": "obl-003",
                "text": "审阅用户修改后的代码变更并提供反馈",
                "evidence_ref_ids": ["user:0"],
            }
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-003",
                "required_paths": ["Injector.cpp", "Loader.cpp"],
                "observable": "review feedback on user modifications",
                "verifier_kind": "NON_FILE",
            }
        ],
    }
    status, errors, gated = _gate(
        payload,
        {"task_id": "t-l22"},
        {"user:0"},
        allowed_paths=allowed,
        user_blob="读注入器并评估 2026；我修了一点你看看",
    )
    assert status == "READY"
    assert errors == []
    assert gated["environment_bindings"][0]["verifier_kind"] == "NON_FILE"
    assert gated["environment_bindings"][0]["required_paths"] == ["Injector.cpp", "Loader.cpp"]

    assert file_obligation_ids(gated) == []
    assert non_file_obligation_ids(gated) == ["obl-003"]
    assert file_required_paths(gated) == ["Injector.cpp", "Loader.cpp"]
    assert workspace_task_context(gated)["initial_required_paths"] == ["Injector.cpp", "Loader.cpp"]
    assert missing_binding_paths(tmp_path, gated) == ["Injector.cpp", "Loader.cpp"]
    (tmp_path / "Injector.cpp").write_text("int inject();\n", encoding="utf-8")
    assert missing_binding_paths(tmp_path, gated) == ["Loader.cpp"]


def test_non_file_coverage_is_not_required() -> None:
    task = _l22_style_task()
    assert "obl-001" not in file_obligation_ids(task)
    assert "obl-001" in non_file_obligation_ids(task)
    assert file_obligation_ids(task) == ["obl-002"]


def test_existence_only_missing_and_oracle_injector_are_rejected() -> None:
    common = {
        "status": "READY",
        "protective_tests": ["test_protective"],
        "obligation_coverage": {"obl-002": ["test_protective"]},
        "expected_value_strategy": "independent",
        "open_questions": [],
        "mutation_solutions": [{"name": "mut", "script": "echo m", "justification": "m"}],
    }
    with pytest.raises(VerifierSynthesisError, match="EXISTENCE_ONLY_MISSING"):
        candidate_from_payload(
            {
                **common,
                "test_outputs_py": (
                    "from pathlib import Path\n"
                    "import os\n"
                    "def test_missing():\n"
                    "    root = Path(os.environ['TRACEFORGE_WORKSPACE'])\n"
                    "    assert (root / 'Injector.cpp').is_file()\n"
                    "def test_protective():\n"
                    "    assert True\n"
                ),
                "missing_capability_tests": ["test_missing"],
                "oracle_solutions": [
                    {"name": "oracle-a", "script": "echo a", "justification": "a"},
                    {"name": "oracle-b", "script": "echo b", "justification": "b"},
                ],
            },
            obligation_ids=["obl-002"],
            model_name="t",
            prompt_sha256="p",
            response_sha256="r",
        )
    with pytest.raises(VerifierSynthesisError, match="ORACLE_WRITES_INJECTOR"):
        candidate_from_payload(
            {
                **common,
                "test_outputs_py": (
                    "def test_missing():\n"
                    "    assert False\n"
                    "def test_protective():\n"
                    "    assert True\n"
                ),
                "missing_capability_tests": ["test_missing"],
                "oracle_solutions": [
                    {
                        "name": "oracle-a",
                        "script": "python3 -c \"from pathlib import Path; Path('Injector.cpp').write_text('int main(){return 0;}')\"",
                        "justification": "write injector",
                    },
                    {"name": "oracle-b", "script": "echo review", "justification": "b"},
                ],
            },
            obligation_ids=["obl-002"],
            model_name="t",
            prompt_sha256="p",
            response_sha256="r",
        )


def _ready_verifier_payload(**overrides: object) -> dict:
    payload = {
        "status": "READY",
        "test_outputs_py": (
            "def test_missing():\n"
            "    assert False\n"
            "def test_protective():\n"
            "    assert True\n"
        ),
        "protective_tests": ["test_protective"],
        "missing_capability_tests": ["test_missing"],
        "obligation_coverage": {"obl-002": ["test_protective"]},
        "expected_value_strategy": "independent",
        "open_questions": [],
        "mutation_solutions": [{"name": "mut", "script": "echo m", "justification": "m"}],
        "oracle_solutions": [
            {"name": "oracle-a", "script": "echo a", "justification": "a"},
            {"name": "oracle-b", "script": "echo b", "justification": "b"},
        ],
    }
    payload.update(overrides)
    return payload


def test_review_oracle_may_mention_injector_apis() -> None:
    candidate, extra = candidate_from_payload(
        _ready_verifier_payload(
            oracle_solutions=[
                {
                    "name": "oracle-review",
                    "script": (
                        "#!/usr/bin/env python3\n"
                        "import os, pathlib\n"
                        "ws = pathlib.Path(os.environ.get('TRACEFORGE_WORKSPACE', '.'))\n"
                        "review = '# Analysis\\nInjector.cpp uses CreateRemoteThread "
                        "and VirtualAllocEx\\n'\n"
                        "(ws / 'review_analysis.md').write_text(review)\n"
                    ),
                    "justification": "write review artifact",
                },
                {"name": "oracle-b", "script": "echo review", "justification": "b"},
            ]
        ),
        obligation_ids=["obl-002"],
        model_name="t",
        prompt_sha256="p",
        response_sha256="r",
    )
    assert extra["status"] == "UNVALIDATED"
    assert candidate is not None
    assert candidate.oracle_solutions[0].name == "oracle-review"


def test_shell_redirect_to_injector_source_is_rejected() -> None:
    for script in (
        "cat > Loader.cpp <<'EOF'\nint main(){return 0;}\nEOF\n",
        "Set-Content -Path RobloxDLL.cpp 'int main(){return 0;}'",
    ):
        with pytest.raises(VerifierSynthesisError, match="ORACLE_WRITES_INJECTOR"):
            candidate_from_payload(
                _ready_verifier_payload(
                    oracle_solutions=[
                        {"name": "oracle-a", "script": script, "justification": "write injector"},
                        {"name": "oracle-b", "script": "echo review", "justification": "b"},
                    ]
                ),
                obligation_ids=["obl-002"],
                model_name="t",
                prompt_sha256="p",
                response_sha256="r",
            )


def test_oracle_gate_does_not_stack_sandbox_red_when_pytest_already_ran(tmp_path: Path) -> None:
    from traceforge.reconstruction.agents.runtime import AgentResult
    from traceforge.reconstruction.verifier_recovery import run_verifier_recovery

    tests = (
        "def test_missing():\n"
        "    assert False\n"
        "def test_protective():\n"
        "    assert True\n"
    )
    digest = hashlib.sha256(tests.encode("utf-8")).hexdigest()
    payload = _ready_verifier_payload(
        test_outputs_py=tests,
        oracle_solutions=[
            {
                "name": "oracle-a",
                "script": "Path('Injector.cpp').write_text('int main(){return 0;}')",
                "justification": "write injector",
            },
            {"name": "oracle-b", "script": "echo review", "justification": "b"},
        ],
    )

    class Runtime:
        backend = "hermes-sandbox"
        model_name = "t"

        def run(self, **kwargs):
            session = kwargs["session"]
            session.sandbox = object()
            session.test_outputs_py = tests
            session.pytest_runs.extend(
                [
                    {"name": "test_missing", "status": "FAIL", "test_sha256": digest},
                    {"name": "test_protective", "status": "PASS", "test_sha256": digest},
                ]
            )
            return AgentResult(
                role="verifier",
                backend=self.backend,
                payload=payload,
                completed=True,
            )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "Injector.cpp").write_text("int inject() {\n", encoding="utf-8")
    recovered, candidate = run_verifier_recovery(
        task=_l22_style_task(),
        workspace_root=workspace,
        agent=Runtime(),
        output_root=tmp_path / "verifier",
    )
    assert candidate is None
    assert recovered["status"] == "REVIEW"
    assert any(str(item).startswith("ORACLE_WRITES_INJECTOR") for item in recovered["errors"])
    assert "SANDBOX_PYTEST_RED_REQUIRED" not in recovered["errors"]


def test_all_non_file_obligations_skip_verifier(tmp_path: Path) -> None:
    from traceforge.reconstruction.agents.runtime import AgentResult
    from traceforge.reconstruction.verifier_recovery import run_verifier_recovery

    class UnusedRuntime:
        backend = "hermes-sandbox"
        model_name = "unused"
        calls = 0

        def run(self, **kwargs):
            del kwargs
            self.calls += 1
            return AgentResult(role="verifier", backend=self.backend, payload={}, completed=True)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("print(1)\n", encoding="utf-8")
    runtime = UnusedRuntime()
    recovered, candidate = run_verifier_recovery(
        task={
            "acceptance_obligations": [
                {"id": "obl-001", "text": "调研 2026 反作弊", "evidence_ref_ids": ["user:0"]}
            ],
            "environment_bindings": [
                {
                    "obligation_id": "obl-001",
                    "required_paths": [],
                    "observable": "",
                    "verifier_kind": "NON_FILE",
                }
            ],
        },
        workspace_root=workspace,
        agent=runtime,
        output_root=tmp_path / "verifier",
    )
    assert recovered["errors"] == ["NON_FILE_TASK"]
    assert recovered["unverified_obligations"] == ["obl-001"]
    assert candidate is None
    assert runtime.calls == 0


def test_binding_basename_is_not_path_identity():
    from traceforge.reconstruction.environment_bindings import path_is_allowed
    assert path_is_allowed("src/foo.py", {"src/foo.py"})
    assert not path_is_allowed("invented/foo.py", {"src/foo.py"})
    assert not path_is_allowed("foo.py", {"src/foo.py"})



def test_file_binding_rejects_initial_presence_as_task_success():
    from traceforge.reconstruction.environment_bindings import normalize_environment_bindings
    for observable in ["", "replayed excerpts still present"]:
        bindings, errors = normalize_environment_bindings(
            {"environment_bindings": [{"obligation_id": "o1", "verifier_kind": "FILE", "required_paths": ["foo.py"], "observable": observable}]},
            [{"id": "o1", "text": "修复负数输入的异常"}], ["foo.py"],
        )
        assert "BINDING_TASK_OUTCOME_REQUIRED:o1" in errors


def test_file_binding_accepts_task_specific_outcome():
    from traceforge.reconstruction.environment_bindings import normalize_environment_bindings
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{"obligation_id": "o1", "verifier_kind": "FILE", "required_paths": ["foo.py"], "observable": "对负数输入返回零，保留正数输入行为"}]},
        [{"id": "o1", "text": "修复负数输入的异常"}], ["foo.py"],
    )
    assert errors == []
    assert bindings[0]["observable"] == "对负数输入返回零，保留正数输入行为"


def test_normalize_binding_path_drops_prose_trailing_punctuation() -> None:
    assert normalize_binding_path("twitter-api-client-main/twitter/util.py;") == (
        "twitter-api-client-main/twitter/util.py"
    )
    assert normalize_binding_path("main.py,") == "main.py"


def test_output_file_binding_does_not_require_initial_workspace() -> None:
    obligations = [{"id": "o1", "text": "写入 .pi-subagents/report.md", "evidence_ref_ids": ["user:0"]}]
    payload = {
        "environment_bindings": [
            {
                "obligation_id": "o1",
                "required_paths": [".pi-subagents/report.md"],
                "initial_required_paths": [],
                "output_paths": [".pi-subagents/report.md"],
                "observable": "报告文件生成并包含审查结论",
                "verifier_kind": "FILE",
            }
        ]
    }
    bindings, errors = normalize_environment_bindings(
        payload,
        obligations,
        [".pi-subagents/report.md"],
        user_blob="写入 .pi-subagents/report.md",
    )
    assert errors == []
    assert bindings[0]["required_paths"] == [".pi-subagents/report.md"]
    assert bindings[0]["initial_required_paths"] == []
    assert bindings[0]["output_paths"] == [".pi-subagents/report.md"]
    task = {"environment_bindings": bindings}
    assert file_required_paths(task) == []


def test_missing_initial_input_is_not_reclassified_as_output() -> None:
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "o1", "verifier_kind": "FILE",
            "required_paths": ["missing.py"], "initial_required_paths": ["missing.py"],
            "output_paths": [], "observable": "缺失的必要源码已修复",
        }]},
        [{"id": "o1", "text": "修复 missing.py"}], ["missing.py"],
        user_blob="修复 missing.py",
    )
    assert errors == []
    assert bindings[0]["initial_required_paths"] == ["missing.py"]
    assert bindings[0]["output_paths"] == []


def test_mixed_required_and_explicit_output_paths_are_preserved() -> None:
    obligations = [{"id": "o1", "text": "更新 src/foo.py 并生成 report.md", "evidence_ref_ids": ["user:0"]}]
    payload = {
        "environment_bindings": [{
            "obligation_id": "o1",
            "required_paths": ["src/foo.py", "report.md"],
            "output_paths": ["report.md"],
            "observable": "报告生成",
            "verifier_kind": "FILE",
        }]
    }
    bindings, errors = normalize_environment_bindings(
        payload, obligations, ["src/foo.py", "report.md"],
        user_blob="更新 src/foo.py 并生成 report.md",
    )
    assert errors == []
    assert bindings[0]["required_paths"] == ["src/foo.py", "report.md"]
    assert bindings[0]["initial_required_paths"] == ["src/foo.py"]
    assert bindings[0]["output_paths"] == ["report.md"]


def test_review_contract_examples_are_not_workspace_evidence() -> None:
    """真实只读审查合同中的分类词和 JSON 格式示例不能变成环境输入。"""
    request = (
        "Read `brief.md` and inspect `src/core.rs`. Review panic/cancellation/shutdown "
        "behavior and lock/lifetime safety. Return Critical/Important/Minor findings. "
        "Write the full report to `reports/review.md`.\n"
        "Finish with a fenced JSON block in this shape:\n"
        "```acceptance-report\n"
        '{"changedFiles": ["src/file.ts"], "testsAdded": ["test/file.test.ts"]}\n'
        "```\n"
    )
    records = [
        {"id": "user:1", "text": "Platform instructions: load references/tools.md and AGENTS.md"},
        {"id": "user:2", "text": request},
    ]
    source = {"tool_timeline": [{
        "name": "read_file", "arguments": {"path": "src/core.rs"},
        "result_text": '// Example: src/from_comment.ts\nfn run() {}\n',
    }]}
    allowed = collect_allowed_paths(source, records)
    assert {"brief.md", "src/core.rs", "reports/review.md"} <= set(allowed)
    assert not ({"Critical/", "Critical/Important/", "panic/", "panic/cancellation/",
                 "lock/", "src/file.ts", "test/file.test.ts", "src/from_comment.ts"}
                & set(allowed))
    obligations = [{"id": "review", "text": "Write reports/review.md with findings",
                    "evidence_ref_ids": ["user:2"]}]
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{"obligation_id": "review", "verifier_kind": "FILE",
                                    "required_paths": [
                                        "brief.md", "src/core.rs", "reports/review.md",
                                    ],
                                    "initial_required_paths": ["brief.md", "src/core.rs"],
                                    "output_paths": ["reports/review.md"],
                                    "observable": "报告提供准确的审查发现"}]},
        obligations, allowed, user_blob="\n".join(row["text"] for row in records),
        user_records=records,
    )
    assert errors == []
    assert set(bindings[0]["initial_required_paths"]) == {"brief.md", "src/core.rs"}
    assert bindings[0]["output_paths"] == ["reports/review.md"]
    assert "AGENTS.md" not in bindings[0]["required_paths"]
    assert "references/tools.md" not in bindings[0]["required_paths"]


def test_real_directories_are_kept_without_promoting_parent_prefixes() -> None:
    request = "Inspect `src/` and assets/. Create reports/result.md."
    allowed = collect_allowed_paths({}, [{"id": "user:0", "text": request}])
    assert {"src/", "assets/", "reports/result.md"} <= set(allowed)
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "o1", "verifier_kind": "FILE",
            "required_paths": ["src/", "assets/", "reports/result.md"],
            "initial_required_paths": ["src/", "assets/"],
            "output_paths": ["reports/result.md"], "observable": "报告覆盖指定目录",
        }]}, [{"id": "o1", "text": request}], allowed, user_blob=request,
    )
    assert errors == []
    binding = bindings[0]
    assert set(binding["initial_required_paths"]) == {"src/", "assets/"}
    assert binding["output_paths"] == ["reports/result.md"]
    assert "reports/" not in binding["required_paths"]


def test_new_test_file_is_an_output_while_explicit_missing_input_stays_input() -> None:
    request = "Modify src/parser.py and add tests/test_parser.py."
    allowed = collect_allowed_paths({}, [{"id": "user:0", "text": request}])
    obligations = [{"id": "o1", "text": request, "evidence_ref_ids": ["user:0"]}]
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{"obligation_id": "o1", "verifier_kind": "FILE",
                                    "required_paths": ["src/parser.py", "tests/test_parser.py"],
                                    "initial_required_paths": ["src/parser.py"],
                                    "output_paths": ["tests/test_parser.py"],
                                    "observable": "解析器修复且新测试覆盖回归"}]},
        obligations, allowed, user_blob=request,
    )
    assert errors == []
    assert bindings[0]["initial_required_paths"] == ["src/parser.py"]
    assert bindings[0]["output_paths"] == ["tests/test_parser.py"]


def test_inline_example_is_context_but_observed_same_path_remains_bindable() -> None:
    request = "Inspect src/real.py; output examples, e.g. src/example.py:12."
    records = [{"id": "user:0", "text": request}]
    assert "src/example.py" not in collect_allowed_paths({}, records)
    source = {"tool_timeline": [{"name": "read_file",
                                "arguments": {"path": "src/example.py"},
                                "result_text": "def run(): return 1\n"}]}
    assert "src/example.py" in collect_allowed_paths(source, records)


def test_listing_path_column_is_allowed_but_matched_source_text_is_not() -> None:
    allowed = collect_allowed_paths({"tool_timeline": [{
        "name": "exec", "arguments": {"command": "rg example src/"},
        "result_text": 'src/real.py:12:# example src/fiction.py\n',
    }]})
    assert {"src/", "src/real.py"} <= set(allowed)
    assert "src/fiction.py" not in allowed


def test_host_and_user_relative_paths_share_replay_coordinates() -> None:
    from traceforge.reconstruction.environment_bindings import collect_binding_path_aliases

    root = "C:/Users/user/project/lua/demo"
    timeline = [
        {"call_id": "brief", "name": "read", "arguments": {"path": root + "/.agent/brief.md"},
         "result_text": "Review the migration\n"},
        {"call_id": "code", "name": "read", "arguments": {"path": root + "/src/core.rs"},
         "result_text": "fn run() {}\n"},
        {"call_id": "find", "name": "find", "arguments": {"path": root, "pattern": "plan.md"},
         "result_text": "No files found"},
    ]
    request = (f"Read from {root}/plan.md, {root}/progress.md. "
               "Read `.agent/brief.md`; write `reports/review.md`.")
    records = [{"id": "user:2", "text": request}]
    source = {"tool_timeline": timeline}
    aliases = collect_binding_path_aliases(source, records)
    allowed = collect_allowed_paths(source, records)
    assert aliases[".agent/brief.md"] == "demo/.agent/brief.md"
    assert aliases["reports/review.md"] == "demo/reports/review.md"
    assert {"demo/plan.md", "demo/progress.md", "demo/.agent/brief.md",
            "demo/reports/review.md"} <= set(allowed)
    assert "Users/user/project/lua/demo/plan.md" not in allowed
    host_paths = ["Users/user/project/lua/demo/" + path
                  for path in ("plan.md", "progress.md", ".agent/brief.md", "reports/review.md")]
    obligations = [{"id": "review", "text": "Write review report",
                    "evidence_ref_ids": ["user:2"]}]
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{"obligation_id": "review", "verifier_kind": "FILE",
                                    "required_paths": host_paths, "output_paths": [host_paths[-1]],
                                    "observable": "报告准确描述审查结论"}]},
        obligations, allowed, user_records=records, path_aliases=aliases,
    )
    assert errors == []
    assert set(bindings[0]["initial_required_paths"]) == {
        "demo/plan.md", "demo/progress.md", "demo/.agent/brief.md"}
    assert bindings[0]["output_paths"] == ["demo/reports/review.md"]


def test_path_aliases_do_not_guess_from_basename_or_conflicting_anchors() -> None:
    from traceforge.reconstruction.environment_bindings import collect_binding_path_aliases

    source = {"tool_timeline": [
        {"call_id": "a", "name": "read", "arguments": {"path": "/home/u/project/one/src/main.py"},
         "result_text": "print(1)\n"},
        {"call_id": "b", "name": "read", "arguments": {"path": "/home/u/project/two/src/main.py"},
         "result_text": "print(2)\n"},
    ]}
    records = [{"id": "user:1", "text": "Read /home/u/project/one/a.md and /home/u/project/one/b.md; modify main.py."},
               {"id": "user:2", "text": "Read /home/u/project/two/a.md and /home/u/project/two/b.md; modify main.py."}]
    aliases = collect_binding_path_aliases(source, records)
    assert "main.py" not in aliases
    assert not path_is_allowed("main.py", collect_file_binding_paths(source))


def test_path_mapping_preserves_non_path_escape_sequences() -> None:
    observable = r"修复 src/parser.py，使正则 \d+ 匹配数字并正确处理 \n"
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{"obligation_id": "o1", "verifier_kind": "FILE",
                                    "required_paths": ["src/parser.py"],
                                    "observable": observable}]},
        [{"id": "o1", "text": "修复 src/parser.py"}], ["repo/src/parser.py"],
        user_blob="修复 src/parser.py",
        path_aliases={"src/parser.py": "repo/src/parser.py"},
    )
    assert errors == []
    assert bindings[0]["observable"] == observable.replace("src/parser.py", "repo/src/parser.py")


@pytest.mark.parametrize("prefix", [
    "The response schema is specified elsewhere.\nRead the input below:\n",
    "Read src/schema.py:\n",
])
def test_unrelated_schema_text_does_not_hide_real_fenced_input(prefix: str) -> None:
    request = prefix + "```bash\ncat src/input.py\n```"
    allowed = collect_allowed_paths({}, [{"id": "user:1", "text": request}])
    assert "src/input.py" in allowed


def test_explicit_absolute_missing_input_survives_without_replay_root() -> None:
    request = r"Read C:\work\app\missing.md."
    allowed = collect_allowed_paths({}, [{"id": "user:1", "text": request}])
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{"obligation_id": "o1", "verifier_kind": "FILE",
                                   "required_paths": ["work/app/missing.md"],
                                   "observable": "输入已审查"}]},
        [{"id": "o1", "text": "审查缺失输入", "evidence_ref_ids": ["user:1"]}],
        allowed, user_records=[{"id": "user:1", "text": request}],
    )
    assert errors == []
    assert bindings[0]["initial_required_paths"] == ["work/app/missing.md"]


def test_existing_replay_coordinate_is_not_prefixed_again() -> None:
    from traceforge.reconstruction.environment_bindings import collect_binding_path_aliases

    source = {"tool_timeline": [{
        "call_id": "read", "name": "read",
        "arguments": {"path": "C:/work/app/src/core.py", "cwd": "C:/work"},
        "result_text": "print(1)",
    }]}
    records = [{"id": "user:1", "text":
                "Read C:/work/app/plan.md and C:/work/app/progress.md and app/src/core.py. "
                "Write app/reports/review.md."}]
    aliases = collect_binding_path_aliases(source, records)
    allowed = collect_allowed_paths(source, records, path_aliases=aliases)
    assert "app/src/core.py" not in aliases
    assert "app/reports/review.md" not in aliases
    assert "app/src/core.py" in allowed
    assert "app/app/src/core.py" not in allowed
    assert "app/app/reports/review.md" not in allowed


def test_explicit_binding_keeps_output_and_observable() -> None:
    request = "Read missing.py. Write reports/review.md."
    allowed = collect_allowed_paths({}, [{"id": "user:1", "text": request}])
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{"obligation_id": "o1", "verifier_kind": "FILE",
                                   "required_paths": ["missing.py", "reports/review.md"],
                                   "initial_required_paths": ["missing.py"],
                                   "output_paths": ["reports/review.md"],
                                   "observable": "报告包含逐行证据"}]},
        [{"id": "o1", "text": "审查后报告", "evidence_ref_ids": ["user:1"]}],
        allowed, user_records=[{"id": "user:1", "text": request}],
    )
    assert errors == []
    assert bindings[0]["initial_required_paths"] == ["missing.py"]
    assert bindings[0]["output_paths"] == ["reports/review.md"]
    assert bindings[0]["observable"] == "报告包含逐行证据"


def test_undeclared_output_is_reported_instead_of_becoming_input() -> None:
    request = "Read src/input.py."
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{"obligation_id": "o1", "verifier_kind": "FILE",
                                   "required_paths": ["src/input.py", "history/report.md"],
                                   "output_paths": ["history/report.md"],
                                   "observable": "输入已审查"}]},
        [{"id": "o1", "text": "审查", "evidence_ref_ids": ["user:1"]}],
        ["src/input.py", "history/report.md"], user_records=[{"id": "user:1", "text": request}],
    )
    assert "BINDING_OUTPUT_PATH_NOT_EXPLICIT:o1:history/report.md" in errors
    assert bindings[0]["output_paths"] == ["history/report.md"]


def test_harness_closing_tags_are_not_path_aliases_and_real_root_remains() -> None:
    from traceforge.reconstruction.environment_bindings import collect_binding_path_aliases

    source = {"tool_timeline": [{
        "call_id": "read", "name": "read_file",
        "arguments": {"path": "C:/work/repo/src/main.py", "cwd": "C:/work/repo"},
        "result_text": "print(1)\n",
    }]}
    plain = "C:/work/repo /root C:/work/repo/src/main.py"
    wrapped = (
        "<environment_context><cwd>C:/work/repo</cwd>"
        "<filesystem><root>/root</root></filesystem></environment_context>\n"
        "修改 C:/work/repo/src/main.py"
    )
    records = [{"id": "user:1", "text": wrapped}]
    aliases = collect_binding_path_aliases(source, records)
    assert aliases == collect_binding_path_aliases(source, [{"id": "user:1", "text": plain}])
    assert aliases["/root"] == "root"
    assert aliases["C:/work/repo/src/main.py"] == "src/main.py"
    assert records[0]["text"] == wrapped


def test_explicit_binding_survives_missing_replay_body_and_error_log_tokens() -> None:
    paths = ["apps/web/components/share.tsx", "apps/web/app/share/page.tsx"]
    request = (
        "修复渲染错误。可能涉及 Date.now() 或 Math.random()。\n"
        "at Share (components/share.tsx:69:13)\n"
        "at Page (app/share/page.tsx:19:9)\n"
        'className="gap-1.5"; Next.js version: 15.5.20'
    )
    records = [{"id": "user:0", "text": request}]
    source = {"tool_timeline": [
        {"name": "read_file", "arguments": {"path": path}, "result_text": "原始片段"}
        for path in paths
    ]}
    replay_paths = ["apps/web/components/other.tsx"]
    allowed = collect_allowed_paths(source, records, replay_files=replay_paths)
    assert "Date.now" in allowed
    payload = {
        "task_id": "render-task", "task_instruction": "修复渲染错误",
        "core_objective": "恢复服务端与客户端渲染一致性",
        "acceptance_obligations": [{
            "id": "o1", "text": "渲染一致", "evidence_ref_ids": ["user:0"],
        }],
        "environment_bindings": [{
            "obligation_id": "o1", "verifier_kind": "FILE", "required_paths": paths,
            "observable": "真实渲染验证不再出现不一致",
        }],
    }
    status, errors, task = _gate(
        payload, {"task_id": "render-task"}, {"user:0"}, allowed_paths=allowed,
        user_blob=request, user_records=records,

    )
    assert status == "READY", errors
    assert file_required_paths(task) == paths
    valid, missing = validate_completion_candidate(
        {"files": [], "decision": "READY"}, replay_from_timeline([]), set(),
        required_paths=file_required_paths(task),
    )
    assert not valid
    assert set(missing) == {f"BINDING_PATH_MISSING:{path}" for path in paths}


def test_explicit_directory_output_is_not_expanded_from_repository_guidance() -> None:
    """真实计划目录失败的最小复现，不包含私人源码。"""
    request = (
        "在 `.claude/plans/` 下创建计划文件，文件名由实现者选择。\n"
        "后端开发说明：新增配置写入 backend/dna.xml。"
    )
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "plan", "verifier_kind": "FILE",
            "required_paths": [".claude/plans/"], "initial_required_paths": [],
            "output_paths": [".claude/plans/"], "observable": "计划文件包含步骤和进度",
        }]},
        [{"id": "plan", "text": "创建任务计划", "evidence_ref_ids": ["user:1"]}],
        [".claude/plans/", "backend/dna.xml"],
        user_records=[{"id": "user:1", "text": request}],
    )
    assert errors == []
    assert bindings[0]["required_paths"] == [".claude/plans/"]
    assert bindings[0]["initial_required_paths"] == []
    assert bindings[0]["output_paths"] == [".claude/plans/"]


def test_empty_file_binding_is_not_filled_from_cited_repository_guidance() -> None:
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "plan", "verifier_kind": "FILE",
            "required_paths": [], "initial_required_paths": [], "output_paths": [],
            "observable": "计划文件存在；具体文件名由实现者选择",
        }]},
        [{"id": "plan", "text": "创建任务计划", "evidence_ref_ids": ["user:1"]}],
        [".claude/plans/", "backend/dna.xml"],
        user_records=[{"id": "user:1", "text":
                       "在 .claude/plans/ 下创建计划。新增配置写入 backend/dna.xml。"}],
    )
    assert errors == ["BINDING_FILE_PATHS_REQUIRED:plan"]
    assert bindings[0]["required_paths"] == []
    assert bindings[0]["output_paths"] == []


def test_declared_initial_input_is_not_changed_by_nearby_output_verb() -> None:
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "read", "verifier_kind": "FILE",
            "required_paths": ["src/input.py"], "initial_required_paths": ["src/input.py"],
            "output_paths": [], "observable": "说明准确覆盖原代码行为",
        }]},
        [{"id": "read", "text": "解释代码", "evidence_ref_ids": ["user:1"]}],
        ["src/input.py"],
        user_records=[{"id": "user:1", "text": "生成关于 src/input.py 的行为说明。"}],
    )
    assert errors == []
    assert bindings[0]["initial_required_paths"] == ["src/input.py"]
    assert bindings[0]["output_paths"] == []


@pytest.mark.parametrize(("initial", "required", "error"), [
    (["report.md"], ["report.md"], "BINDING_PATH_ROLE_CONFLICT:report:report.md"),
    (["src/input.py"], ["report.md"], "BINDING_PATH_UNION_MISMATCH:report"),
])
def test_explicit_path_roles_must_be_consistent(initial, required, error) -> None:
    _, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "report", "verifier_kind": "FILE",
            "required_paths": required, "initial_required_paths": initial,
            "output_paths": ["report.md"], "observable": "报告正确",
        }]},
        [{"id": "report", "text": "报告结果", "evidence_ref_ids": ["user:1"]}],
        ["src/input.py", "report.md"],
        user_records=[{"id": "user:1", "text": "读取 src/input.py，在 report.md 中写报告。"}],
    )
    assert error in errors


def test_missing_binding_is_not_inferred_from_request_paths() -> None:
    bindings, errors = normalize_environment_bindings(
        {}, [{"id": "read", "text": "读取 src/input.py", "evidence_ref_ids": ["user:1"]}],
        ["src/input.py"], user_records=[{"id": "user:1", "text": "读取 src/input.py"}],
    )
    assert bindings == []
    assert errors == ["BINDING_REQUIRED:read"]


def test_historical_assistant_filename_cannot_become_required_output() -> None:
    _, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "plan", "verifier_kind": "FILE",
            "required_paths": [".claude/plans/old-choice.md"], "initial_required_paths": [],
            "output_paths": [".claude/plans/old-choice.md"], "observable": "计划已更新",
        }]},
        [{"id": "plan", "text": "创建计划", "evidence_ref_ids": ["user:1"]}],
        [".claude/plans/", ".claude/plans/old-choice.md"],
        user_records=[{"id": "user:1", "text": "在 .claude/plans/ 下创建计划，文件名自行选择。"}],
    )
    assert "BINDING_OUTPUT_PATH_NOT_EXPLICIT:plan:.claude/plans/old-choice.md" in errors


@pytest.mark.parametrize(("path", "user_text"), [
    ("审计报告.md", "请将结论写入'审计报告.md'。"),
    ("Dockerfile", "创建 Dockerfile"),
    ("final report.md", "输出到 'final report.md'。"),
    ("report.md", "请保存为report.md。"),
    ("docs/", "在 docs 中创建报告，文件名自行选择。"),
    ("report.md", "强制格式如下：report.md"),
])
def test_explicit_source_paths_do_not_depend_on_filename_extraction(path, user_text) -> None:
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "o1", "verifier_kind": "FILE",
            "required_paths": [path], "initial_required_paths": [], "output_paths": [path],
            "observable": "产物包含用户要求的结果",
        }]},
        [{"id": "o1", "text": "输出结果", "evidence_ref_ids": ["user:1"]}],
        [], user_records=[{"id": "user:1", "text": user_text}],
    )
    assert errors == []
    assert bindings[0]["output_paths"] == [path]


@pytest.mark.parametrize("user_text", [
    "写入 src/report.md", "写入 report.md.bak", "写入 oldreport.md",
    "写入 report.md/child.txt", "写入 /report.md", "写入 C:/report.md",
])
def test_output_source_rejects_basename_and_path_substrings(user_text) -> None:
    _, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "o1", "verifier_kind": "FILE",
            "required_paths": ["report.md"], "initial_required_paths": [],
            "output_paths": ["report.md"], "observable": "报告已生成",
        }]},
        [{"id": "o1", "text": "输出报告", "evidence_ref_ids": ["user:1"]}],
        ["report.md"], user_records=[{"id": "user:1", "text": user_text}],
    )
    assert "BINDING_OUTPUT_PATH_NOT_EXPLICIT:o1:report.md" in errors


@pytest.mark.parametrize("path", [
    "/home/unobserved/report.md", "/Users/unobserved/report.md", "../report.md",
])
def test_model_path_cannot_invent_absolute_coordinates(path) -> None:
    _, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "o1", "verifier_kind": "FILE",
            "required_paths": [path], "initial_required_paths": [], "output_paths": [path],
            "observable": "报告已生成",
        }]},
        [{"id": "o1", "text": "输出报告", "evidence_ref_ids": ["user:1"]}],
        ["report.md"], user_records=[{"id": "user:1", "text": "Write report.md"}],
    )
    assert f"BINDING_PATH_UNSAFE:o1:{path}" in errors


@pytest.mark.parametrize("source_role", ["system", "developer"])
def test_original_system_output_reaches_formal_intent_gate(tmp_path, source_role) -> None:
    from traceforge.reconstruction.agents.runtime import AgentResult
    from traceforge.reconstruction.intent_recovery import run_intent_recovery

    class IntentFixture:
        backend = "fixture"
        model_name = "fixture"

        def run(self, *, role, instruction, session, output_root):
            assert "C:/work/repo/reports/result.md" in instruction
            return AgentResult(
                role=role.name, backend=self.backend, completed=True, payload={
                    "task_id": "t1", "task_instruction": "完成代码审查，按原系统约定保存报告",
                    "core_objective": "审查代码并输出报告",
                    "acceptance_obligations": [{
                        "id": "review", "text": "审查代码", "evidence_ref_ids": ["user:1"],
                    }],
                    "environment_bindings": [{
                        "obligation_id": "review", "verifier_kind": "FILE",
                        "required_paths": ["src/core.py", "reports/result.md"],
                        "initial_required_paths": ["src/core.py"],
                        "output_paths": ["reports/result.md"], "observable": "报告覆盖代码问题",
                    }],
                },
            )

    result = run_intent_recovery(
        source={
            "tasks": [{"task_id": "t1", "message_indices": [1]}],
            "raw_session": {"messages": [
                {"role": source_role, "content": "审查报告必须写入 C:/work/repo/reports/result.md"},
                {"role": "user", "content": "审查 src/core.py"},
                {"role": "assistant", "content": "我选择 reports/history.md 作为报告名"},
            ]},
            "tool_timeline": [{
                "name": "read_file", "arguments": {
                    "path": "C:/work/repo/src/core.py", "cwd": "C:/work/repo",
                }, "result_text": "print(1)\n",
            }],
        },
        agent=IntentFixture(), output_root=tmp_path,
    )
    assert result["status"] == "READY", result["errors"]
    assert result["task"]["environment_bindings"][0]["output_paths"] == ["reports/result.md"]
    assert result["task"]["acceptance_obligations"][0]["evidence_ref_ids"] == ["user:1"]


def test_absolute_directory_alias_keeps_directory_marker() -> None:
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "o1", "verifier_kind": "FILE",
            "required_paths": ["/repo/docs/"], "initial_required_paths": [],
            "output_paths": ["/repo/docs/"], "observable": "目录包含要求的报告",
        }]},
        [{"id": "o1", "text": "创建报告", "evidence_ref_ids": ["user:1"]}],
        ["docs"], user_records=[{"id": "user:1", "text": "在 /repo/docs/ 下创建报告"}],
        path_aliases={"/repo/docs/": "docs"},
    )
    assert errors == []
    assert bindings[0]["required_paths"] == ["docs/"]
    assert bindings[0]["output_paths"] == ["docs/"]


def test_known_absolute_alias_survives_adjacent_chinese_prose() -> None:
    bindings, errors = normalize_environment_bindings(
        {"environment_bindings": [{
            "obligation_id": "o1", "verifier_kind": "FILE",
            "required_paths": ["report.md"], "initial_required_paths": [],
            "output_paths": ["report.md"], "observable": "报告正确",
        }]},
        [{"id": "o1", "text": "输出报告", "evidence_ref_ids": ["user:1"]}],
        ["report.md"], user_records=[{"id": "user:1", "text": "请保存为/repo/report.md。"}],
        path_aliases={"/repo/report.md": "report.md"},
    )
    assert errors == []
    assert bindings[0]["output_paths"] == ["report.md"]


def test_non_file_named_output_is_separate_from_initial_inputs() -> None:
    task = {
        "acceptance_obligations": [{"id": "obl-001", "text": "依据 source.txt 写 report.md"}],
        "environment_bindings": [{
            "obligation_id": "obl-001", "verifier_kind": "NON_FILE",
            "required_paths": ["source.txt", "report.md"],
            "initial_required_paths": ["source.txt"], "output_paths": ["report.md"],
            "observable": "报告忠实引用资料并回答原问题",
        }],
    }
    bindings, errors = normalize_environment_bindings(
        task, task["acceptance_obligations"], allowed_paths=["source.txt"],
        user_blob="依据 source.txt 写 report.md",
    )
    assert errors == []
    task["environment_bindings"] = bindings
    context = workspace_task_context(task)
    assert context["initial_required_paths"] == ["source.txt"]
    assert context["output_paths"] == ["report.md"]
    assert file_obligation_ids(task) == []
    task["environment_bindings"][0].pop("output_paths")
    assert workspace_task_context(task)["output_paths"] == []
