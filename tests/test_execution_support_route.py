from __future__ import annotations

from types import SimpleNamespace

from traceforge.reconstruction.eligible_reconstruction import execution_support_route
from traceforge.reconstruction.env_replay import replay_selected_environment
from traceforge.reconstruction.terminal_universe_environment import ReplayedFile


def _replay(*paths: str):
    return SimpleNamespace(
        files=tuple(ReplayedFile(path, "x", "e1") for path in paths)
    )


def _file_binding(path: str = "foo.py") -> dict:
    return {
        "obligation_id": "patch",
        "verifier_kind": "FILE",
        "required_paths": [path],
    }


def _non_file_binding(oid: str = "publish") -> dict:
    return {"obligation_id": oid, "verifier_kind": "NON_FILE", "required_paths": []}


def test_code_file_with_replay_tree_is_terminal_file() -> None:
    result = execution_support_route(
        task={"domain_route": "code_file"},
        source={"selected_span_has_file_ops": True, "route": "ELIGIBLE_CODE_FILE"},
        replay=_replay("foo.py", "bar.py"),
    )
    assert result["route"] == "TERMINAL_FILE"
    assert result["env_origin"] == "REPLAYED"
    assert result["allow_completion"] is True
    assert result["allow_file_verifier"] is False
    assert result["terminal_batch_hint"] is True

def test_terminal_domain_is_canonical_for_code_file_route() -> None:
    result = execution_support_route(
        task={"domain_route": "terminal"},
        source={"selected_span_has_file_ops": True},
        replay=_replay("app.py"),
    )
    assert result["route"] == "TERMINAL_FILE"
    assert result["domain_route"] == "terminal"


def test_retrieval_with_replayed_tree_still_plants() -> None:
    result = execution_support_route(
        task={"domain_route": "retrieval"},
        source={"selected_span_has_file_ops": True},
        replay=_replay("notes.md"),
    )
    assert result["route"] == "TERMINAL_FILE"
    assert result["env_origin"] == "REPLAYED"
    assert result["allow_completion"] is True
    assert result["allow_file_verifier"] is False


def test_empty_tree_without_bindings_is_default_empty() -> None:
    result = execution_support_route(
        task={"domain_route": "code_file"},
        source={"selected_span_has_file_ops": False, "route": "ELIGIBLE_CODE_FILE"},
        replay=_replay(),
    )
    assert result["route"] == "DEFAULT_EMPTY"
    assert result["env_origin"] == "DEFAULT_EMPTY"
    assert result["allow_completion"] is True
    assert result["allow_file_verifier"] is False


def test_empty_tree_with_file_obligation_is_default_empty() -> None:
    result = execution_support_route(
        task={"domain_route": "code_file", "environment_bindings": [_file_binding("app.py")]},
        source={"selected_span_has_file_ops": False, "route": "ELIGIBLE_CODE_FILE"},
        replay=_replay(),
    )
    assert result["route"] == "DEFAULT_EMPTY"
    assert result["env_origin"] == "DEFAULT_EMPTY"
    assert result["allow_completion"] is True
    assert result["allow_file_verifier"] is True


def test_empty_tree_all_non_file_is_none() -> None:
    result = execution_support_route(
        task={"domain_route": "code_file", "environment_bindings": [_non_file_binding()]},
        source={"selected_span_has_file_ops": False, "route": "ELIGIBLE_CODE_FILE"},
        replay=_replay(),
    )
    assert result["route"] == "NO_FILE_WORKSPACE"
    assert result["env_origin"] == "NONE"
    assert result["allow_completion"] is False
    assert result["allow_file_verifier"] is False
    assert result["reason_codes"] == ["NO_OBSERVABLE_FILES", "NO_FILE_ACCEPTANCE"]
    assert result["unverified_obligations"] == ["publish"]


def test_empty_retrieval_without_file_is_unsupported() -> None:
    result = execution_support_route(
        task={"domain_route": "retrieval"},
        source={"selected_span_has_file_ops": False},
        replay=_replay(),
    )
    assert result["route"] == "RETRIEVAL_UNSUPPORTED"
    assert result["env_origin"] == "NONE"
    assert result["allow_completion"] is False
    assert result["reason_codes"] == ["RETRIEVAL_UNSUPPORTED"]


def test_thin_tree_is_replayed_not_hard_stop() -> None:
    result = execution_support_route(
        task={"domain_route": "code_file"},
        source={"selected_span_has_file_ops": True, "route": "ELIGIBLE_CODE_FILE"},
        replay=_replay("build.bat"),
    )
    assert result["route"] == "TERMINAL_FILE"
    assert result["env_origin"] == "REPLAYED"
    assert result["allow_completion"] is True
    assert result["reason_codes"] == []
    assert result["terminal_batch_hint"] is False


def test_prior_visible_read_can_thicken_a_thin_selected_span() -> None:
    timeline = [
        {
            "call_id": "r0",
            "span_id": "s-other",
            "name": "exec",
            "arguments": {"command": "cat helper.py"},
            "result_text": "def helper():\n    return 2\n",
        },
        {
            "call_id": "r1",
            "span_id": "s-this",
            "name": "exec",
            "arguments": {"command": "cat foo.py"},
            "result_text": "def main():\n    return 1\n",
        },
    ]
    replay = replay_selected_environment(timeline, {"s-this"})
    assert {item.path for item in replay.files} == {"helper.py", "foo.py"}
    result = execution_support_route(
        task={"domain_route": "code_file"},
        source={"selected_span_has_file_ops": True, "route": "ELIGIBLE_CODE_FILE"},
        replay=replay,
    )
    assert result["route"] == "TERMINAL_FILE"
    assert result["env_origin"] == "REPLAYED"
    assert result["allow_completion"] is True
    assert result["terminal_batch_hint"] is True
    assert result["replay_file_count"] == 2


def test_two_source_files_remain_terminal() -> None:
    result = execution_support_route(
        task={"domain_route": "code_file"},
        source={"selected_span_has_file_ops": True, "route": "ELIGIBLE_CODE_FILE"},
        replay=_replay("Loader.cpp", "Noclip.h", "build.bat"),
    )
    assert result["route"] == "TERMINAL_FILE"
    assert result["allow_completion"] is True
    assert result["terminal_batch_hint"] is True


def test_mixed_file_and_non_file_still_allows_completion():
    task = {
        "domain_route": "code_file",
        "environment_bindings": [_non_file_binding(), _file_binding()],
    }
    result = execution_support_route(task=task, source={}, replay=_replay("foo.py", "bar.py"))
    assert result["allow_completion"] is True
    assert result["allow_file_verifier"] is True
    assert result["env_origin"] == "REPLAYED"
    assert result["route"] == "TERMINAL_FILE"
    assert result["unverified_obligations"] == ["publish"]


def test_all_non_file_with_tree_still_allows_completion():
    task = {
        "domain_route": "code_file",
        "environment_bindings": [_non_file_binding()],
    }
    result = execution_support_route(task=task, source={}, replay=_replay("foo.py", "bar.py"))
    assert result["allow_completion"] is True
    assert result["allow_file_verifier"] is False
    assert result["env_origin"] == "REPLAYED"
    assert result["route"] == "TERMINAL_FILE"
    assert result["unverified_obligations"] == ["publish"]


def test_task_rechecks_intent_then_completes_non_file_tree(tmp_path, monkeypatch):
    from traceforge.reconstruction import eligible_reconstruction as module

    def fake_completion(**kwargs):
        assert kwargs.get("replay") is not None
        return {
            "status": "READY",
            "errors": [],
            "candidates": [
                {
                    "decision": "READY",
                    "index": 0,
                    "workspace": str(tmp_path / "ws"),
                    "env_root": str(tmp_path / "env"),
                }
            ],
        }

    def fake_sufficiency(**kwargs):
        return {
            "label": "SUFFICIENT",
            "decision": "READY",
            "confidence": 0.8,
            "errors": [],
        }

    monkeypatch.setattr(module, "complete_from_replayed", fake_completion)
    monkeypatch.setattr(module, "run_workspace_sufficiency", fake_sufficiency)
    result = module._task_result(
        task={
            "task_id": "business",
            "environment_bindings": [_non_file_binding()],
        },
        root=tmp_path,
        source={},
        agent=None,
        verification_model=None,
        verification_config=None,
        replay=_replay("foo.py", "bar.py"),
        support={"allow_completion": True},
        task_source={"domain_route": "code_file"},
    )
    assert result["status"] == "REVIEW"
    assert result["stopped_at"] == "verification"
    assert result["errors"] == ["NO_FILE_ACCEPTANCE"]
    assert result["verification"]["status"] == "NOT_APPLICABLE"
    assert result["env_origin"] == "REPLAYED"
    assert result["workspace"] == str(tmp_path / "ws")
    assert result["sufficiency"]["label"] == "SUFFICIENT"
    assert result["execution_support_route"]["allow_completion"] is True
    assert result["execution_support_route"]["allow_file_verifier"] is False


def test_file_intent_remains_supported():
    result = execution_support_route(
        task={"environment_bindings": [_file_binding()]},
        source={"domain_route": "code_file"},
        replay=_replay("foo.py", "bar.py"),
    )
    assert result["allow_completion"] is True
    assert result["allow_file_verifier"] is True
    assert result["env_origin"] == "REPLAYED"


def test_empty_file_task_result_seeds_default_empty(tmp_path, monkeypatch):
    from traceforge.reconstruction import eligible_reconstruction as module

    def fake_completion(**kwargs):
        assert (tmp_path / "tasks" / "t1" / "initial_workspace").is_dir()
        return {"status": "REVIEW", "errors": ["STOP_FOR_TEST"], "candidates": []}

    monkeypatch.setattr(module, "complete_from_default_empty", fake_completion)
    result = module._task_result(
        task={
            "task_id": "t1",
            "environment_bindings": [_file_binding("app.py")],
        },
        root=tmp_path,
        source={},
        agent=None,
        verification_model=None,
        verification_config=None,
        replay=_replay(),
        support={"allow_completion": True, "env_origin": "DEFAULT_EMPTY"},
        task_source={"domain_route": "code_file"},
    )
    assert result["env_origin"] == "DEFAULT_EMPTY"
    assert result["execution_support_route"]["route"] == "DEFAULT_EMPTY"
    assert result["stopped_at"] == "completion"
    assert result["errors"] == ["STOP_FOR_TEST"]
