"""让原生 Harbor search 任务使用已部署的 AGS 运行时。"""

from __future__ import annotations

from harbor_ags.environment import AGSPrebuiltEnvironment

_SEARCH_ENV = frozenset({"SERPER_API_KEY", "JINA_API_KEY", "TRACEFORGE_FETCH_PROVIDER"})


class SearchAGSEnvironment(AGSPrebuiltEnvironment):
    """只为求解环境保留任务明确声明的检索变量，继续剥离模型凭据。"""

    def _without_model_env(self, env: dict[str, str]) -> dict[str, str]:
        filtered = super()._without_model_env(env)
        for name in _SEARCH_ENV:
            filtered.pop(name, None)
        if self.role == "agent":
            declared = self.task_env_config.env or {}
            filtered.update({name: env[name] for name in _SEARCH_ENV
                             if name in declared and name in env})
        return filtered
