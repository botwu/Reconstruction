"""部署运行时可用时，验证检索变量不会被误当成模型凭据。"""

from types import SimpleNamespace

import pytest

pytest.importorskip("harbor_ags.environment")
from traceforge.harbor_ags.search import SearchAGSEnvironment  # noqa: E402


@pytest.mark.parametrize("role, declared, expected", [
    ("agent", {"SERPER_API_KEY", "JINA_API_KEY"}, {"SERPER_API_KEY", "JINA_API_KEY"}),
    ("agent", {"TRACEFORGE_FETCH_PROVIDER"}, {"TRACEFORGE_FETCH_PROVIDER"}),
    ("agent", set(), set()),
    ("verifier", {"SERPER_API_KEY", "JINA_API_KEY"}, set()),
])
def test_only_declared_search_variables_reach_agent(role, declared, expected):
    runtime = object.__new__(SearchAGSEnvironment)
    runtime._role = role
    runtime.task_env_config = SimpleNamespace(env=dict.fromkeys(declared, "configured"))
    search = {"SERPER_API_KEY", "JINA_API_KEY", "TRACEFORGE_FETCH_PROVIDER"}
    model = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TOKENHUB_KEY", "SERPER_OTHER"}
    environment = dict.fromkeys(search | model | {"PATH"}, "fixture-value")
    result = runtime._without_model_env(environment)
    assert set(result) == expected | {"PATH"}
    assert not model & set(result)



def test_declared_credentials_survive_real_ags_initialization(tmp_path, monkeypatch):
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths

    (tmp_path / "task/environment").mkdir(parents=True)
    (tmp_path / "task/workspace").mkdir()
    monkeypatch.setenv("SERPER_API_KEY", "fixture-serper")
    monkeypatch.setenv("JINA_API_KEY", "fixture-jina")
    runtime = SearchAGSEnvironment(
        environment_dir=tmp_path / "task/environment",
        environment_name="search-contract",
        session_id="search-contract__env",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=EnvironmentConfig(
            workdir="/home/user/workspace",
            env={"SERPER_API_KEY": "${SERPER_API_KEY}",
                 "JINA_API_KEY": "${JINA_API_KEY}",
                 "ANTHROPIC_API_KEY": "fixture-model"},
        ),
        template="fixture-template",
        api_key="fixture-ags",
    )
    startup = runtime._startup_env()
    per_command = runtime._merge_env({"TOKENHUB_KEY": "fixture-model"})
    for values in (startup, per_command):
        assert values["SERPER_API_KEY"] == "fixture-serper"
        assert values["JINA_API_KEY"] == "fixture-jina"
        assert "ANTHROPIC_API_KEY" not in values
        assert "TOKENHUB_KEY" not in values
    assert runtime.sandbox_id is None
