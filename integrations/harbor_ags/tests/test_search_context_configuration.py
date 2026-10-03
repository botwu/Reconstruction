"""检索角色统一 rg 上下文分隔符，不改变 terminal 或 verifier。"""

import asyncio
from types import SimpleNamespace

import pytest
from traceforge.harbor_ags.search import SearchAGSEnvironment

from harbor_ags.environment import AGSPrebuiltEnvironment


def _environment(role):
    environment = object.__new__(SearchAGSEnvironment)
    environment._role = role
    environment.task_env_config = SimpleNamespace(env={})
    return environment


@pytest.mark.parametrize("role", ["agent", "verifier"])
def test_search_context_configuration_is_role_scoped(monkeypatch, role):
    monkeypatch.setattr(AGSPrebuiltEnvironment, "_without_model_env", lambda self, env: dict(env))
    environment = _environment(role)
    filtered = environment._without_model_env({"RIPGREP_CONFIG_PATH": "/untrusted", "PATH": "/bin"})
    assert filtered["PATH"] == "/bin"
    if role == "agent":
        assert filtered["RIPGREP_CONFIG_PATH"] == "/tmp/harbor_ags_runtime/search-ripgrep.conf"
    else:
        assert "RIPGREP_CONFIG_PATH" not in filtered


@pytest.mark.parametrize("role", ["agent", "verifier"])
def test_search_context_configuration_is_uploaded_after_role_setup(monkeypatch, role):
    calls = []

    async def prepare(self):
        calls.append("prepared")

    async def upload(source, target):
        assert calls == ["prepared"]
        calls.append((source.read_text(), target))

    monkeypatch.setattr(AGSPrebuiltEnvironment, "_prepare_role_files", prepare)
    environment = _environment(role)
    environment.upload_file = upload
    asyncio.run(environment._prepare_role_files())
    if role == "agent":
        assert calls == ["prepared", (
            "--field-context-separator=:\n", "/tmp/harbor_ags_runtime/search-ripgrep.conf",
        )]
    else:
        assert calls == ["prepared"]


def test_search_context_configuration_upload_failure_is_visible(monkeypatch):
    async def prepare(self):
        pass

    async def upload(source, target):
        raise OSError("配置上传失败")

    monkeypatch.setattr(AGSPrebuiltEnvironment, "_prepare_role_files", prepare)
    environment = _environment("agent")
    environment.upload_file = upload
    with pytest.raises(OSError, match="配置上传失败"):
        asyncio.run(environment._prepare_role_files())
