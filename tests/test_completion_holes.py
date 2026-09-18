from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceforge.reconstruction.completion_holes import (
    index_completion_holes,
    is_runtime_log,
    is_support_file,
    listing_stub_error,
)
from traceforge.reconstruction.env_replay import replay_from_timeline
from traceforge.reconstruction.environment_bindings import file_required_paths
from traceforge.reconstruction.terminal_universe_environment import (
    ReplayResult,
    validate_completion_candidate,
)
from traceforge.reconstruction.workspace_completion import (
    COMPLETION_PROMPT_VERSION,
    ENV_DEFAULT_EMPTY,
    ENV_REPLAYED,
    TASK_Q_EVIDENCE_ID,
    run_workspace_completion,
)


class RecordingRuntime:
    model_name = "test-model"
    backend = "hermes-sandbox"
    calls = 0
    instruction = ""

    def __init__(self, payload: dict | None = None):
        self.payload = payload or {
            "candidates": [
                {
                    "files": [],
                    "dependencies": [],
                    "runtime_constraints": [],
                    "uncertainties": [],
                    "decision": "READY",
                }
            ]
        }

    def run(self, *, role, instruction, session, output_root):
        del role, session, output_root
        self.calls += 1
        self.instruction = instruction
        from traceforge.reconstruction.agents.runtime import AgentResult

        return AgentResult(
            role="completion",
            backend=self.backend,
            payload=self.payload,
            final_text="{}",
            completed=True,
        )


def _read_foo() -> list[dict]:
    return [
        {
            "call_id": "c1",
            "name": "read_file",
            "arguments": {"path": "foo.py"},
            "result_text": "def main():\n    return 1\n",
        }
    ]


def test_support_file_names() -> None:
    assert is_support_file("requirements.txt")
    assert is_support_file("pyproject.toml")
    assert is_support_file("settings.yaml")
    assert not is_support_file("RobloxDLL.cpp")
    assert not is_support_file("foo.py")
    assert is_runtime_log("roblox_injector.log")
    assert not is_runtime_log("modules/Noclip.h")


def test_runtime_log_partial_is_not_a_hole() -> None:
    timeline = [
        *_read_foo(),
        {
            "call_id": "p1",
            "name": "read_file",
            "arguments": {"path": "modules/Noclip.h", "offset": 18, "limit": 20},
            "result_text": "  18: static int g_savedPartCount = 0;\n  19: \n  20: void Update() {\n",
        },
        {
            "call_id": "p2",
            "name": "read_file",
            "arguments": {"path": "roblox_injector.log", "offset": 1, "limit": 8},
            "result_text": "=== Injector Started ===\n[+] DLL found\n",
        },
    ]
    replay = replay_from_timeline(timeline)
    index = index_completion_holes(replay, timeline, {"core_objective": "读 noclip"})
    kinds = {item["path"]: item["kind"] for item in index.holes}
    assert kinds["modules/Noclip.h"] == "PARTIAL"
    assert "roblox_injector.log" not in kinds
    assert any(item.path == "roblox_injector.log" for item in replay.files)


def test_holes_skip_complete_read_only() -> None:
    replay = replay_from_timeline(_read_foo())
    index = index_completion_holes(replay, _read_foo(), {"core_objective": "读入口"})
    assert index.holes == ()
    assert "foo.py" in index.body_paths


def test_holes_include_partial_and_mentioned_support() -> None:
    timeline = [
        *_read_foo(),
        {
            "call_id": "p1",
            "name": "read_file",
            "arguments": {"path": "notes.md", "offset": 1, "limit": 2},
            "result_text": "hello\n",
        },
        {
            "call_id": "note",
            "name": "exec",
            "arguments": {"command": "echo app_name"},
            "result_text": "settings.yaml declares app_name",
        },
    ]
    replay = replay_from_timeline(timeline)
    index = index_completion_holes(
        replay, timeline, {"core_objective": "读 app_name 并解释 main"}
    )
    kinds = {item["path"]: item["kind"] for item in index.holes}
    assert kinds["notes.md"] == "PARTIAL"
    assert kinds["settings.yaml"] == "SUPPORT"
    assert "foo.py" not in kinds


def test_listing_ignores_compiler_outputs_and_binaries() -> None:
    timeline = [
        *_read_foo(),
        {
            "call_id": "ls1",
            "name": "exec",
            "arguments": {"command": "rg /Fo: Get-ChildItem"},
            "result_text": (
                "build.bat:12: cl.exe /c imgui\\imgui.cpp /Fo:imgui/imgui.obj\n"
                "E:\\Roblox IN\\RobloxInjector\\Injector.exe\n"
                "C:\\Program Files\\Microsoft Visual Studio\\Installer\\vswhere.exe\n"
                "RobloxDLL.cpp\n"
            ),
        },
    ]
    replay = replay_from_timeline(timeline)
    index = index_completion_holes(replay, timeline, {"core_objective": "读入口"})
    junk = {
        "Fo:imgui/imgui.obj",
        "imgui/imgui.obj",
        "IN/RobloxInjector/Injector.exe",
        "Studio/Installer/vswhere.exe",
        "Injector.exe",
        "vswhere.exe",
    }
    assert junk.isdisjoint(index.listing_names)
    assert all(item["path"] not in junk for item in index.holes)
    assert "RobloxDLL.cpp" in index.listing_names


def test_listing_does_not_invent_identifier_c_files() -> None:
    timeline = [
        *_read_foo(),
        {
            "call_id": "ls1",
            "name": "exec",
            "arguments": {"command": "rg dllName; Get-ChildItem"},
            "result_text": (
                "rg: imgui/: I/O error\n"
                "Mode LastWriteTime Length Name\n"
                "---- ------------- ------ ----\n"
                "-a---- 7/23/2026 1234 RobloxDLL.cpp\n"
                "Injector.cpp:42:    std::wstring dllName = config.targetName;\n"
                "Loader.cpp:10:    auto path = dllPath.c_str();\n"
                "    std::string name = value.c_str();\n"
                "    int v2 = 0; int v3 = 1;\n"
            ),
        },
    ]
    replay = replay_from_timeline(timeline)
    index = index_completion_holes(replay, timeline, {"core_objective": "读入口"})
    invented = {
        "dllName.c",
        "dllPath.c",
        "config.targetName.c",
        "name.c",
        "path.c",
        "value.c",
        "v2.c",
        "v3.c",
    }
    assert invented.isdisjoint(index.listing_names)
    assert all(item["path"] not in invented for item in index.holes)
    assert "RobloxDLL.cpp" in index.listing_names


def test_listing_ignores_versions_and_doi() -> None:
    timeline = [
        *_read_foo(),
        {
            "call_id": "ls1",
            "name": "exec",
            "arguments": {"command": "rg 0.5f"},
            "result_text": "0.5f\n10.1177\n0.4.12\nRobloxDLL.cpp\n",
        },
    ]
    replay = replay_from_timeline(timeline)
    index = index_completion_holes(replay, timeline, {"core_objective": "读入口"})
    assert "0.5f" not in index.listing_names
    assert "10.1177" not in index.listing_names
    assert "0.4.12" not in index.listing_names
    assert "RobloxDLL.cpp" in index.listing_names


def test_untrusted_read_is_not_missing_body() -> None:
    timeline = [
        {
            "call_id": "c1",
            "name": "exec",
            "arguments": {
                "command": (
                    "python3 -c \"from pathlib import Path; "
                    "Path('Injector.cpp').write_text('hacked')\""
                )
            },
            "result_text": "",
        },
        {
            "call_id": "c2",
            "name": "exec",
            "arguments": {"command": "cat Injector.cpp"},
            "result_text": "int main() { return 0; }\n",
        },
    ]
    replay = replay_from_timeline(timeline)
    index = index_completion_holes(replay, timeline, {"core_objective": "读 injector"})
    assert replay.files == ()
    assert all(item["path"] != "Injector.cpp" for item in index.holes)
    assert all(item["kind"] != "MISSING_BODY" for item in index.holes)


def test_listing_name_with_neighborhood_may_be_model_completed() -> None:
    timeline = [
        *_read_foo(),
        {
            "call_id": "ls1",
            "name": "exec",
            "arguments": {"command": "rg RobloxDLL.cpp"},
            "result_text": "RobloxDLL.cpp\nInjector.cpp\n",
        },
    ]
    replay = replay_from_timeline(timeline)
    index = index_completion_holes(replay, timeline, {"core_objective": "读入口"})
    assert "RobloxDLL.cpp" in index.listing_names
    kinds = {item["path"]: item["kind"] for item in index.holes}
    assert kinds.get("RobloxDLL.cpp") == "STUB"
    candidate = {
        "files": [
            {
                "path": "RobloxDLL.cpp",
                "content": "int main() { return 0; }\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": ["ls1"],
            }
        ],
        "decision": "READY",
    }
    ok, errors = validate_completion_candidate(
        candidate,
        replay,
        {"ls1", "c1"},
        listing_names=index.listing_names,
        body_paths=index.body_paths,
    )
    assert ok, errors
    stub = {
        "files": [
            {
                "path": "RobloxDLL.cpp",
                "content": "// observed name, body unobserved\n",
                "provenance": "SYNTHETIC_STUB",
                "evidence_ref_ids": ["ls1"],
            }
        ],
        "decision": "READY",
    }
    ok_stub, stub_errors = validate_completion_candidate(
        stub,
        replay,
        {"ls1", "c1"},
        listing_names=index.listing_names,
        body_paths=index.body_paths,
    )
    assert not ok_stub
    assert "BINDING_PATH_STUB_ONLY:RobloxDLL.cpp" in stub_errors


def test_empty_tree_invention_rejected() -> None:
    candidate = {
        "files": [
            {
                "path": "hermes-smoke-app/main.py",
                "content": "print(1)\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": ["note"],
            }
        ],
        "decision": "READY",
    }
    ok, errors = validate_completion_candidate(
        candidate, ReplayResult((), (), (), ()), {"note"}
    )
    assert not ok
    assert "EMPTY_TREE_INVENTION" in errors


def test_empty_tree_invention_allowed_for_default_empty() -> None:
    candidate = {
        "files": [
            {
                "path": "app.py",
                "content": "def main():\n    raise NotImplementedError\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": [TASK_Q_EVIDENCE_ID],
            }
        ],
        "decision": "READY",
    }
    ok, errors = validate_completion_candidate(
        candidate,
        ReplayResult((), (), (), ()),
        {TASK_Q_EVIDENCE_ID},
        required_paths=["app.py"],
        env_origin=ENV_DEFAULT_EMPTY,
    )
    assert ok, errors


def test_support_file_allowed_around_observed_project() -> None:
    replay = replay_from_timeline(_read_foo())
    candidate = {
        "files": [
            {
                "path": "requirements.txt",
                "content": "flask==3.0.0\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": ["c1"],
            }
        ],
        "dependencies": [],
        "runtime_constraints": [],
        "uncertainties": [],
        "decision": "READY",
    }
    ok, errors = validate_completion_candidate(candidate, replay, {"c1"})
    assert ok, errors


def test_replayed_path_runs_agent_then_keeps_tree(tmp_path: Path) -> None:
    replay = replay_from_timeline(_read_foo(), tmp_path / "bE0")
    runtime = RecordingRuntime()
    result = run_workspace_completion(
        task={"core_objective": "读入口"},
        replay=replay,
        timeline=_read_foo(),
        agent=runtime,
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "bE0",
    )
    assert runtime.calls == 1
    assert result["status"] == "READY"
    assert result["holes"] == []
    assert result["agent"]["skip_reason"] is None
    assert result["completion_strategy"] == "from_replayed"
    assert "from_replayed" in runtime.instruction
    workspace = Path(result["candidates"][0]["workspace"])
    assert (workspace / "foo.py").read_text(encoding="utf-8").startswith("def main")
    assert result["candidates"][0]["manifest"]["provenance"]["foo.py"]["kind"] == "REPLAYED"


def _l22_root() -> Path:
    return Path(__file__).resolve().parents[1] / "artifacts/eligible-live/L22"


def _replay_from_artifact(path: Path) -> ReplayResult:
    from traceforge.reconstruction.terminal_universe_environment import ReplayedFile

    payload = json.loads(path.read_text(encoding="utf-8"))
    files = tuple(
        ReplayedFile(
            item["path"],
            item["content"],
            item["first_observation_event_id"],
            item.get("completeness", "COMPLETE"),
        )
        for item in payload.get("files", [])
        if isinstance(item, dict)
    )
    return ReplayResult(files, (), (), ())


@pytest.mark.skipif(
    not (_l22_root() / "reconstruction_source.json").is_file()
    or not (_l22_root() / "replay.json").is_file(),
    reason="L22 artifacts missing",
)
def test_l22_listing_source_may_materialize_real_body_not_stub(tmp_path: Path) -> None:
    root = _l22_root()
    source_path = root / "reconstruction_source.json"
    replay_path = root / "replay.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    replay = _replay_from_artifact(replay_path)
    timeline = list(source.get("tool_timeline") or [])
    task = {"core_objective": "read the injector and report whether it still works"}
    index = index_completion_holes(replay, timeline, task)
    hole_kinds = {item["path"]: item["kind"] for item in index.holes}
    assert hole_kinds.get("RobloxDLL.cpp") in {None, "STUB"}
    assert "RobloxDLL.cpp" in index.listing_names
    refs = {str(item.get("call_id") or f"timeline:{i}") for i, item in enumerate(timeline)}
    invented = {
        "files": [
            {
                "path": "RobloxDLL.cpp",
                "content": "int main() { return 0; }\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": [next(iter(refs))],
            }
        ],
        "dependencies": [],
        "runtime_constraints": [],
        "uncertainties": [],
        "decision": "READY",
    }
    ok, errors = validate_completion_candidate(
        invented,
        replay,
        refs,
        listing_names=index.listing_names,
        body_paths=index.body_paths,
    )
    assert ok, errors
    stub = {
        "files": [
            {
                "path": "RobloxDLL.cpp",
                "content": "// observed name, body unobserved\n",
                "provenance": "SYNTHETIC_STUB",
                "evidence_ref_ids": [next(iter(refs))],
            }
        ],
        "dependencies": [],
        "runtime_constraints": [],
        "uncertainties": [],
        "decision": "READY",
    }
    ok_stub, stub_errors = validate_completion_candidate(
        stub,
        replay,
        refs,
        listing_names=index.listing_names,
        body_paths=index.body_paths,
    )
    assert not ok_stub
    assert "BINDING_PATH_STUB_ONLY:RobloxDLL.cpp" in stub_errors
    support = {
        "files": [
            {
                "path": "CMakeLists.txt",
                "content": "cmake_minimum_required(VERSION 3.20)\nproject(injector)\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": [next(iter(refs))],
            }
        ],
        "dependencies": [],
        "runtime_constraints": [],
        "uncertainties": [],
        "decision": "READY",
    }
    ok_support, support_errors = validate_completion_candidate(
        support, replay, refs, listing_names=index.listing_names, body_paths=index.body_paths
    )
    assert ok_support, support_errors
    runtime = RecordingRuntime({"candidates": [invented]})
    result = run_workspace_completion(
        task=task,
        replay=replay,
        timeline=timeline,
        agent=runtime,
        output_root=tmp_path / "l22",
    )
    assert result["status"] == "READY"
    assert result["completion_strategy"] == "from_replayed"
    materialized = [
        Path(record["workspace"]) / "RobloxDLL.cpp"
        for record in result["candidates"]
        if record.get("workspace")
    ]
    assert any(path.is_file() for path in materialized)
    assert all(
        "body unobserved" not in path.read_text(encoding="utf-8")
        for path in materialized
        if path.is_file()
    )


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "artifacts/eligible-live/L2/replay.json").is_file(),
    reason="L2 artifacts missing",
)
def test_l2_empty_replay_stays_empty(tmp_path: Path) -> None:
    replay_path = Path(__file__).resolve().parents[1] / "artifacts/eligible-live/L2/replay.json"
    replay = _replay_from_artifact(replay_path)
    runtime = RecordingRuntime()
    result = run_workspace_completion(
        task={"core_objective": "解释超时"},
        replay=replay,
        timeline=[],
        agent=runtime,
        output_root=tmp_path / "l2",
    )
    assert runtime.calls == 0
    assert result["status"] == "REVIEW"
    assert "EMPTY_REPLAY_TREE" in result["errors"]
    assert result["candidates"][0]["workspace"] is None


def test_skip_empty_tree_is_review(tmp_path: Path) -> None:
    runtime = RecordingRuntime()
    result = run_workspace_completion(
        task={"core_objective": "解释超时"},
        replay=ReplayResult((), (), (), ()),
        timeline=[],
        agent=runtime,
        output_root=tmp_path / "completion",
    )
    assert runtime.calls == 0
    assert result["status"] == "REVIEW"
    assert result["env_origin"] == ENV_REPLAYED
    assert "EMPTY_REPLAY_TREE" in result["errors"]
    assert result["agent"]["skip_reason"] == "EMPTY_REPLAY_TREE"
    assert result["candidates"][0]["workspace"] is None
    assert result["prompt_version"] == COMPLETION_PROMPT_VERSION


def test_default_empty_does_not_skip_and_rejects_stub(tmp_path: Path) -> None:
    RecordingRuntime.calls = 0
    runtime = RecordingRuntime(
        {
            "candidates": [
                {
                    "files": [
                        {
                            "path": "app.py",
                            "content": "// observed name, body unobserved\n",
                            "provenance": "MODEL_COMPLETED",
                            "evidence_ref_ids": [TASK_Q_EVIDENCE_ID],
                        }
                    ],
                    "dependencies": [],
                    "runtime_constraints": [],
                    "uncertainties": [],
                    "decision": "READY",
                }
            ]
        }
    )
    result = run_workspace_completion(
        task=_file_binding_task("app.py"),
        replay=ReplayResult((), (), (), ()),
        timeline=[],
        agent=runtime,
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "seed",
        env_origin=ENV_DEFAULT_EMPTY,
    )
    assert runtime.calls == 1
    assert result["env_origin"] == ENV_DEFAULT_EMPTY
    assert result["completion_strategy"] == "from_default_empty"
    assert result["status"] == "REVIEW"
    assert "EMPTY_REPLAY_TREE" not in result["errors"]
    assert result["agent"]["skip_reason"] is None
    assert TASK_Q_EVIDENCE_ID in result["evidence_ref_ids"]
    assert "from_default_empty" in runtime.instruction
    assert "TOOL_PROCESS_SKETCH" in runtime.instruction
    assert any(
        "BINDING_PATH_STUB_ONLY:app.py" in item
        for item in (result["candidates"][0]["errors"] + result["errors"])
    )


def test_default_empty_accepts_q_grounded_body(tmp_path: Path) -> None:
    RecordingRuntime.calls = 0
    runtime = RecordingRuntime(
        {
            "candidates": [
                {
                    "files": [
                        {
                            "path": "app.py",
                            "content": "def main():\n    raise NotImplementedError\n",
                            "provenance": "MODEL_COMPLETED",
                            "evidence_ref_ids": [TASK_Q_EVIDENCE_ID],
                        }
                    ],
                    "dependencies": [],
                    "runtime_constraints": [],
                    "uncertainties": [],
                    "decision": "READY",
                }
            ]
        }
    )
    result = run_workspace_completion(
        task=_file_binding_task("app.py"),
        replay=ReplayResult((), (), (), ()),
        timeline=[],
        agent=runtime,
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "seed",
        env_origin=ENV_DEFAULT_EMPTY,
    )
    assert runtime.calls == 1
    assert result["status"] == "READY"
    assert result["env_origin"] == ENV_DEFAULT_EMPTY
    assert "EMPTY_REPLAY_TREE" not in result["errors"]
    workspace = Path(result["candidates"][0]["workspace"])
    assert (workspace / "app.py").read_text(encoding="utf-8").startswith("def main")


def _file_binding_task(*paths: str) -> dict:
    return {
        "core_objective": "读入口并补齐头文件",
        "acceptance_obligations": [
            {"id": "obl-001", "text": "读代码", "evidence_ref_ids": ["user:0"]}
        ],
        "environment_bindings": [
            {
                "obligation_id": "obl-001",
                "required_paths": list(paths),
                "observable": "头文件可读",
                "verifier_kind": "FILE",
            }
        ],
    }


def test_partial_excerpt_is_kept_and_may_grow() -> None:
    timeline = [
        {
            "call_id": "c2",
            "name": "read_file",
            "arguments": {"path": "Injector.cpp", "offset": 1, "limit": 2},
            "result_text": "int inject() {\n",
        }
    ]
    replay = replay_from_timeline(timeline)
    assert replay.files
    assert replay.files[0].completeness == "PARTIAL"
    observed = replay.files[0].content
    grown = {
        "files": [
            {
                "path": "Injector.cpp",
                "content": observed + "    return 1;\n}\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": ["c2"],
            }
        ],
        "decision": "READY",
    }
    ok, errors = validate_completion_candidate(grown, replay, {"c2"})
    assert ok, errors
    assert observed in grown["files"][0]["content"]


def test_file_binding_stub_cannot_ready() -> None:
    replay = replay_from_timeline(_read_foo())
    task = _file_binding_task("Config.h")
    required = file_required_paths(task)
    assert required == ["Config.h"]
    stub = {
        "files": [
            {
                "path": "Config.h",
                "content": "// observed name, body unobserved\n",
                "provenance": "SYNTHETIC_STUB",
                "evidence_ref_ids": ["c1"],
            }
        ],
        "decision": "READY",
    }
    ok, errors = validate_completion_candidate(
        stub, replay, {"c1"}, required_paths=required
    )
    assert not ok
    assert "BINDING_PATH_STUB_ONLY:Config.h" in errors
    real = {
        "files": [
            {
                "path": "Config.h",
                "content": "#pragma once\nstruct Config { int port = 8080; };\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": ["c1"],
            }
        ],
        "decision": "READY",
    }
    ok_real, real_errors = validate_completion_candidate(
        real, replay, {"c1"}, required_paths=required
    )
    assert ok_real, real_errors


def test_ungrounded_listing_without_evidence_cannot_ready() -> None:
    timeline = [
        *_read_foo(),
        {
            "call_id": "ls1",
            "name": "exec",
            "arguments": {"command": "rg OrphanUtil.cpp"},
            "result_text": "OrphanUtil.cpp\n",
        },
    ]
    replay = replay_from_timeline(timeline)
    index = index_completion_holes(replay, timeline, {"core_objective": "读入口"})
    assert "OrphanUtil.cpp" in index.listing_names
    stub = {
        "files": [
            {
                "path": "OrphanUtil.cpp",
                "content": "// observed name, body unobserved\n",
                "provenance": "SYNTHETIC_STUB",
                "evidence_ref_ids": ["ls1"],
            }
        ],
        "decision": "READY",
    }
    ok_stub, stub_errors = validate_completion_candidate(
        stub,
        replay,
        {"ls1", "c1"},
        listing_names=index.listing_names,
        body_paths=index.body_paths,
    )
    assert not ok_stub
    assert "BINDING_PATH_STUB_ONLY:OrphanUtil.cpp" in stub_errors
    no_evidence = {
        "files": [
            {
                "path": "OrphanUtil.cpp",
                "content": "int orphan_util() { return 0; }\n",
                "provenance": "MODEL_COMPLETED",
                "evidence_ref_ids": [],
            }
        ],
        "decision": "READY",
    }
    ok_empty, empty_errors = validate_completion_candidate(
        no_evidence,
        replay,
        {"ls1", "c1"},
        listing_names=index.listing_names,
        body_paths=index.body_paths,
    )
    assert not ok_empty
    assert "EVIDENCE_REF_UNKNOWN:OrphanUtil.cpp" in empty_errors
    assert listing_stub_error(
        "OrphanUtil.cpp",
        "// observed name, body unobserved\n",
        listing_names=index.listing_names,
        body_paths=index.body_paths,
        replay_paths={item.path for item in replay.files},
    ) == "BINDING_PATH_STUB_ONLY:OrphanUtil.cpp"


def test_completion_prompt_requires_real_bodies(tmp_path: Path) -> None:
    timeline = [
        *_read_foo(),
        {
            "call_id": "p1",
            "name": "read_file",
            "arguments": {"path": "notes.md", "offset": 1, "limit": 2},
            "result_text": "hello\n",
        },
        {
            "call_id": "ls1",
            "name": "exec",
            "arguments": {"command": "rg RobloxDLL.cpp"},
            "result_text": "RobloxDLL.cpp\n",
        },
    ]
    replay = replay_from_timeline(timeline, tmp_path / "bE0")
    runtime = RecordingRuntime()
    result = run_workspace_completion(
        task=_file_binding_task("RobloxDLL.cpp"),
        replay=replay,
        timeline=timeline,
        agent=runtime,
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "bE0",
    )
    assert runtime.calls == 1
    assert result["prompt_version"] == COMPLETION_PROMPT_VERSION
    assert "generation targets" in runtime.instruction
    assert "body unobserved" in runtime.instruction
    assert "must exist as real bodies" in runtime.instruction
    assert "from_replayed" in runtime.instruction
    assert "from_default_empty" not in runtime.instruction
    assert "TOOL_PROCESS_SKETCH" not in runtime.instruction


def test_default_empty_without_process_logic_is_review(tmp_path: Path) -> None:
    runtime = RecordingRuntime()
    result = run_workspace_completion(
        task={"core_objective": "解释一下这段日志为什么超时"},
        replay=ReplayResult((), (), (), ()),
        timeline=[],
        agent=runtime,
        output_root=tmp_path / "completion",
        workspace_root=tmp_path / "seed",
        env_origin=ENV_DEFAULT_EMPTY,
    )
    assert runtime.calls == 0
    assert result["env_origin"] == ENV_DEFAULT_EMPTY
    assert result["completion_strategy"] == "from_default_empty"
    assert result["status"] == "REVIEW"
    assert "TOOL_PROCESS_INSUFFICIENT" in result["errors"]
    assert result["agent"]["skip_reason"] == "TOOL_PROCESS_INSUFFICIENT"
