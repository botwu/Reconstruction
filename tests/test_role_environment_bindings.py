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
)
from traceforge.reconstruction.intent_recovery import _gate
from traceforge.reconstruction.terminal_universe_environment import validate_completion_candidate
from traceforge.reconstruction.workspace_sufficiency import excerpt_or_stub_only_insufficiency
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


def test_file_bindings_drop_listing_only_names() -> None:
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
        file_binding_paths=bindable,
    )
    assert status == "READY"
    assert errors == []
    assert "RobloxDLL.cpp" not in file_required_paths(gated)
    assert "Injector.cpp" in file_required_paths(gated)


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
    assert is_fatal_tool_result("write_file", "error: PROTECTED_FILE_OVERWRITE:build.bat") is True
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


def test_non_file_paths_are_stripped_without_review() -> None:
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
    assert status == "READY", errors
    assert errors == []
    assert gated["environment_bindings"][0]["verifier_kind"] == "NON_FILE"
    assert gated["environment_bindings"][0]["required_paths"] == []


def test_non_file_coverage_is_not_required() -> None:
    task = _l22_style_task()
    assert "obl-001" not in file_obligation_ids(task)
    assert "obl-001" in non_file_obligation_ids(task)
    assert file_obligation_ids(task) == ["obl-002"]


def test_explicit_insufficient_is_never_upgraded_by_excerpt_keywords() -> None:
    assert not excerpt_or_stub_only_insufficiency(
        "INSUFFICIENT",
        "7 of 9 required files are stubs with observed name markers.",
        ["Injector.cpp — full source code (currently stub)"],
    )


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
                "required_paths": [],
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
        file_binding_paths=[],
    )
    assert errors == []
    assert bindings[0]["required_paths"] == [".pi-subagents/report.md"]
    assert bindings[0]["initial_required_paths"] == []
    assert bindings[0]["output_paths"] == [".pi-subagents/report.md"]
    task = {"environment_bindings": bindings}
    assert file_required_paths(task) == []


def test_missing_initial_input_is_not_reclassified_as_output() -> None:
    from traceforge.reconstruction.environment_bindings import derive_binding

    binding = derive_binding(
        {"id": "o1", "text": "修复 missing.py"},
        ["missing.py"],
        "修复 missing.py",
        file_binding_paths=[],
    )
    assert binding["initial_required_paths"] == ["missing.py"]
    assert binding["output_paths"] == []


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
        file_binding_paths=["src/foo.py"],
    )
    assert errors == []
    assert bindings[0]["required_paths"] == ["src/foo.py", "report.md"]
    assert bindings[0]["initial_required_paths"] == ["src/foo.py"]
    assert bindings[0]["output_paths"] == ["report.md"]
