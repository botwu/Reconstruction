from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from traceforge.reconstruction.environment_completion import run_environment_completion_container
from traceforge.reconstruction.container_verification import (
    ContainerTestRun,
    AGSRuntimeAdapter,
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
        async def exec(self, command, **kwargs): self.calls.append(("exec", kwargs["user"])); return "ok"
        async def upload_dir(self, source, target): self.calls.append(("upload", target))
        async def download_dir(self, source, target): self.calls.append(("download", source))
        def assert_cleanup_verified(self): self.calls.append(("audit",))
    async def case():
        env = Environment(); adapter = AGSRuntimeAdapter(env)
        await adapter.start(read_only=True); await adapter.exec("ls"); await adapter.stop()
        assert env.calls == [("start", False), ("exec", "user"), ("stop", True), ("audit",)]
    asyncio.run(case())


def test_environment_completion_publishes_standard_artifact(tmp_path):
    source = tmp_path / "source"; source.mkdir(); (source / "app.py").write_text("before", encoding="utf-8")
    runtime = FakeRuntime(tmp_path)
    async def agent(runtime, prompt):
        (runtime.remote / "context.txt").write_text("evidence", encoding="utf-8")
        return {"evidence_ref_ids_by_path": {"context.txt": ["ev-1"]}, "confidence": 0.8}
    root = run_environment_completion_container(
        task={"task": "x"}, attempt_ref="a", replay_workspace=source,
        evidence=[{"evidence_ref_id": "ev-1"}], output_root=tmp_path / "artifacts",
        runtime=runtime, agent_runner=agent
    )
    payload = __import__("json").loads((root / "environment_completion.json").read_text())
    assert payload["mode"] == "container-agentic"
    assert payload["candidates"][0]["status"] == "READY"
