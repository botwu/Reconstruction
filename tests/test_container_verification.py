from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

from traceforge.reconstruction.container_verification import (
    AGSRuntimeAdapter,
    ContainerTestRun,
    pytest_test_runner,
    run_completion_container,
    run_sufficiency_container,
    run_verifier_red_calibration,
)


class FakeRuntime:
    def __init__(self, root: Path):
        self.root = root
        self.remote = root / "remote"
        self.started = False
        self.read_only = False
        self.stopped = False

    async def start(self, *, read_only=False):
        self.started = True
        self.read_only = read_only

    async def stop(self, *, delete=True):
        self.stopped = True

    async def upload_dir(self, source_dir: Path, target_dir: str):
        self.remote.mkdir(parents=True, exist_ok=True)
        for path in source_dir.rglob("*"):
            if path.is_file():
                target = self.remote / path.relative_to(source_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)

    async def download_dir(self, source_dir: str, target_dir: Path):
        shutil.copytree(self.remote, target_dir)

    async def exec(self, command: str, *, cwd=None, timeout_sec=None, user=None):
        class Result:
            return_code = 1 if self.read_only and command.startswith("touch ") else 0

        return Result()


def test_completion_runs_inside_runtime_and_preserves_replay(tmp_path):
    async def case():
        source = tmp_path / "source"
        source.mkdir()
        (source / "app.py").write_text("before", encoding="utf-8")
        runtime = FakeRuntime(tmp_path)

        async def agent(runtime, prompt):
            (runtime.remote / "support.py").write_text("support", encoding="utf-8")
            return {"evidence_ref_ids_by_path": {"support.py": ["ev-1"]}}

        result = await run_completion_container(
            runtime=runtime,
            replay_workspace=source,
            task_prompt="complete context",
            evidence_refs={"ev-1"},
            output_workspace=tmp_path / "completed",
            agent_runner=agent,
        )
        assert result.status == "READY"
        assert result.added_files == ("support.py",)
        assert (result.workspace / "app.py").read_text() == "before"
        assert runtime.started and runtime.stopped
    asyncio.run(case())


def test_sufficiency_requires_read_only_probe(tmp_path):
    async def case():
        source = tmp_path / "source"
        source.mkdir()
        (source / "app.py").write_text("x", encoding="utf-8")
        runtime = FakeRuntime(tmp_path)

        async def judge(runtime, prompt):
            return {"label": "sufficient", "confidence": 0.9, "reason": "source present", "missing_critical": []}

        result = await run_sufficiency_container(
            runtime=runtime, workspace=source, task_prompt="solve", judge_runner=judge
        )
        assert result.label == "sufficient"
        assert result.write_blocked is True
        assert result.errors == ()
    asyncio.run(case())


def test_red_calibration_requires_missing_fail_and_protective_pass(tmp_path):
    async def case():
        runtime = FakeRuntime(tmp_path)

        async def tests(runtime, names):
            return tuple(
                ContainerTestRun(name, "FAIL" if name.startswith("missing") else "PASS")
                for name in names
            )

        result = await run_verifier_red_calibration(
            runtime=runtime,
            test_runner=tests,
            missing_test_names=("missing_capability",),
            protective_test_names=("protective_behavior",),
        )
        assert result.status == "READY"
        assert result.errors == ()
    asyncio.run(case())


def test_completion_rejects_replay_mutation(tmp_path):
    async def case():
        source = tmp_path / "source"
        source.mkdir()
        (source / "app.py").write_text("before", encoding="utf-8")
        runtime = FakeRuntime(tmp_path)

        async def agent(runtime, prompt):
            (runtime.remote / "app.py").write_text("changed", encoding="utf-8")
            return {"evidence_ref_ids_by_path": {}}

        result = await run_completion_container(
            runtime=runtime, replay_workspace=source, task_prompt="x", evidence_refs=set(),
            output_workspace=tmp_path / "completed", agent_runner=agent
        )
        assert result.status == "REVIEW"
        assert any(error.startswith("REPLAY_FILE_MUTATED") for error in result.errors)
    asyncio.run(case())


def test_ags_runtime_adapter_maps_lifecycle(tmp_path):
    class Environment:
        def __init__(self): self.calls = []
        async def start(self, force_build): self.calls.append(("start", force_build))
        async def stop(self, delete): self.calls.append(("stop", delete))
        async def exec(self, command, **kwargs):
            self.calls.append(("exec", kwargs["user"]))
            return "ok"
        async def upload_dir(self, source, target): self.calls.append(("upload", target))
        async def download_dir(self, source, target): self.calls.append(("download", source))
        def assert_cleanup_verified(self): self.calls.append(("audit",))
    async def case():
        env = Environment()
        adapter = AGSRuntimeAdapter(env)
        await adapter.start(read_only=True)
        await adapter.exec("ls")
        await adapter.stop()
        assert env.calls == [("start", False), ("exec", "user"), ("stop", True), ("audit",)]
    asyncio.run(case())


def test_pytest_runner_maps_exit_codes(tmp_path):
    async def case():
        runtime = FakeRuntime(tmp_path)
        class Result:
            return_code = 0
            stdout = "1 passed"
            stderr = ""
        async def execute(command, **kwargs):
            if "pytest" not in command:
                return SimpleNamespace(return_code=0, stdout="a" * 64, stderr="")
            if "pytest" in command:
                assert "test_ok" in command
                assert "python3 -I -c" in command
                assert "PYTHONPATH=" not in command
                assert not re.search(r"(?<![\w/])python -m pytest", command)
            return Result()
        runtime.exec = execute
        runs = await pytest_test_runner(runtime, ("test_ok",))
        assert runs[0].status == "PASS"
    asyncio.run(case())


def test_pytest_infrastructure_exit_codes_are_not_red_failures(tmp_path):
    async def case():
        runtime = FakeRuntime(tmp_path)
        for code, expected_status in [(1, "FAIL"), (2, "INFRA_ERROR"), (3, "INFRA_ERROR"), (4, "INFRA_ERROR"), (5, "INFRA_ERROR"), (127, "INFRA_ERROR")]:
            class Result:
                return_code = code
                stdout = ""
                stderr = ""
            async def execute(command, **kwargs):
                if "pytest" not in command:
                    class Probe:
                        return_code = 0
                        stdout = "a" * 64
                        stderr = ""
                    return Probe()
                return Result()
            runtime.exec = execute
            runs = await pytest_test_runner(runtime, ("test_target",))
            assert runs[0].status == expected_status, (code, runs)
    asyncio.run(case())


def test_verifier_test_mutation_is_infra_and_original_is_preserved(tmp_path):
    from traceforge.reconstruction.agents.sandbox import LocalExecRuntime, run_coro
    source = tmp_path / "source"
    source.mkdir()
    (source / "foo.py").write_text("ORIGINAL\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_outputs.py").write_text("import os\nfrom pathlib import Path\ndef test_mutates():\n    (Path(os.environ['TRACEFORGE_WORKSPACE']) / 'foo.py').write_text('CHANGED')\n    assert False\n")
    runtime = LocalExecRuntime(tmp_path / "runtime")
    async def case():
        await runtime.start(read_only=True)
        await runtime.upload_dir(source, "/home/user/workspace")
        await runtime.upload_dir(tests, "/tests")
        try:
            runs = await pytest_test_runner(runtime, ("test_mutates",))
            assert runs[0].status == "INFRA_ERROR"
            assert runs[0].error_code in {"VERIFIER_INPUT_MUTATED", "VERIFIER_INPUT_WRITE_DENIED"}
            assert (runtime.remote / "foo.py").read_text() == "ORIGINAL\n"
        finally:
            await runtime.stop()
    run_coro(case())


def test_local_exec_runtime_stages_vendor_and_separates_import_infra(tmp_path):
    from traceforge.reconstruction.agents.sandbox import LocalExecRuntime
    from traceforge.verifier.grading import prepare_pytest_site

    runtime = LocalExecRuntime(tmp_path / "runtime")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_outputs.py").write_text(
        "def test_pass():\n"
        "    assert True\n"
        "def test_fail():\n"
        "    assert False\n"
        "def test_import_infra():\n"
        "    import definitely_missing_traceforge_dependency\n",
        encoding="utf-8",
    )

    async def case():
        await runtime.start(read_only=True)
        await runtime.upload_dir(workspace, "/home/user/workspace")
        vendor_site = prepare_pytest_site(tmp_path / "vendor")
        await runtime.upload_dir(vendor_site.parent, "/tests")
        await runtime.upload_dir(tests, "/tests")
        runs = await pytest_test_runner(
            runtime,
            ("test_pass", "test_fail", "test_import_infra"),
        )
        assert [run.status for run in runs] == ["PASS", "FAIL", "INFRA_ERROR"]
        assert runs[2].error_code == "PYTEST_IMPORT_ERROR"
        await runtime.stop()

    asyncio.run(case())
